"""UI-only authorization with real domain execution on isolated ledgers."""
from datetime import timedelta
import json
from uuid import uuid4

import pytest
from sqlalchemy import select,func

from app.agent import authorization,language
from app.agent.service import create,send,view
from app.data.models import AgentTask,DemoSession,Account,TransferOrder,User
from app.data.security_models import SecurityState,SecurityAuthorization
from app.data.wealth_models import RiskAssessment,WealthOrder
from app.data.subscription_models import SubscriptionAuthorization
from app.domain import security,wealth,card_management as cards,subscriptions,birthdays,scheduled_transfers,collections
from app.domain import operations as proposals
from app.domain.errors import DomainError
from test_security import mfa
from test_unified_dialogue import turn,sequence
from test_http import client


def task(db,intent='transfer',sid='s1'):
    session=db.get(DemoSession,sid)
    row=AgentTask(id=uuid4().hex,owner_id=session.owner_id,session_id=sid,dataset_version=session.dataset_version,
        mode='mock',status='gathering_info',intent=intent,created_at_utc=security.now().isoformat(),updated_at_utc=security.now().isoformat())
    db.add(row);db.commit();return row


def prepare(db,kind='proposal',amount=10000,sid='s1'):
    intent={'proposal':'transfer','wealth':'wealth_buy','card':'card_management','subscription':'subscription_revoke',
        'birthday':'birthday_create','schedule':'schedule_transfer','collection':'aa_collection'}[kind]
    row=task(db,intent,sid);kwargs={'session_id':sid,'agent_task_id':row.id}
    if kind=='proposal':
        record=proposals.prepare_transfer(db,source_account_id='a1c',target={'type':'own_account','account_id':'a1s'},amount_minor=amount,**kwargs)['proposal']
        row.proposal_id=record['id']
    elif kind=='wealth':
        wealth.submit_assessment(db,session_id=sid,answers=[2,2,2,2])
        record=wealth.prepare_order(db,kind='buy',product_id='P1',account_id='a1c',amount_minor=10000,**kwargs)
    elif kind=='card':
        record=cards.prepare(db,kind='apply',account_id='a1c',applicant=db.get(User,'u1').display_name,**kwargs)
    elif kind=='subscription':
        subscriptions.seed_test_authorizations(db,owner_id='u1',account_id='a1c');db.commit()
        auth=db.scalar(select(SubscriptionAuthorization).where(SubscriptionAuthorization.merchant=='云音乐'))
        record=subscriptions.prepare(db,kind='revoke',authorization_id=auth.id,**kwargs)
    elif kind=='birthday':
        day=(birthdays.birthday_now(db)+timedelta(days=10)).date().isoformat()
        record=birthdays.prepare(db,kind='create',terms={'birthday_date':day,'source_account_id':'a1c','budget_minor':100000,
            'recipient':'合成收件人','address':'合成测试地址','item_keys':['flowers','cake']},**kwargs)
    elif kind=='schedule':
        record=scheduled_transfers.prepare_schedule(db,source_account_id='a1c',target={'type':'own_account','account_id':'a1s'},
            amount_minor=10000,execute_at_utc=(security.now()+timedelta(hours=2)).isoformat(),**kwargs)['schedule']
    else:
        record=collections.prepare_collection(db,destination_account_id='a1c',total_minor=30000,recipient_ids=['r1','r2'],**kwargs)
    if kind!='proposal':
        ctx=json.loads(row.context_json);ctx[authorization.RESOURCES[kind][1]]=record['id'];row.context_json=json.dumps(ctx)
    row.status='awaiting_confirmation';db.commit()
    return row,next(o for o in view(db,row)['authorization_operations'] if o['resource_kind']==kind)


@pytest.mark.parametrize('kind',['proposal','wealth','card','subscription','birthday','schedule','collection'])
def test_all_supported_operations_can_authorize_from_task_and_replay_receipt(bank,kind):
    db,_=bank;row,action=prepare(db,kind);session=db.get(DemoSession,'s1')
    assert action['can_confirm'] and len(action['action_id'])==64
    if action['level']=='red':mfa(db,action['resource_id'],kind=kind)
    token=authorization.issue_token(db,session,row.id,kind,action['resource_id'],action['action_id'])['confirmation_token']
    result=authorization.confirm(db,session,row.id,kind,action['resource_id'],action['action_id'],token)
    status=result['authorization_operations'][0]['status']
    assert status in {'executed','settled','submitted','scheduled','collecting'}
    assert not result['authorization_operations'][0]['can_confirm']
    count=len(result['messages'])
    again=authorization.confirm(db,session,row.id,kind,action['resource_id'],action['action_id'],token)
    assert len(again['messages'])==count
    if kind=='card':assert '等待银行审批' in result['messages'][-1]['content']
    if kind=='schedule':assert '尚未转账' in result['messages'][-1]['content']


@pytest.mark.parametrize('kind',['proposal','wealth','card','subscription','birthday','schedule','collection'])
def test_cancel_each_operation_keeps_no_payment_and_no_authority(bank,kind):
    db,_=bank;row,action=prepare(db,kind);session=db.get(DemoSession,'s1')
    before=list(db.execute(select(Account.id,Account.balance_minor).order_by(Account.id)))
    result=authorization.cancel(db,session,row.id,kind,action['resource_id'],action['action_id'])
    assert result['authorization_operations'][0]['status'] in {'cancelled','expired'}
    assert before==list(db.execute(select(Account.id,Account.balance_minor).order_by(Account.id)))
    assert not db.scalar(select(func.count()).select_from(TransferOrder))


def test_modified_version_foreign_session_and_paused_task_cannot_authorize(bank):
    from app.agent.control import change
    db,_=bank;row,a=prepare(db);session=db.get(DemoSession,'s1')
    with pytest.raises(DomainError):authorization.issue_token(db,db.get(DemoSession,'s2'),row.id,'proposal',a['resource_id'],a['action_id'])
    row.plan_version+=1;db.commit()
    with pytest.raises(DomainError,match='变化'):authorization.issue_token(db,session,row.id,'proposal',a['resource_id'],a['action_id'])
    a=view(db,row)['authorization_operations'][0];change(db,row,'pause')
    assert not view(db,row)['authorization_operations'][0]['can_confirm']
    with pytest.raises(DomainError,match='暂停'):authorization.issue_token(db,session,row.id,'proposal',a['resource_id'],a['action_id'])
    authorization.cancel(db,session,row.id,'proposal',a['resource_id'],a['action_id'])


def test_red_proof_and_failed_token_counters_are_not_bypassed_by_task_route(bank):
    db,_=bank;row,a=prepare(db,amount=200000);session=db.get(DemoSession,'s1')
    token=authorization.issue_token(db,session,row.id,'proposal',a['resource_id'],a['action_id'])['confirmation_token']
    with pytest.raises(DomainError) as error:authorization.confirm(db,session,row.id,'proposal',a['resource_id'],a['action_id'],token)
    assert error.value.code=='MFA_REQUIRED'
    mfa(db,a['resource_id'])
    with pytest.raises(DomainError):authorization.confirm(db,session,row.id,'proposal',a['resource_id'],a['action_id'],'invalid-token')
    assert db.get(SecurityState,'u1').failures==1
    assert not db.scalar(select(func.count()).select_from(TransferOrder))


def test_assessment_in_chat_continues_preparation_but_requires_separate_trade_confirmation(bank,monkeypatch):
    db,_=bank;session=db.get(DemoSession,'s1')
    sequence(monkeypatch,turn('wealth_buy',source='活期账户',product='P1',amount_yuan='100'))
    state=create(db,session,'live','从活期账户申购100元P1')
    pending=state['prerequisites'][0]['pending_id'];assert not state['wealth_order']
    before=db.get(Account,'a1c').balance_minor
    result=authorization.submit_assessment(db,session,state['id'],pending,[0,0,0,0])
    assert result['wealth_order']['status']=='pending_confirmation' and not result['prerequisites']
    assert before==db.get(Account,'a1c').balance_minor
    count=db.scalar(select(func.count()).select_from(WealthOrder))
    authorization.submit_assessment(db,session,state['id'],pending,[2,2,2,2])
    assert db.scalar(select(func.count()).select_from(WealthOrder))==count


def test_assessment_cannot_bypass_product_risk_and_new_message_invalidates_questionnaire(bank,monkeypatch):
    db,_=bank;session=db.get(DemoSession,'s1')
    sequence(monkeypatch,turn('wealth_buy',source='活期账户',product='P3',amount_yuan='100'),turn(reply='你好。'))
    state=create(db,session,'live','从活期账户申购100元P3');pending=state['prerequisites'][0]['pending_id']
    result=authorization.submit_assessment(db,session,state['id'],pending,[0,0,0,0])
    assert not result['wealth_order'] and '风险等级' in result['messages'][-1]['content']
    row=db.get(AgentTask,state['id']);authorization.stage_assessment(row,{'kind':'assessment'});db.commit()
    pending=json.loads(row.context_json)['pending_risk_assessment']['id']
    send(db,session,row.id,'你好')
    with pytest.raises(DomainError):authorization.submit_assessment(db,session,row.id,pending,[2,2,2,2])


def test_subscription_access_continues_original_revocation_without_model_or_execution(bank,monkeypatch):
    from app.agent.conversation_reads import confirm_subscription_query
    db,_=bank;session=db.get(DemoSession,'s1')
    subscriptions.seed_test_authorizations(db,owner_id='u1',account_id='a1c');db.commit()
    seen=sequence(monkeypatch,turn('subscription_revoke',merchant='云音乐'))
    state=create(db,session,'live','取消云音乐的银行代扣')
    assert state['next_actions'] and not state['subscription_request']
    result=confirm_subscription_query(db,session,state['id'],state['next_actions'][0]['pending_id'])
    assert len(seen)==1 and not result['next_actions']
    assert result['subscription_request']['status']=='pending_confirmation'
    assert db.scalar(select(SubscriptionAuthorization).where(SubscriptionAuthorization.merchant=='云音乐')).status=='active'
    assert all(grant.resource.startswith('subscription-query:') for grant in db.scalars(select(SecurityAuthorization)))


def test_http_confirmation_requires_csrf_and_task_bound_action(client,bank):
    db,_=bank;row,a=prepare(db);client.cookies.set('finagent_demo_session','s1')
    route=a['authorization_path'];body={'action_id':a['action_id']}
    headers={'Origin':'http://testserver','X-FinAgent-CSRF':'test-csrf'}
    assert client.post(route+'/confirmation-token',json=body).status_code==403
    assert client.post(route+'/confirmation-token',headers=headers,json={**body,'authorized':True}).status_code==422
    issued=client.post(route+'/confirmation-token',headers=headers,json=body);assert issued.status_code==200,issued.text
    response=client.post(route+'/confirm',headers=headers,json={**body,'confirmation_token':issued.json()['data']['confirmation_token']})
    assert response.status_code==200,response.text
    assert response.json()['data']['authorization_operations'][0]['status']=='executed'
    client.cookies.set('finagent_demo_session','other')
    assert client.get(route).status_code==404


def test_expired_assessment_during_confirmation_recovers_to_chat_questionnaire(bank):
    db,_=bank;row,a=prepare(db,'wealth');session=db.get(DemoSession,'s1')
    db.scalar(select(RiskAssessment).where(RiskAssessment.owner_id=='u1')).expires_at_utc=(security.now()-timedelta(seconds=1)).isoformat();db.commit()
    mfa(db,a['resource_id'],kind='wealth')
    token=authorization.issue_token(db,session,row.id,'wealth',a['resource_id'],a['action_id'])['confirmation_token']
    with pytest.raises(DomainError) as failure:authorization.confirm(db,session,row.id,'wealth',a['resource_id'],a['action_id'],token)
    assert failure.value.code=='RISK_ASSESSMENT_REQUIRED'
    state=view(db,row);assert state['prerequisites'] and not state['authorization_operations'][0]['can_confirm']
    resumed=authorization.submit_assessment(db,session,row.id,state['prerequisites'][0]['pending_id'],[0,0,0,0])
    assert resumed['wealth_order']['id']!=a['resource_id']
    assert resumed['steps'][-1]['status']=='awaiting_confirmation' and resumed['steps'][-1]['result_ref']==resumed['wealth_order']['id']


def test_task_verification_http_counts_failures_and_locks_without_sending_code_in_response(client,bank):
    import app.config
    db,_=bank;row,a=prepare(db,amount=200000);client.cookies.set('finagent_demo_session','s1')
    route=a['authorization_path'];body={'action_id':a['action_id']}
    headers={'Origin':'http://testserver','X-FinAgent-CSRF':'test-csrf'}
    response=client.post(route+'/challenge',headers=headers,json=body);assert response.status_code==200
    challenge=response.json()['data'];assert 'code' not in challenge
    delivered=json.loads((app.config.ROOT/'var/auth-simulator'/(challenge['challenge_id']+'.json')).read_text(encoding='utf8'))
    wrong=('1' if delivered['code'][0]=='0' else '0')+delivered['code'][1:]
    for _ in range(5):
        assert client.post(route+'/verify',headers=headers,json={**body,'challenge_id':challenge['challenge_id'],'code':wrong}).status_code==403
    db.expire_all();assert db.get(SecurityState,'u1').failures==5
    assert not view(db,row)['authorization_operations'][0]['can_confirm']
    assert not db.scalar(select(func.count()).select_from(TransferOrder))
