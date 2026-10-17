"""Owner-scoped financial archive and current bank balances; no payment tools."""
import json
from datetime import datetime,timezone
from decimal import Decimal
from sqlalchemy import select,inspect,exists,literal,or_,func
from app.data.financial_models import FinancialDatasetRow,FinancialDatasetImport
from app.data.models import Account,DemoSetting,Card
from app.domain.errors import DomainError

def available(db):
    if '_financial_archive_available' not in db.info:
        db.info['_financial_archive_available']=inspect(db.get_bind()).has_table('financial_dataset_rows')
    return db.info['_financial_archive_available']

def rows(db,owner,table,*,limit=None,offset=0):
    if not available(db):return []
    setting=db.get(DemoSetting,1)
    if not setting:return []
    return [json.loads(r.payload_json) for r in db.scalars(select(FinancialDatasetRow).where(
        FinancialDatasetRow.dataset_version==setting.dataset_version,FinancialDatasetRow.owner_id==owner,
        FinancialDatasetRow.table_name==table).order_by(FinancialDatasetRow.row_key).offset(offset).limit(limit))]

def dataset_manifest(db):
    setting=db.get(DemoSetting,1)
    if not setting or not available(db):return None
    row=db.get(FinancialDatasetImport,setting.dataset_version)
    return json.loads(row.manifest_json) if row else None

HISTORY_TABLES={'credit_card_statements':'statement_date','liability_events':'occurred_at_local',
    'monthly_financial_snapshots':'as_of','monthly_summary':'month','investment_valuations':'valuation_date',
    'deposits':'opened_on','liabilities':'origination_date','insurance_policies':'coverage_starts_on',
    'auto_debit_mandates':'authorization_started_on','subscription_billing_events':'scheduled_date'}

def read_history(db,owner,table,*,start=None,end=None,card_id=None,liability_id=None,subscription_id=None,status=None,limit=50,offset=0,session_id=None):
    if table not in HISTORY_TABLES:raise DomainError('INVALID_INPUT','不支持的档案明细类型',422)
    if not 1<=limit<=100 or offset<0:raise DomainError('INVALID_INPUT','分页参数无效')
    if table in {'auto_debit_mandates','subscription_billing_events'}:
        from app.domain.security import assert_subscription_access
        assert_subscription_access(db,session_id)
    setting=db.get(DemoSetting,1)
    if not setting:raise DomainError('DATA_UNAVAILABLE','数据未初始化',503)
    if not available(db):return {'items':[],'total':0,'limit':limit,'offset':offset,'has_more':False}
    filters=[FinancialDatasetRow.owner_id==owner,FinancialDatasetRow.dataset_version==setting.dataset_version,FinancialDatasetRow.table_name==table]
    date_value=func.substr(func.json_extract(FinancialDatasetRow.payload_json,'$.'+HISTORY_TABLES[table]),1,7 if table=='monthly_summary' else 10)
    if start or end:
        from app.domain.clock import parse_day
        if start:parse_day(start);filters.append(date_value>=start[:7] if table=='monthly_summary' else date_value>=start)
        if end:parse_day(end);filters.append(date_value<=end[:7] if table=='monthly_summary' else date_value<=end)
        if start and end and start>end:raise DomainError('INVALID_INPUT','开始日期不能晚于结束日期')
    if card_id:
        from app.domain.financial_queries import require_card
        require_card(db,owner,card_id)
    for key,value in [('card_id',card_id),('liability_id',liability_id),('subscription_id',subscription_id),('status',status)]:
        if value:filters.append(func.json_extract(FinancialDatasetRow.payload_json,'$.'+key)==value)
    total=db.scalar(select(func.count()).select_from(FinancialDatasetRow).where(*filters)) or 0
    result=[json.loads(row) for row in db.scalars(select(FinancialDatasetRow.payload_json).where(*filters).order_by(date_value.desc(),FinancialDatasetRow.row_key.desc()).limit(limit).offset(offset))]
    return {'table':table,'items':result,'total':total,'limit':limit,'offset':offset,'has_more':offset+len(result)<total,
            'dataset_version':setting.dataset_version,'snapshot_as_of':setting.demo_date,'query':dict(start=start,end=end,card_id=card_id,liability_id=liability_id,subscription_id=subscription_id,status=status)}

def integer(row,key):return int(row.get(key) or 0)

def metadata(db,owner,table,identifier):
    if not available(db):return None
    setting=db.get(DemoSetting,1)
    if not setting:return None
    r=db.get(FinancialDatasetRow,(setting.dataset_version,table,identifier))
    return json.loads(r.payload_json) if r and r.owner_id==owner else None

def guard_card(db,owner,card_id):
    from app.domain.servicing import retired
    if retired(db,owner,card_id):raise DomainError('CARD_REPLACED','该原卡已补发并永久停用，不能解挂、解冻或消费',409)
    card=metadata(db,owner,'cards',card_id)
    if card and card.get('card_type')=='credit':
        raise DomainError('CARD_READ_ONLY','这张导入信用卡仅支持查询账单与消费，尚未接入支付或卡管理',409)
    if card and card.get('status')=='expired':
        raise DomainError('CARD_EXPIRED','这张案例卡已到期；解冻或调整限额不能恢复到期卡，请使用有效卡',409)

def portfolio(db,owner):
    overview=rows(db,owner,'用户金融总览')
    if not overview:return {'available':False}
    setting=db.get(DemoSetting,1)
    overview=overview[0]
    # Subscription details remain behind the existing independent yellow query.
    overview={k:v for k,v in overview.items() if '订阅' not in k and '续费' not in k}
    accounts=rows(db,owner,'accounts')
    native={a.id:a for a in db.scalars(select(Account).where(Account.owner_id==owner))}
    term_ids={a['id'] for a in accounts if a['account_type']=='term_deposit'}
    demand=sum(a.balance_minor for a in native.values() if a.id not in term_ids)
    term=sum(a.balance_minor for a in native.values() if a.id in term_ids)
    from app.domain.ledger import available_balance
    spendable=sum(available_balance(db,a) for a in native.values() if a.status=='active')
    investment=sum(integer(r,'market_value_minor') for r in rows(db,owner,'investment_holdings'))
    physical=sum(integer(r,'estimated_value_minor') for r in rows(db,owner,'tangible_assets'))
    liabilities=rows(db,owner,'liabilities')
    debt=sum(integer(r,'outstanding_balance_minor') for r in liabilities)
    # Newly executed native wealth trades are separate from imported read-only positions.
    from app.data.wealth_models import WealthPosition,WealthProduct
    new_wealth=sum(p.units_hundredths*product.nav_minor//100 for p,product in db.execute(
        select(WealthPosition,WealthProduct).join(WealthProduct).where(WealthPosition.owner_id==owner)))
    total=demand+term+investment+new_wealth+physical
    from app.domain.financial_queries import card_directory
    from app.domain.spending import _coverage
    from app.domain.clock import selected_range
    manifest=dataset_manifest(db) or {}
    coverage=_coverage(db,owner,list(native),selected_range(manifest.get('period_start',setting.demo_date),manifest.get('period_end',setting.demo_date)))
    overview={**overview,'活期存款_元':f'{demand/100:.2f}','当前可用活期_元':f'{spendable/100:.2f}','定期存款本金_元':f'{term/100:.2f}',
        '全部银行存款_元':f'{(demand+term)/100:.2f}','总资产_元':f'{total/100:.2f}','净资产_元':f'{(total-debt)/100:.2f}'}
    return {'available':True,'dataset_version':setting.dataset_version,'as_of':setting.demo_date,'snapshot_as_of':setting.demo_date,
        'bank_queried_at_utc':datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'overview':overview,'balances':{'demand_minor':demand,'term_minor':term,'available_minor':spendable,
            'investment_snapshot_minor':investment,'new_app_wealth_minor':new_wealth,'physical_minor':physical,
            'liabilities_snapshot_minor':debt,'total_assets_minor':total,'net_worth_minor':total-debt},
        'accounts':[{**r,'balance_minor':native[r['id']].balance_minor if r['id'] in native else integer(r,'balance_minor'),
            'current_status':native[r['id']].status if r['id'] in native else r['status']} for r in accounts],
        'cards':card_directory(db,owner),
        'deposits':rows(db,owner,'deposits'),'investments':rows(db,owner,'investment_holdings'),
        'liabilities':liabilities,'physical_assets':rows(db,owner,'tangible_assets'),'insurance':rows(db,owner,'insurance_policies'),
        'history':rows(db,owner,'monthly_financial_snapshots'),
        'snapshot_note':'银行余额随本系统操作更新；基金、贷款、信用卡欠款、房车和保险为2026-09-24导入档案，未新增其交易执行能力。',
        'coverage':{**coverage,'scope':'声明的合成案例资料；非银行资产负债为导入日快照'}}

def imported_subscriptions(db,session_id):
    from app.domain.security import assert_subscription_access
    from app.domain.operations import _session,_now
    assert_subscription_access(db,session_id)
    session,_=_session(db,session_id,_now())
    contracts=rows(db,session.owner_id,'subscriptions')
    links=rows(db,session.owner_id,'native_subscription_links')
    linkmap={r['contract_id']:r for r in links}
    from app.data.subscription_models import SubscriptionAuthorization
    from app.domain.servicing import contract_projection
    contracts=[contract_projection(db,session.owner_id,c) for c in contracts]
    for contract in contracts:
        link=linkmap.get(contract['subscription_id'])
        contract['native_authorization_id']=link['authorization_id'] if link else None
        if link and (auth:=db.get(SubscriptionAuthorization,link['authorization_id'])):
            contract['current_bank_mandate_status']=auth.status
        else:contract['current_bank_mandate_status']='archive_only'
    return {'as_of':db.get(DemoSetting,1).demo_date,'dataset_version':session.dataset_version,'contracts':contracts,
        'billing_events':rows(db,session.owner_id,'subscription_billing_events',limit=500),
        'note':'历史合同完整保留。提醒和解除银行代扣仅适用于可办理的按月借记卡授权；商户续订可通过单独的本地模拟合同取消单关闭，不代表真实平台已取消。导入不签发新的后台扣款授权。'}

def executable_subscription_filter():
    """Imported history is not authority to catch up months of unpaid debits."""
    from app.data.subscription_models import SubscriptionAuthorization
    from app.data.security_models import SecurityAuthorization
    imported=select(FinancialDatasetRow.row_key).where(FinancialDatasetRow.table_name=='native_subscription_links')
    grant=exists(select(SecurityAuthorization.id).where(
        SecurityAuthorization.resource==literal('subscription:')+SubscriptionAuthorization.id,
        SecurityAuthorization.owner_id==SubscriptionAuthorization.owner_id,
        SecurityAuthorization.status=='active',SecurityAuthorization.remaining>0))
    from app.data.servicing_models import MerchantContractState
    stopped=exists(select(MerchantContractState.contract_id).join(FinancialDatasetRow,
        (func.json_extract(FinancialDatasetRow.payload_json,'$.contract_id')==MerchantContractState.contract_id) &
        (FinancialDatasetRow.dataset_version==MerchantContractState.dataset_version) &
        (FinancialDatasetRow.owner_id==MerchantContractState.owner_id)).where(
        FinancialDatasetRow.table_name=='native_subscription_links',FinancialDatasetRow.row_key==SubscriptionAuthorization.id,
        MerchantContractState.owner_id==SubscriptionAuthorization.owner_id,MerchantContractState.dataset_version==SubscriptionAuthorization.dataset_version))
    return or_(SubscriptionAuthorization.id.not_in(imported),grant) & ~stopped
