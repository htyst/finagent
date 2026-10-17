import json
from datetime import datetime,timedelta,timezone
import pytest
from sqlalchemy import select,func
from app.data.models import Card,DemoSession,DemoSetting,Account,Transaction,AuditEvent,AgentTask
from app.data.card_models import CardControl
from app.data.financial_models import FinancialDatasetRow
from app.data.servicing_models import ServiceRequest,CreditFacility,MerchantContractState
from app.data.subscription_models import SubscriptionAuthorization,SubscriptionCharge
from app.domain import servicing as svc,security,card_management
from app.domain.errors import DomainError
from app.agent.service import create,send
from app.agent.conversation_reads import confirm_subscription_query
from test_security import mfa
from test_dynamic_tools import dynamic,sequence,calls,reply,headers
from test_http import client


def add(db,table,key,payload):
    db.add(FinancialDatasetRow(dataset_version=db.get(DemoSetting,1).dataset_version,table_name=table,row_key=key,
        owner_id='u1',payload_json=json.dumps(payload)));db.commit()


def credit(db):
    add(db,'cards','credit-test',dict(id='credit-test',owner_id='u1',card_type='credit',card_last4='9876',alias='信用卡',
        status='active',credit_limit_minor='1500000',outstanding_minor='120000',available_credit_minor='1380000'))
    return 'credit-test'


def contract(db):
    add(db,'subscriptions','contract-test',dict(subscription_id='contract-test',service_name='轻听音乐（虚构）',
        auto_renew_enabled='true',status='active',access_end_date='2026-10-30',next_billing_date='2026-10-30',current_price_minor='1500',billing_cycle='monthly'))
    return 'contract-test'


def lost(db):
    card=db.scalar(select(Card).where(Card.owner_id=='u1',Card.status=='active'))
    c=db.get(CardControl,card.id)
    if not c:c=CardControl(card_id=card.id,limit_minor=30000,online_enabled=0,lost=1,version=1);db.add(c)
    else:c.lost=1;c.limit_minor=30000;c.online_enabled=0
    card.status='frozen';db.commit();return card


def confirm(db,rid,sid='s1'):
    if security.describe(db,sid,'service',rid)['level']=='red':mfa(db,rid,sid=sid,kind='service')
    token=svc.issue_token(db,session_id=sid,request_id=rid)['confirmation_token']
    return svc.confirm(db,session_id=sid,request_id=rid,confirmation_token=token),token


def test_replacement_strong_auth_retains_controls_and_permanently_retires_old(bank):
    db,_=bank;card=lost(db);account=db.get(Account,card.account_id);before=account.balance_minor
    rid=svc.prepare(db,session_id='s1',kind='replace_card',subject_id=card.id)['id']
    assert security.describe(db,'s1','service',rid)['level']=='red'
    token=svc.issue_token(db,session_id='s1',request_id=rid)['confirmation_token']
    with pytest.raises(DomainError) as no_mfa:svc.confirm(db,session_id='s1',request_id=rid,confirmation_token=token)
    assert no_mfa.value.code=='MFA_REQUIRED'
    result,token=confirm(db,rid);receipt=result['receipt'];new=db.get(Card,receipt['new_card_id'])
    assert result['status']=='executed' and new.account_id==card.account_id and new.status=='frozen'
    assert card_management.controls(db,new)==dict(limit_minor=30000,online_enabled=False,lost=False,version=1)
    assert account.balance_minor==before and svc.retired(db,'u1',card.id)
    with pytest.raises(DomainError) as retired:card_management.prepare(db,session_id='s1',kind='unloss',card_id=card.id)
    assert retired.value.code=='CARD_REPLACED'
    assert svc.confirm(db,session_id='s1',request_id=rid,confirmation_token=token)['receipt']==receipt
    assert db.scalar(select(func.count()).select_from(Card).where(Card.owner_id=='u1'))==3


def test_replacement_rejects_not_lost_other_owner_stale_and_cancelled(bank):
    db,_=bank;card=db.scalar(select(Card).where(Card.owner_id=='u1',Card.status=='active'))
    with pytest.raises(DomainError):svc.prepare(db,session_id='s1',kind='replace_card',subject_id=card.id)
    card=lost(db)
    with pytest.raises(DomainError):svc.prepare(db,session_id='other',kind='replace_card',subject_id=card.id)
    rid=svc.prepare(db,session_id='s1',kind='replace_card',subject_id=card.id)['id']
    db.get(CardControl,card.id).version+=1;db.commit()
    with pytest.raises(DomainError) as stale:confirm(db,rid)
    assert stale.value.code=='STALE_RESOURCE'
    svc.cancel(db,session_id='s1',request_id=rid)
    with pytest.raises(DomainError):confirm(db,rid)


def test_credit_application_does_not_adjust_until_independent_approval(bank):
    from app.domain.credit_cards import read
    db,_=bank;cid=credit(db)
    rid=svc.prepare(db,session_id='s1',kind='credit_limit',subject_id=cid,amount_minor=2000000)['id']
    result,token=confirm(db,rid)
    assert result['status']=='submitted' and read(db,session_id='s1')['credit_cards'][0]['credit_limit_minor']==1500000
    assert db.get(CreditFacility,(db.get(DemoSetting,1).dataset_version,cid)) is None
    assert svc.confirm(db,session_id='s1',request_id=rid,confirmation_token=token)['status']=='submitted'
    approved=svc.decide(db,session_id='s1',request_id=rid,decision='approve',reason='本地模拟测试通过')
    assert approved['status']=='executed'
    current=read(db,session_id='s1')['credit_cards'][0]
    assert current['credit_limit_minor']==2000000 and current['available_credit_minor']==1880000
    assert current['outstanding_minor']==120000 and current['limit_origin']=='local_simulated_approval'
    assert svc.decide(db,session_id='s1',request_id=rid,decision='approve',reason='重复')['receipt']==approved['receipt']
    with pytest.raises(DomainError):svc.decide(db,session_id='s1',request_id=rid,decision='reject',reason='不能改写')


@pytest.mark.parametrize('amount',[0,119999,1500000,100000001,True])
def test_credit_bounds(bank,amount):
    db,_=bank;cid=credit(db)
    with pytest.raises(DomainError):svc.prepare(db,session_id='s1',kind='credit_limit',subject_id=cid,amount_minor=amount)


def test_credit_rejection_and_stale_competing_application(bank):
    db,_=bank;cid=credit(db)
    first=svc.prepare(db,session_id='s1',kind='credit_limit',subject_id=cid,amount_minor=2000000)['id'];confirm(db,first)
    second=svc.prepare(db,session_id='s2',kind='credit_limit',subject_id=cid,amount_minor=2500000)['id'];confirm(db,second,'s2')
    svc.decide(db,session_id='s1',request_id=first,decision='approve',reason='通过')
    with pytest.raises(DomainError) as stale:svc.decide(db,session_id='s2',request_id=second,decision='approve',reason='旧申请')
    assert stale.value.code=='STALE_RESOURCE'
    assert svc.decide(db,session_id='s2',request_id=second,decision='reject',reason='资料已变化')['status']=='rejected'


def test_credit_approval_role_cannot_be_replaced_by_user_or_model(bank,client,monkeypatch,tmp_path):
    db,_=bank;cid=credit(db);client.cookies.set('finagent_demo_session','s1')
    keyfile=tmp_path/'approver.key';keyfile.write_text('isolated-approver',encoding='utf8');monkeypatch.setenv('FINAGENT_APPROVER_KEY_FILE',str(keyfile))
    rid=svc.prepare(db,session_id='s1',kind='credit_limit',subject_id=cid,amount_minor=2000000)['id']
    path='/api/service-requests/'+rid+'/decision';payload={'decision':'approve','reason':'测试'}
    assert client.post(path,headers=headers(),json=payload).status_code==403
    privileged={**headers(),'X-FinAgent-Approver':'isolated-approver'}
    assert client.post(path,headers=privileged,json=payload).status_code==409
    confirm(db,rid)
    assert client.post(path,headers=headers(),json=payload).status_code==403
    assert client.post(path,headers=privileged,json=payload).status_code==200


def test_contract_cancel_has_read_and_write_authorizations_and_preserves_archive(bank):
    from app.domain.financial_profile import imported_subscriptions,metadata
    db,_=bank;cid=contract(db);original=metadata(db,'u1','subscriptions',cid)
    with pytest.raises(DomainError) as missing:svc.prepare(db,session_id='s1',kind='cancel_contract',subject_id=cid)
    assert missing.value.code=='SUBSCRIPTION_CONFIRMATION_REQUIRED'
    security.confirm_subscription_access(db,'s1')
    rid=svc.prepare(db,session_id='s1',kind='cancel_contract',subject_id=cid)['id']
    assert security.describe(db,'s1','service',rid)['level']=='yellow'
    assert imported_subscriptions(db,'s1')['contracts'][0]['auto_renew_enabled']=='true'
    done,token=confirm(db,rid)
    projected=imported_subscriptions(db,'s1')['contracts'][0]
    assert projected['auto_renew_enabled'] is False and projected['access_end_date']=='2026-10-30' and projected['next_billing_date'] is None
    assert metadata(db,'u1','subscriptions',cid)==original
    assert svc.confirm(db,session_id='s1',request_id=rid,confirmation_token=token)['receipt']==done['receipt']
    from app.data.security_models import SecurityAuthorization
    db.scalar(select(SecurityAuthorization).where(SecurityAuthorization.resource=='subscription-query:s1')).status='revoked';db.commit()
    assert not svc.records(db,session_id='s1')['items']
    with pytest.raises(DomainError):svc.records(db,session_id='s1',kind='cancel_contract')


def test_merchant_cancellation_stops_future_charges_without_revoking_bank_mandate(bank):
    from app.domain.subscriptions import run_due_subscriptions,_charge_due
    db,factory=bank;cid=contract(db);security.confirm_subscription_access(db,'s1')
    now=datetime.now(timezone.utc);due=now.replace(hour=1,minute=0,second=0,microsecond=0)
    auth=SubscriptionAuthorization(id='test-mandate',owner_id='u1',dataset_version=db.get(DemoSetting,1).dataset_version,
        account_id='a1c',merchant='轻听音乐（虚构）',amount_minor=1500,period='monthly',charge_day=due.day,next_charge_at_utc=due.isoformat(),
        timezone='Asia/Shanghai',status='active',version=1,created_at_utc=now.isoformat());db.add(auth);db.commit()
    add(db,'native_subscription_links',auth.id,{'contract_id':cid,'authorization_id':auth.id})
    security.grant(db,'u1','subscription:'+auth.id,security.subscription_terms(auth),'yellow',(now+timedelta(days=1)).isoformat(),1);db.commit()
    rid=svc.prepare(db,session_id='s1',kind='cancel_contract',subject_id=cid)['id'];confirm(db,rid)
    before=db.get(Account,'a1c').balance_minor
    assert svc.merchant_stopped(db,auth) and auth.status=='active'
    run_due_subscriptions(factory,now=now+timedelta(days=1))
    assert _charge_due(db,auth.id,now+timedelta(days=1)) is None
    assert not db.scalar(select(SubscriptionCharge.id).where(SubscriptionCharge.authorization_id==auth.id))
    assert db.get(Account,'a1c').balance_minor==before


def test_dynamic_service_tools_prepare_fixed_cards_and_keep_missing_fields(dynamic,monkeypatch):
    db,_=dynamic;credit(db);sequence(monkeypatch,calls(('prepare_credit_limit_change',{'card':'9876'})))
    result=create(db,db.get(DemoSession,'s1'),'live','申请提高尾号9876信用卡额度')
    assert result['status']=='gathering_info' and not result['service_request']
    sequence(monkeypatch,calls(('prepare_credit_limit_change',{'amount_yuan':'20000'})))
    result=send(db,db.get(DemoSession,'s1'),result['id'],'申请额度20000元')
    assert result['service_request']['kind']=='credit_limit' and result['authorization_operations'][0]['level']=='red'


def test_dynamic_contract_query_confirmation_then_distinct_write_card(dynamic,client,monkeypatch):
    db,_=dynamic;contract(db);client.cookies.set('finagent_demo_session','s1')
    seen=sequence(monkeypatch,calls(('prepare_merchant_contract_cancel',{'merchant':'轻听音乐'})))
    result=create(db,db.get(DemoSession,'s1'),'live','取消轻听音乐的会员订阅')
    assert result['next_actions'] and not result['service_request'] and len(seen)==1
    result=confirm_subscription_query(db,db.get(DemoSession,'s1'),result['id'],result['next_actions'][0]['pending_id'])
    assert result['service_request']['kind']=='cancel_contract' and result['authorization_operations'][0]['level']=='yellow'
    assert not db.scalar(select(MerchantContractState.contract_id))


def test_contract_fixed_constraints_and_expired_entitlement_are_preserved(dynamic,monkeypatch):
    from app.domain.financial_profile import imported_subscriptions
    db,_=dynamic;cid=contract(db)
    row=db.get(FinancialDatasetRow,(db.get(DemoSetting,1).dataset_version,'subscriptions',cid))
    raw=json.loads(row.payload_json);raw['access_end_date']='2025-01-01';row.payload_json=json.dumps(raw);db.commit()
    security.confirm_subscription_access(db,'s1')
    sequence(monkeypatch,calls(('prepare_merchant_contract_cancel',{'merchant':'轻听音乐','operation_context':{
        'conditions':['保留本期权益至当前权益截止日'],'prohibitions':['不退款','不解除银行代扣','不取消其他订阅']}})))
    result=create(db,db.get(DemoSession,'s1'),'live','取消轻听音乐会员续订，保留本期权益，不退款，不取消其他订阅')
    rid=result['service_request']['id'];assert result['service_request']['summary']['access_period_state']=='expired'
    confirm(db,rid)
    projected=imported_subscriptions(db,'s1')['contracts'][0]
    assert projected['access_period_state']=='expired' and projected['access_end_date']=='2025-01-01' and projected['next_billing_date'] is None


def test_modified_service_order_invalidates_old_token_and_is_bound_to_new_amount(dynamic,monkeypatch):
    db,_=dynamic;credit(db)
    sequence(monkeypatch,calls(('prepare_credit_limit_change',{'card':'9876','amount_yuan':'20000'})))
    result=create(db,db.get(DemoSession,'s1'),'live','申请尾号9876的信用卡额度调到20000元')
    old=result['service_request']['id'];token=svc.issue_token(db,session_id='s1',request_id=old)['confirmation_token']
    sequence(monkeypatch,calls(('prepare_credit_limit_change',{'card':'9876','amount_yuan':'25000','operation_context':{'behavior':'modify'}})))
    result=send(db,db.get(DemoSession,'s1'),result['id'],'改为申请尾号9876的信用卡额度25000元')
    assert result['service_request']['id']!=old and result['service_request']['summary']['requested_limit_minor']==2500000
    assert db.get(ServiceRequest,old).status=='invalidated'
    with pytest.raises(DomainError):svc.confirm(db,session_id='s1',request_id=old,confirmation_token=token)


def test_unfinished_service_cannot_bypass_pause_or_user_cancellation(dynamic,monkeypatch):
    from app.agent.control import change
    db,_=dynamic;credit(db)
    sequence(monkeypatch,calls(('prepare_credit_limit_change',{'card':'9876','amount_yuan':'20000'})))
    result=create(db,db.get(DemoSession,'s1'),'live','申请尾号9876信用卡额度提高到20000元')
    rid=result['service_request']['id'];task=db.get(AgentTask,result['id']);change(db,task,'pause','暂停')
    with pytest.raises(DomainError):svc.issue_token(db,session_id='s1',request_id=rid)
    change(db,task,'resume','继续')
    from app.agent.service import cancel
    result=cancel(db,db.get(DemoSession,'s1'),task.id)
    assert db.get(ServiceRequest,rid).status=='invalidated' and result['status']=='cancelled'


@pytest.mark.parametrize('text,args',[
 ('如何补卡',{'card':'1234'}),('不要补卡',{'card':'1234'}),
 ('明天帮我补卡',{'card':'1234'}),
 ('帮我补卡并保留原卡号',{'card':'1234'}),
 ('申请提高尾号9876信用卡额度5000元',{'card':'9876','amount_yuan':'5000'}),
 ('申请降低尾号9876信用卡额度20000元',{'card':'9876','amount_yuan':'20000'}),
 ('申请把尾号9876信用卡额度调到20000元',{'card':'9876','amount_yuan':'25000'}),
 ('申请把尾号9876信用卡额度调到20000美元',{'card':'9876','amount_yuan':'20000'}),
 ('申请把尾号9876信用卡额度调到20000元',{'card':'9876','amount_yuan':'20000','operation_context':{'conditions':['收入超过指定值才申请']}}),
 ('申请把尾号9876信用卡额度调到20000元',{'card':'9999','amount_yuan':'20000'}),
])
def test_service_grounding_rejects_negation_and_model_invented_fields(dynamic,monkeypatch,text,args):
    db,_=dynamic;credit(db)
    tool='prepare_card_replacement' if '补卡' in text else 'prepare_credit_limit_change'
    sequence(monkeypatch,calls((tool,args)),reply('还需要核对明确办理信息，当前未办理。'))
    result=create(db,db.get(DemoSession,'s1'),'live',text)
    assert not result['service_request'] and not db.scalar(select(ServiceRequest.id))
