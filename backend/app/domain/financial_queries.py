"""Shared, owner-scoped read facts. Credit events are expenses, never cash."""
from datetime import datetime,timezone
from sqlalchemy import select,union_all,literal,case,func,String,cast,and_
from app.data.models import Transaction,DemoSetting,Card,Account
from app.data.financial_models import FinancialEvent
from app.domain.categories import category_expression
from app.domain.errors import DomainError
import re

def card_directory(db,owner):
    from app.domain.financial_profile import rows
    from app.domain.card_management import controls
    archived={r['id']:r for r in rows(db,owner,'cards')}
    result=[]
    for card in db.scalars(select(Card).where(Card.owner_id==owner).order_by(Card.id)):
        raw=archived.pop(card.id,{})
        control=controls(db,card)
        from app.domain.servicing import retired
        status='replaced' if retired(db,owner,card.id) else 'expired' if raw.get('status')=='expired' else 'lost' if control['lost'] else card.status
        result.append({**raw,'id':card.id,'owner_id':owner,'alias':card.alias,'brand':card.brand,'card_mask':'**'+card.card_last4,
            'card_last4':card.card_last4,'account_id':card.account_id,'status':status,'version':card.version,'controls':control,
            'card_type':'debit','read_only':False,'capabilities':[] if status in {'expired','replaced'} else ['debit_card_management']})
    for raw in archived.values():
        if raw.get('card_type')=='credit':
            from app.domain.servicing import credit_projection
            raw=credit_projection(db,owner,raw)
        result.append({**raw,'card_mask':'**'+raw['card_last4'],'account_id':raw.get('account_id') or None,
            'version':int(raw.get('version') or 1),'controls':{'limit_minor':int(raw.get('credit_limit_minor') or 0),'online_enabled':False,'lost':False},
            'read_only':True,'capabilities':['read_statements','read_expenses']})
    return sorted(result,key=lambda r:r['id'])

def require_card(db,owner,card_id):
    card=next((r for r in card_directory(db,owner) if r['id']==card_id),None)
    if not card:raise DomainError('RESOURCE_NOT_FOUND','银行卡不存在或不属于当前用户',404)
    return card

def resolve_card_reference(db,owner,reference):
    cards=card_directory(db,owner);ref=(reference or '').strip()
    tails=re.findall(r'(?<!\d)\d{4}(?!\d)',ref)
    choices=[c for c in cards if ref==c['id'] or ref==c['alias'] or c['card_last4'] in tails]
    if not choices and ref in {'信用卡','我的信用卡','本人信用卡'}:choices=[c for c in cards if c['card_type']=='credit']
    if len(choices)!=1:raise DomainError('MODEL_OUTPUT_MISMATCH','请明确一张本人银行卡的尾号；没有扩大到全部银行卡查询',422)
    return choices[0]

def fact_query(db,owner,account_ids=None,card_id=None,*,expense_only=False):
    """The same projection supplies summaries, statements, evidence and rankings."""
    filters=[Transaction.owner_id==owner]
    if account_ids is not None:filters.append(Transaction.account_id.in_(account_ids))
    if card_id:filters.append(Transaction.card_id==card_id)
    if expense_only:filters.extend([Transaction.status=='posted',Transaction.type.in_(['purchase','refund'])])
    cash=select(Transaction.id.label('id'),Transaction.owner_id,Transaction.account_id,Transaction.card_id,Transaction.type,
        Transaction.direction,Transaction.amount_minor,Transaction.currency,category_expression(Transaction.category).label('category'),
        Transaction.merchant,Transaction.posted_at_utc,Transaction.status,Transaction.business_id,Transaction.original_transaction_id,
        literal('cash_ledger').label('source')).where(*filters)
    setting=db.get(DemoSetting,1)
    credit_filters=[FinancialEvent.owner_id==owner,FinancialEvent.dataset_version==(setting.dataset_version if setting else ''),
                    FinancialEvent.event_type.in_(['credit_purchase','subscription_charge','credit_refund'])]
    if account_ids is not None:credit_filters.append(literal(False)) # a deposit-account filter is cash-only
    if card_id:credit_filters.append(FinancialEvent.card_id==card_id)
    refund=FinancialEvent.event_type=='credit_refund'
    credit=select(FinancialEvent.id,FinancialEvent.owner_id,(literal('credit:')+func.coalesce(FinancialEvent.card_id,FinancialEvent.liability_id)).label('account_id'),FinancialEvent.card_id,
        case((refund,'refund'),else_='purchase').label('type'),case((refund,'credit'),else_='debit').label('direction'),FinancialEvent.amount_minor,
        literal('CNY').label('currency'),category_expression(FinancialEvent.category).label('category'),FinancialEvent.merchant,FinancialEvent.posted_at_utc,
        literal('posted').label('status'),FinancialEvent.business_id,cast(literal(None),String).label('original_transaction_id'),literal('credit_card_ledger').label('source')).where(*credit_filters)
    return union_all(cash,credit).subquery('financial_facts')

def date_filtered(facts,period,category=None,merchant=None):
    filters=[facts.c.posted_at_utc>=period.start_utc,facts.c.posted_at_utc<period.end_utc]
    if category:filters.append(facts.c.category==category)
    if merchant:filters.append(facts.c.merchant.contains(merchant,autoescape=True))
    return filters

def fact_detail(db,owner,transaction_id):
    facts=fact_query(db,owner)
    found=db.execute(select(facts).where(facts.c.id==transaction_id)).mappings().all()
    if len(found)!=1:raise DomainError('RESOURCE_NOT_FOUND','交易不存在或不属于当前用户',404)
    return dict(found[0])

def list_facts(db,owner,*,start=None,end=None,account_id=None,card_id=None,category=None,merchant=None,
               type=None,direction=None,status=None,sort='date_desc',limit=20,offset=0):
    from app.domain.clock import selected_range,parse_day,DateRange
    from app.domain.categories import query_category
    from app.domain.spending import _coverage
    if not 1<=limit<=100 or offset<0:raise DomainError('INVALID_INPUT','分页参数无效')
    if account_id and not db.scalar(select(Account.id).where(Account.owner_id==owner,Account.id==account_id)):raise DomainError('RESOURCE_NOT_FOUND','账户不存在',404)
    if card_id:require_card(db,owner,card_id)
    setting=db.get(DemoSetting,1)
    if not setting:raise DomainError('DATA_UNAVAILABLE','数据尚未初始化',503)
    period=selected_range(start or '1900-01-01',end or setting.demo_date)
    facts=fact_query(db,owner,[account_id] if account_id else None,card_id)
    category=query_category(category);filters=date_filtered(facts,period,category,merchant)
    if direction:
        if direction not in {'debit','credit'}:raise DomainError('INVALID_INPUT','收支方向无效')
        filters.append(facts.c.direction==direction)
    if status:
        if status not in {'posted','pending','void'}:raise DomainError('INVALID_INPUT','交易状态无效')
        filters.append(facts.c.status==status)
    if type:
        from app.domain.ledger import KINDS
        if type not in KINDS:raise DomainError('INVALID_INPUT','交易类型无效')
        filters.append(facts.c.type==type)
    order={'date_desc':facts.c.posted_at_utc.desc(),'date_asc':facts.c.posted_at_utc.asc(),'amount_desc':facts.c.amount_minor.desc(),'amount_asc':facts.c.amount_minor.asc()}.get(sort)
    if order is None:raise DomainError('INVALID_INPUT','排序方式无效')
    total=db.scalar(select(func.count()).select_from(facts).where(*filters)) or 0
    items=[dict(r) for r in db.execute(select(facts).where(*filters).order_by(order,facts.c.id).offset(offset).limit(limit)).mappings()]
    accounts=[account_id] if account_id else list(db.scalars(select(Account.id).where(Account.owner_id==owner)))
    coverage=_coverage(db,owner,accounts,period)
    return {'items':items,'total':total,'limit':limit,'offset':offset,'has_more':offset+len(items)<total,
        'coverage':coverage['segments'],'query':dict(start=start,end=end,account_id=account_id,card_id=card_id,category=category,merchant=merchant,type=type,direction=direction,status=status,sort=sort),
        'included_sources':['cash_ledger','credit_card_ledger'] if not account_id else ['cash_ledger']}
