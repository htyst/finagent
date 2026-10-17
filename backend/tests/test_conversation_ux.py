import json
import pytest
from sqlalchemy import select,func
from app.agent import dialogue,knowledge,language
from app.agent.service import create,send
from app.agent.dialogue_schema import DialogueTurn,ReadRequest
from app.agent.conversation_reads import confirm_subscription_query
from app.data.models import DemoSession,AgentTask,Account,TransferOrder,AgentMessage
from app.data.security_models import SecurityAuthorization
from app.domain.errors import DomainError
from test_unified_dialogue import turn,sequence
from test_financial_import import imported,login

@pytest.mark.parametrize('text',['理财建议','给我一些理财方面的建议','帮我一点投资理财建议'])
def test_guidance_cannot_become_missing_payment_fields_or_a_write(bank,monkeypatch,text):
    db,_=bank
    sequence(monkeypatch,
        turn('clarify',intent_candidates=['wealth_buy'],missing_fields=['product','source','amount_yuan']),
        turn('wealth_buy',product='P1',source='活期账户',amount_yuan='1000'))
    state=create(db,db.get(DemoSession,'s1'),'live',text)
    answer=state['messages'][-1]['content']
    assert state['intent']=='wealth_guidance' and state['status']=='completed'
    assert '风险' in answer and '付款账户' not in answer and '请明确产品' not in answer
    assert state['proposal'] is None and state.get('wealth_order') is None
    assert [s['tool_name'] for s in state['steps']]==['search_knowledge']


def test_guidance_during_transfer_clarification_preserves_but_does_not_advance_draft(bank,monkeypatch):
    db,_=bank;session=db.get(DemoSession,'s1')
    sequence(monkeypatch,turn('transfer',target='储蓄账户',amount_yuan='200'),
        turn('clarify',intent_candidates=['wealth_buy'],missing_fields=['product','source','amount_yuan']),
        turn('wealth_guidance','knowledge'))
    state=create(db,session,'live','给储蓄账户转200元')
    before=json.loads(db.get(AgentTask,state['id']).context_json)['dialogue_slots']
    state=send(db,session,state['id'],'理财建议')
    answer=state['messages'][-1]['content']
    assert '应急' in answer and '请明确' not in answer
    assert state['proposal'] is None and state.get('wealth_order') is None
    assert json.loads(db.get(AgentTask,state['id']).context_json)['dialogue_slots']==before
    assert not db.scalar(select(func.count()).select_from(TransferOrder))


def test_consultation_does_not_replace_explicit_or_mixed_purchase():
    from app.agent.consultation import normalize,general_guidance
    for text in ['从活期账户申购1000元P1','理财建议，并申购1000元P1','给小林转200元','比较P1和P2']:
        assert not general_guidance(text)
        decision=turn('wealth_buy')
        assert normalize(decision,text) is decision


def test_guidance_rejects_payment_template_and_numbers_from_metadata(bank,monkeypatch):
    db,_=bank
    sequence(monkeypatch,
        turn('reply',reply='请明确产品编号、付款账户、转账金额。'),
        turn('wealth_guidance','knowledge',reply='留6个月的钱。',evidence_refs=['kb:financial-guidance']))
    state=create(db,db.get(DemoSession,'s1'),'live','理财建议')
    answer=state['messages'][-1]['content']
    assert '6个月' not in answer and '付款账户' not in answer and '资金用途' in answer

@pytest.mark.parametrize('query,expected',[('介绍一下你自己','introduction'),('给我一些理财方面的建议','financial-guidance'),('理财建议','financial-guidance')])
def test_public_knowledge_is_retrievable_without_account_data(query,expected):
    result=knowledge.search(query)
    assert result['items'][0]['id']==expected
    assert all('balance_minor' not in d for d in result['items'])

def test_general_financial_guidance_survives_wealth_facts(bank,monkeypatch):
    db,_=bank
    sequence(monkeypatch,turn('reply','knowledge',tools=[ReadRequest(name='read_wealth')]),turn('reply','knowledge'))
    state=create(db,db.get(DemoSession,'s1'),'live','理财建议')
    answer=state['messages'][-1]['content']
    assert '风险' in answer and '预计多久' in answer and '没有足够' not in answer
    assert {'search_knowledge','read_wealth'}<={s['tool_name'] for s in state['steps']}
    assert state['proposal'] is None

def test_subscription_query_inline_confirmation_is_explicit_idempotent_and_not_background_authority(bank,monkeypatch):
    db,_=bank;session=db.get(DemoSession,'s1')
    sequence(monkeypatch,turn('subscription_inspect',tools=[ReadRequest(name='read_subscriptions')]))
    state=create(db,session,'live','管理订阅代扣')
    action=state['next_actions'][0];assert action['kind']=='confirm_subscription_query'
    assert not db.scalar(select(SecurityAuthorization).where(SecurityAuthorization.resource=='subscription-query:s1'))
    state=confirm_subscription_query(db,session,state['id'],action['pending_id'])
    assert not state['next_actions'] and '已确认查询范围' in state['messages'][-1]['content']
    assert state['steps'][-1]['status']=='succeeded'
    count=len(state['messages']);again=confirm_subscription_query(db,session,state['id'],action['pending_id']);assert len(again['messages'])==count
    grants=list(db.scalars(select(SecurityAuthorization)))
    assert all(g.resource.startswith('subscription-query:') for g in grants)
    assert not db.scalar(select(func.count()).select_from(TransferOrder))

def test_changed_question_invalidates_old_subscription_confirmation(bank,monkeypatch):
    db,_=bank;session=db.get(DemoSession,'s1')
    sequence(monkeypatch,turn('subscription_inspect',tools=[ReadRequest(name='read_subscriptions')]),turn(reply='你好，我是FinAgent。'))
    state=create(db,session,'live','查询订阅扣费');action=state['next_actions'][0]
    state=send(db,session,state['id'],'你好')
    assert not state['next_actions']
    with pytest.raises(DomainError,match='查询问题已变化'):confirm_subscription_query(db,session,state['id'],action['pending_id'])
    assert not db.scalar(select(SecurityAuthorization).where(SecurityAuthorization.resource=='subscription-query:s1'))

def test_other_session_cannot_confirm_subscription_query(bank,monkeypatch):
    db,_=bank;sequence(monkeypatch,turn('subscription_inspect',tools=[ReadRequest(name='read_subscriptions')]))
    state=create(db,db.get(DemoSession,'s1'),'live','查看订阅')
    with pytest.raises(DomainError):confirm_subscription_query(db,db.get(DemoSession,'other'),state['id'],state['next_actions'][0]['pending_id'])
    assert not db.scalar(select(SecurityAuthorization))

@pytest.mark.parametrize('code',['MODEL_BUDGET_EXCEEDED','MODEL_UNAVAILABLE','MODEL_RATE_LIMITED'])
def test_optional_synthesis_failure_retains_calculated_facts(bank,monkeypatch,code):
    db,_=bank
    def fail(payload):raise DomainError(code,'补充分析失败',503)
    sequence(monkeypatch,turn('spending',tools=[ReadRequest(name='summarize_spending')]),fail)
    state=create(db,db.get(DemoSession,'s1'),'live','分析本月账单')
    assert state['status']=='partially_completed' and any(s['status']=='succeeded' for s in state['steps'])
    answer='\n'.join(m['content'] for m in state['messages'] if m['role']=='assistant')
    assert '净消费' in answer and '已保留' in answer and '主要花在' in answer
    assert state['proposal'] is None

def test_bill_answer_is_concise_by_default_and_details_remain_in_steps(bank,monkeypatch):
    db,_=bank;sequence(monkeypatch,turn('spending',tools=[ReadRequest(name='summarize_spending')]),turn('spending'))
    state=create(db,db.get(DemoSession,'s1'),'live','分析本月账单')
    assert len(state['messages'][-1]['content'])<650
    assert state['steps'][0]['result']['current']['categories']
    assert '展开报告' in state['messages'][-1]['content']

@pytest.mark.parametrize('merchant',[None,'不能悄悄丢掉的商户'])
def test_anomalies_after_spending_does_not_leak_incompatible_filters(bank,merchant):
    from app.agent import registry
    db,_=bank;task=AgentTask(id='read-chain',owner_id='u1',context_json=json.dumps({'last_query':{'args':{'merchant':merchant}}}))
    decision=turn('spending',follow_up=True)
    checked=[('inspect_anomalies',registry.validate('inspect_anomalies',{}))]
    if merchant:
        with pytest.raises(DomainError):dialogue.normalize_reads(db,task,'分析本月账单',decision,checked)
    else:
        output=dialogue.normalize_reads(db,task,'分析本月账单',decision,checked)
        assert 'merchant' not in output[0][1]

def test_query_confirmation_http_route_requires_csrf_and_cannot_grant_a_foreign_query(imported,monkeypatch):
    monkeypatch.setenv('FINAGENT_AGENT_ENGINE','legacy')
    client,_=imported;headers=login(client,'u1');monkeypatch.setenv('FINAGENT_MODE','live')
    sequence(monkeypatch,turn('subscription_inspect',tools=[ReadRequest(name='read_subscriptions')]))
    state=client.post('/api/tasks',headers=headers,json={'message':'管理订阅代扣'}).json()['data']
    action=state['next_actions'][0];route='/api/tasks/'+state['id']+'/subscription-query-confirmation';payload={'pending_id':action['pending_id']}
    assert client.post(route,json=payload).status_code==403
    resumed=client.post(route,headers=headers,json=payload);assert resumed.status_code==200
    result=resumed.json()['data'];assert not result['next_actions'] and '678.00' in result['messages'][-1]['content']
    count=len(result['messages']);assert len(client.post(route,headers=headers,json=payload).json()['data']['messages'])==count
    foreign_headers=login(client,'u2');assert client.post(route,headers=foreign_headers,json=payload).status_code==404
