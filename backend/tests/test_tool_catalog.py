import json
import pytest
from sqlalchemy import select,func
from app.agent import dynamic_protocol as protocol,dynamic_runtime as runtime,registry,tool_catalog,authorization
from app.agent.service import create,send
from app.data.models import DemoSession,Card,AgentTask
from app.data.card_models import CardControl,CardRequest
from app.data.subscription_models import SubscriptionAuthorization,SubscriptionRequest
from app.domain import subscriptions,security
from app.domain.errors import DomainError
from app.agent.conversation_reads import confirm_subscription_query
from test_dynamic_tools import dynamic,sequence,calls,reply,headers
from test_http import client
from test_security import mfa


def session(db):return db.get(DemoSession,'s1')


def test_catalog_matches_all_actual_tools_and_contains_no_execution_api():
    catalog=tool_catalog.catalog();names=set(protocol.models())
    assert catalog['tool_count']==62
    assert set().union(*(set(v) for v in catalog['groups'].values()))==names
    assert all(not any(k in n for k in ('execute','confirm','token','verify','password')) for n in names)
    assert 'prepare_card_loss' in names and 'prepare_cancel_bank_mandate' in names
    for name in names:assert protocol.DESCRIPTIONS.get(name) or registry.DESCRIPTIONS.get(name)


@pytest.mark.parametrize('name',['read_tool_catalog','read_wealth_products','read_risk_assessment','read_investment_performance','read_birthday_offers'])
def test_new_queries_are_executable_and_owner_scoped(dynamic,name):
    db,_=dynamic;args=registry.TOOL_MODELS[name]().model_dump()
    result=registry.execute(db,session_id='s1',owner_id='u1',name=name,arguments=args)
    assert isinstance(result,dict)
    with pytest.raises(DomainError):registry.execute(db,session_id='s1',owner_id='u2',name=name,arguments=args)


@pytest.mark.parametrize('name,args,text,kind,level',[
 ('prepare_card_loss',{},'挂失这张卡','loss','red'),
 ('prepare_online_payment_block',{},'关闭这张卡的线上支付','online_block','yellow'),
 ('prepare_online_payment_unblock',{},'恢复这张卡的线上支付','online_unblock','red'),
 ('prepare_card_limit_change',{'amount_yuan':'500'},'把这张卡单笔交易限额改成500元','limit','yellow'),
])
def test_dedicated_card_actions_prepare_the_correct_fixed_order(dynamic,monkeypatch,name,args,text,kind,level):
    db,_=dynamic;card=db.scalar(select(Card).where(Card.owner_id=='u1',Card.status=='active'))
    if kind=='online_unblock':
        control=db.get(CardControl,card.id)
        if control:control.online_enabled=0
        else:db.add(CardControl(card_id=card.id,limit_minor=100000,online_enabled=0,lost=0,version=1))
        db.commit()
    before=security.digest(dict(card=card.status,controls=__import__('app.domain.card_management',fromlist=['controls']).controls(db,card)))
    sequence(monkeypatch,calls((name,{'card':card.card_last4,**args})))
    result=create(db,session(db),'live',text+'，尾号'+card.card_last4)
    assert result['card_request']['kind']==kind
    assert result['authorization_operations'][0]['level']==level
    assert result['card_request']['status']=='pending_confirmation'
    assert security.digest(dict(card=card.status,controls=__import__('app.domain.card_management',fromlist=['controls']).controls(db,card)))==before


def test_loss_requires_red_verification_and_receipt_is_not_replayed(dynamic,client,monkeypatch):
    db,_=dynamic;client.cookies.set('finagent_demo_session','s1');card=db.scalar(select(Card).where(Card.owner_id=='u1',Card.status=='active'))
    sequence(monkeypatch,calls(('prepare_card_loss',{'card':card.card_last4})),reply('已取得业务回执，结果见固定办理记录。'))
    result=create(db,session(db),'live','挂失尾号'+card.card_last4+'的银行卡');a=result['authorization_operations'][0];path=a['authorization_path']
    token=client.post(path+'/confirmation-token',headers=headers(),json={'action_id':a['action_id']}).json()['data']['confirmation_token']
    rejected=client.post(path+'/confirm',headers=headers(),json={'action_id':a['action_id'],'confirmation_token':token})
    assert rejected.status_code!=200 and db.get(CardControl,card.id).lost==0
    mfa(db,a['resource_id'],kind='card')
    token=client.post(path+'/confirmation-token',headers=headers(),json={'action_id':a['action_id']}).json()['data']['confirmation_token']
    payload={'action_id':a['action_id'],'confirmation_token':token}
    assert client.post(path+'/confirm',headers=headers(),json=payload).status_code==200
    db.expire_all();assert db.get(CardControl,card.id).lost==1
    assert client.post(path+'/confirm',headers=headers(),json=payload).status_code==200
    assert db.scalar(select(func.count()).select_from(CardRequest))==1


def test_alias_cannot_override_fixed_action():
    with pytest.raises(ValueError):protocol.models()['prepare_card_loss'].model_validate({'card':'1234','action':'online_unblock'})
    name,args=protocol.operation_values('prepare_card_loss',{'card':'1234'})
    assert name=='prepare_card_management' and args['action']=='loss'


def test_alias_and_generic_action_share_replay_identity():
    name,args=protocol.operation_values('prepare_card_loss',{'card':'1234'})
    base,original=protocol.operation_values('prepare_card_management',{'card':'1234','action':'loss'})
    assert name==base and args==original


def test_cancel_bank_mandate_has_separate_read_and_write_confirmations(dynamic,client,monkeypatch):
    db,_=dynamic;client.cookies.set('finagent_demo_session','s1')
    subscriptions.seed_test_authorizations(db,owner_id='u1',account_id='a1c');db.commit()
    seen=sequence(monkeypatch,calls(('prepare_cancel_bank_mandate',{'merchant':'云音乐'})),reply('办理回执已保存；商户会员合同仍需另行处理。'))
    result=create(db,session(db),'live','解除云音乐的银行代扣')
    assert result['next_actions'] and not result['subscription_request'] and len(seen)==1
    auth=db.scalar(select(SubscriptionAuthorization).where(SubscriptionAuthorization.merchant=='云音乐'));assert auth.status=='active'
    pending=result['next_actions'][0]['pending_id']
    response=client.post('/api/tasks/'+result['id']+'/subscription-query-confirmation',headers=headers(),json={'pending_id':pending})
    assert response.status_code==200,response.text
    result=response.json()['data'];a=result['authorization_operations'][0];path=a['authorization_path']
    assert a['level']=='yellow' and result['subscription_request']['kind']=='revoke' and auth.status=='active'
    token=client.post(path+'/confirmation-token',headers=headers(),json={'action_id':a['action_id']}).json()['data']['confirmation_token']
    payload={'action_id':a['action_id'],'confirmation_token':token}
    assert client.post(path+'/confirm',headers=headers(),json=payload).status_code==200
    db.expire_all();assert db.get(SubscriptionAuthorization,auth.id).status=='revoked'
    assert client.post(path+'/confirm',headers=headers(),json=payload).status_code==200
    assert len(seen)==2 and db.scalar(select(func.count()).select_from(SubscriptionRequest))==1


def test_catalog_is_available_to_model_as_structured_result(dynamic,monkeypatch):
    db,_=dynamic
    def see_catalog(messages,context):
        data=json.loads(messages[-1]['content'])['data'];assert 'prepare_card_loss' in data['groups']['卡片管理']
        return reply('可以准备挂失和解除银行代扣，办理需要你手动确认。')
    sequence(monkeypatch,calls(('read_tool_catalog',{})),see_catalog)
    result=create(db,session(db),'live','你能挂失和解除订阅代扣吗')
    assert result['status']=='completed' and not result['authorization_operations']


def test_demo_merchant_suffix_does_not_block_actual_bank_mandate(dynamic,monkeypatch):
    db,_=dynamic
    subscriptions.seed_test_authorizations(db,owner_id='u1',account_id='a1c');db.commit()
    auth=db.scalar(select(SubscriptionAuthorization).where(SubscriptionAuthorization.merchant=='云音乐'));auth.merchant='云音乐（虚构）';db.commit()
    sequence(monkeypatch,calls(('prepare_cancel_bank_mandate',{'merchant':'云音乐'})))
    result=create(db,session(db),'live','解除云音乐（虚构）的银行代扣')
    pending=result['next_actions'][0]['pending_id']
    result=confirm_subscription_query(db,session(db),result['id'],pending)
    assert result['subscription_request']['kind']=='revoke'
    assert result['subscription_request']['summary']['merchant']=='云音乐（虚构）' and auth.status=='active'


def test_demo_suffix_normalization_never_matches_different_merchant(dynamic,monkeypatch):
    db,_=dynamic;subscriptions.seed_test_authorizations(db,owner_id='u1',account_id='a1c');db.commit()
    sequence(monkeypatch,calls(('prepare_cancel_bank_mandate',{'merchant':'完全不同的商户'})))
    result=create(db,session(db),'live','解除云音乐的银行代扣');pending=result['next_actions'][0]['pending_id']
    result=confirm_subscription_query(db,session(db),result['id'],pending)
    assert not result['subscription_request'] and not result['authorization_operations']


@pytest.mark.parametrize('tool,action,level',[('prepare_card_freeze','freeze','yellow'),('prepare_card_unfreeze','unfreeze','red')])
def test_freeze_and_unfreeze_use_dedicated_tools_and_original_proposals(dynamic,monkeypatch,tool,action,level):
    db,_=dynamic;card=db.scalar(select(Card).where(Card.owner_id=='u1',Card.status=='active'))
    if action=='unfreeze':card.status='frozen';card.version+=1;db.commit()
    sequence(monkeypatch,calls((tool,{'card':card.card_last4})))
    result=create(db,session(db),'live',('冻结' if action=='freeze' else '解冻')+'尾号'+card.card_last4+'的卡')
    assert result['proposal']['summary']['action']==action and result['proposal']['status']=='pending_confirmation'
    assert result['authorization_operations'][0]['level']==level


def test_virtual_card_application_alias_preserves_applicant_and_account(dynamic,monkeypatch):
    db,_=dynamic
    sequence(monkeypatch,calls(('prepare_virtual_card_application',{'source':'活期账户','applicant':'本人'})))
    result=create(db,session(db),'live','申请虚拟借记卡，申请人是本人，关联活期账户')
    assert result['card_request']['kind']=='apply' and result['card_request']['status']=='pending_confirmation'
    assert result['authorization_operations'][0]['level']=='yellow'


def test_generic_card_tool_schemas_do_not_advertise_wrong_action_types():
    with pytest.raises(ValueError):protocol.models()['prepare_card_action'].model_validate({'action':'loss'})
    with pytest.raises(ValueError):protocol.models()['prepare_card_management'].model_validate({'action':'freeze'})


def test_tool_answers_normalize_literal_newlines():
    r={'facts':[{'id':'f1','node':'s1','key':'unrealized_pnl_minor','label':'浮动盈亏','value':'127.17','unit':'yuan','source_ref':'ok'}],
        'evidence':[{'source_ref':'ok','tool':'read_investment_performance'}]}
    text,_=runtime.render_response(protocol.Respond(text='浮动盈亏{{f1}}\\n\\n这是快照。',evidence_refs=['ok']),r)
    assert '\n\n' in text and '\\n' not in text
