import json
import pytest
from sqlalchemy import select,func
from app.agent.service import create,send
from app.agent.forms import supplement_transfer,payment_account
from app.agent import authorization
from app.data.models import AgentTask,Account,DemoSession,Proposal,TransferOrder
from app.domain.errors import DomainError
from test_unified_dialogue import turn,sequence
from test_http import client

DETAILS={'name':'小林','bank_name':'测试银行','account_number':'2131244534'}

def start(db,monkeypatch):
    seen=sequence(monkeypatch,turn('transfer',target='小林',amount_yuan='200'))
    return create(db,db.get(DemoSession,'s1'),'live','给小林转200元'),seen

def test_transfer_form_selects_payer_preserves_amount_and_confirms_once(bank,monkeypatch):
    db,_=bank;state,seen=start(db,monkeypatch)
    assert state['transfer_details']['amount_yuan']=='200.00' and state['transfer_details']['name']=='小林'
    assert not state['transfer_details']['source_reference']
    state=supplement_transfer(db,db.get(DemoSession,'s1'),state['id'],{**DETAILS,'source_reference':'a1c','amount_yuan':'200.00'})
    assert len(seen)==1 and state['status']=='awaiting_confirmation'
    fixed=json.loads(db.get(Proposal,state['proposal']['id']).params_json)
    assert fixed['source_account_id']=='a1c' and fixed['amount_minor']==20000 and fixed['target']['account_number']==DETAILS['account_number']
    assert not db.scalar(select(func.count()).select_from(TransferOrder))
    a=state['authorization_operations'][0];session=db.get(DemoSession,'s1')
    token=authorization.issue_token(db,session,state['id'],'proposal',a['resource_id'],a['action_id'])['confirmation_token']
    confirmed=authorization.confirm(db,session,state['id'],'proposal',a['resource_id'],a['action_id'],token)
    assert confirmed['proposal']['status']=='executed' and db.scalar(select(func.count()).select_from(TransferOrder))==1
    with pytest.raises(DomainError):supplement_transfer(db,session,state['id'],{**DETAILS,'source_reference':'a1c'})

def test_legacy_form_then_bare_account_label_completes_existing_transfer(bank,monkeypatch):
    db,_=bank;session=db.get(DemoSession,'s1');state,_=start(db,monkeypatch)
    state=supplement_transfer(db,session,state['id'],DETAILS)
    assert state['status']=='gathering_info' and state['transfer_details']['amount_yuan']=='200.00'
    account=db.get(Account,'a1c');text=f'{account.alias} 尾号{account.account_last4}'
    sequence(monkeypatch,turn('transfer','supplement',source=text))
    state=send(db,session,state['id'],text)
    assert state['proposal']['summary']['amount_minor']==20000
    assert json.loads(db.get(Proposal,state['proposal']['id']).params_json)['source_account_id']=='a1c'

def test_structured_submission_retains_existing_grounded_slots(bank,monkeypatch):
    db,_=bank;state,_=start(db,monkeypatch);row=db.get(AgentTask,state['id']);ctx=json.loads(row.context_json)
    ctx.pop('source_account_id',None);ctx.pop('amount_minor',None)
    ctx.update(dialogue_intent='transfer',dialogue_slots={'source':'活期账户','amount_yuan':'200'})
    row.context_json=json.dumps(ctx,ensure_ascii=False);db.commit()
    state=supplement_transfer(db,db.get(DemoSession,'s1'),row.id,DETAILS)
    assert state['status']=='awaiting_confirmation' and state['proposal']['summary']['amount_minor']==20000

@pytest.mark.parametrize('reference',['a2c','不存在的账户','活期账户 尾号0000'])
def test_foreign_unknown_or_conflicting_payer_does_not_prepare_or_lose_amount(bank,monkeypatch,reference):
    db,_=bank;state,_=start(db,monkeypatch)
    with pytest.raises(DomainError):supplement_transfer(db,db.get(DemoSession,'s1'),state['id'],{**DETAILS,'source_reference':reference})
    assert json.loads(db.get(AgentTask,state['id']).context_json)['amount_minor']==20000
    assert not db.scalar(select(func.count()).select_from(Proposal))

@pytest.mark.parametrize('untrusted_history',[False,True])
def test_generic_transfer_can_be_completed_entirely_by_form(bank,monkeypatch,untrusted_history):
    db,_=bank;sequence(monkeypatch,turn('transfer',target='小林',amount_yuan='200') if untrusted_history else turn('transfer'))
    state=create(db,db.get(DemoSession,'s1'),'live','转账')
    assert state['transfer_details'] is not None
    assert not state['transfer_details']['amount_yuan']
    state=supplement_transfer(db,db.get(DemoSession,'s1'),state['id'],{**DETAILS,'source_reference':'a1c','amount_yuan':'200'})
    assert state['proposal']['summary']['amount_minor']==20000

def test_account_reference_uses_both_alias_and_tail(bank):
    db,_=bank;a=db.get(Account,'a1c')
    assert payment_account(db,'u1',f'{a.alias} 尾号{a.account_last4}').id==a.id
    a.status='closed';db.commit()
    assert payment_account(db,'u1',a.id) is None

def test_transfer_details_http_csrf_and_strict_payload(client,bank,monkeypatch):
    db,_=bank;state,_=start(db,monkeypatch);client.cookies.set('finagent_demo_session','s1')
    route='/api/tasks/'+state['id']+'/transfer-details';payload={**DETAILS,'source_reference':'a1c','amount_yuan':'200'}
    headers={'Origin':'http://testserver','X-FinAgent-CSRF':'test-csrf'}
    assert client.post(route,json=payload).status_code==403
    assert client.post(route,headers=headers,json={**payload,'authorized':True}).status_code==422
    result=client.post(route,headers=headers,json=payload)
    assert result.status_code==200 and result.json()['data']['proposal']['summary']['amount_minor']==20000
