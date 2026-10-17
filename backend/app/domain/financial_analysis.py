"""Server-side aggregates for amounts that an LLM must not add from excerpts."""
from sqlalchemy import select,func,case
from app.data.models import Account,DemoSetting
from app.domain.clock import current_month_range,selected_range,parse_day
from app.domain.categories import query_category
from app.domain.financial_queries import fact_query,date_filtered,require_card
from app.domain.errors import DomainError

def analyze(db,owner,kind,*,start=None,end=None,account_id=None,card_id=None,category=None,merchant=None,top_n=10):
    if kind not in {'expense_by_merchant','cashflow','largest_expenses','daily_expenses'}:raise DomainError('INVALID_INPUT','分析类型无效')
    if not 1<=top_n<=50:raise DomainError('INVALID_INPUT','排名条数须为1至50')
    setting=db.get(DemoSetting,1)
    if bool(start)!=bool(end):raise DomainError('INVALID_INPUT','请提供完整查询日期')
    period=selected_range(start,end) if start else current_month_range(parse_day(setting.demo_date))
    if account_id and not db.scalar(select(Account.id).where(Account.owner_id==owner,Account.id==account_id)):raise DomainError('RESOURCE_NOT_FOUND','账户不存在',404)
    if card_id:require_card(db,owner,card_id)
    category=query_category(category)
    f=fact_query(db,owner,[account_id] if account_id else None,card_id,expense_only=kind!='cashflow')
    filters=date_filtered(f,period,category,merchant)
    if kind=='cashflow':
        filters.extend([f.c.source=='cash_ledger',f.c.status=='posted',f.c.type.not_in(['internal_transfer','deposit_funding','deposit_redemption'])])
        credit=case((f.c.direction=='credit',f.c.amount_minor),else_=0);debit=case((f.c.direction=='debit',f.c.amount_minor),else_=0)
        income=case((f.c.type=='income',f.c.amount_minor),else_=0)
        incoming,outgoing,salary,count=db.execute(select(func.sum(credit),func.sum(debit),func.sum(income),func.count()).where(*filters)).one()
        summary=dict(inflow_minor=int(incoming or 0),outflow_minor=int(outgoing or 0),income_minor=int(salary or 0),net_cashflow_minor=int((incoming or 0)-(outgoing or 0)),transaction_count=count)
        items=[];basis='现金账户对外现金流；剔除本人转账与定期本金内部调拨，信用卡还款是现金流出，信用卡消费不再重复计现金流出'
    elif kind=='largest_expenses':
        filters.append(f.c.type=='purchase')
        total=db.scalar(select(func.count()).select_from(f).where(*filters)) or 0
        items=[dict(r) for r in db.execute(select(f).where(*filters).order_by(f.c.amount_minor.desc(),f.c.id).limit(top_n)).mappings()]
        summary={'eligible_purchase_count':total,'returned_count':len(items)};basis='全筛选集合内已入账消费按原消费金额排名；退款另列，不以首页摘录排名'
    else:
        key=func.coalesce(f.c.merchant,'商户未提供') if kind=='expense_by_merchant' else func.strftime('%Y-%m-%d',f.c.posted_at_utc,'+8 hours')
        purchase=func.sum(case((f.c.type=='purchase',f.c.amount_minor),else_=0));refund=func.sum(case((f.c.type=='refund',f.c.amount_minor),else_=0));net=purchase-refund
        rows=db.execute(select(key.label('group'),purchase.label('purchase_minor'),refund.label('refund_minor'),net.label('net_minor'),func.count().label('transaction_count')).where(*filters).group_by(key).order_by(net.desc() if kind=='expense_by_merchant' else key).limit(top_n)).mappings()
        items=[dict(r) for r in rows]
        allgroups=db.scalar(select(func.count()).select_from(select(key).where(*filters).group_by(key).subquery())) or 0
        total=db.scalar(select(func.coalesce(func.sum(case((f.c.type=='purchase',f.c.amount_minor),else_=-f.c.amount_minor)),0)).where(*filters))
        summary={'net_minor':int(total or 0),'group_count':allgroups,'returned_groups':len(items),'has_more_groups':allgroups>len(items)};basis='全量已入账现金与信用卡消费减退款，归组后排名/按北京时间日期汇总'
    return {'analysis_kind':kind,'period':period.as_dict(),'summary':summary,'items':items,'basis':basis,
        'query':dict(start=start,end=end,account_id=account_id,card_id=card_id,category=category,merchant=merchant,top_n=top_n),
        'dataset_version':setting.dataset_version}
