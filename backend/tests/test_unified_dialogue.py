"""Deterministic model substitutes exercise the real Live/domain boundary.

These are not evidence of real-provider semantic accuracy or human adjudication.
"""
import json
from time import monotonic
import httpx
import pytest
from sqlalchemy import func, select
from app.agent import language, dialogue, model_client
from app.agent.dialogue_schema import DialogueTurn, Goal, ReadRequest, FieldSource
from app.agent.service import create, send, cancel
from app.agent.model_budget import budget
from app.data.models import AgentTask, DemoSession, Proposal, TransferOrder
from app.domain.errors import DomainError
from app.domain.operations import issue_confirmation_token, confirm_proposal, get_proposal


def turn(intent='reply', behavior='new_task', **kwargs):
    fields = {key: kwargs.pop(key) for key in list(kwargs) if key in DialogueTurn.model_fields}
    return language.Understanding(intent=intent, dialogue=DialogueTurn(behavior=behavior, **fields), **kwargs)


def sequence(monkeypatch, *values):
    seen = []
    iterator = iter(values)
    def interpret(payload):
        seen.append(payload)
        value = next(iterator)
        return value(payload) if callable(value) else value
    monkeypatch.setattr(language, 'interpret', interpret)
    return seen


def transfer(**kwargs):
    return turn('transfer', source='活期账户', target='储蓄账户', amount_yuan='100', **kwargs)


@pytest.mark.parametrize('text', ['从活期账户转100元到储蓄账户', '挂失尾号1234', '申请虚拟借记卡',
    '取消云音乐自动续费', '比较理财产品', '明天预约转账', '生日安排', '查询年度账单', '先别转，查一下余额'])
def test_every_normal_live_message_enters_semantics_before_lexical_business(bank, monkeypatch, text):
    db, _ = bank
    seen = sequence(monkeypatch, turn(reply='请说明你想了解的具体内容。'))
    result = create(db, db.get(DemoSession,'s1'), 'live', text)
    assert len(seen) == 1 and result['proposal'] is None and not result['steps']
    assert 'dialogue_context' in seen[0]
    assert not db.scalar(select(func.count()).select_from(TransferOrder))


def test_continuous_parameter_edits_invalidate_old_form_and_proof(bank, monkeypatch):
    db, _ = bank; session=db.get(DemoSession,'s1')
    sequence(monkeypatch, transfer(), turn('transfer','modify',amount_yuan='800'),
             turn('transfer','modify',amount_yuan='600'))
    state=create(db,session,'live','从活期账户转100元到储蓄账户')
    for text, amount in [('改成800',80000),('改成600',60000)]:
        old=state['proposal']['id']; proof=issue_confirmation_token(db,session_id='s1',proposal_id=old)['confirmation_token']
        state=send(db,session,state['id'],text)
        assert state['proposal']['summary']['amount_minor']==amount
        assert get_proposal(db,session_id='s1',proposal_id=old)['status']=='invalidated'
        with pytest.raises(DomainError): confirm_proposal(db,session_id='s1',proposal_id=old,confirmation_token=proof)
    memory=json.loads(db.get(AgentTask,state['id']).context_json)['dialogue_memory']
    assert memory['fields']['amount_yuan']['value']=='600'
    assert memory['fields']['amount_yuan']['explicit_selection'] is True
    assert memory['fields']['amount_yuan']['approved_fixed_form'] is False


def test_rejected_recipient_is_not_reused_and_can_choose_other_tail(bank,monkeypatch):
    db,_=bank; session=db.get(DemoSession,'s1')
    sequence(monkeypatch,turn('transfer',source='活期账户',target='张三 尾号3821',amount_yuan='100'),
        turn('transfer','modify',clear_fields=['target','target_note']),
        turn('transfer','supplement',target='9156',follow_up=True))
    state=create(db,session,'live','从活期账户转100元给张三，选尾号3821')
    old=state['proposal']['id']
    state=send(db,session,state['id'],'不是这个张三')
    assert state['proposal'] is None
    assert get_proposal(db,session_id='s1',proposal_id=old)['status']=='invalidated'
    assert 'target' not in json.loads(db.get(AgentTask,state['id']).context_json)['dialogue_slots']
    state=send(db,session,state['id'],'选尾号9156')
    assert state['proposal']['summary']['target']['id']=='r2'


def test_pause_query_revokes_before_second_model_call_and_keeps_draft(bank,monkeypatch):
    db,_=bank; session=db.get(DemoSession,'s1'); old=[]
    def explain(payload):
        assert get_proposal(db,session_id='s1',proposal_id=old[0])['status']=='invalidated'
        ref=payload['tool_results'][0]['source_ref']
        return turn('accounts','pause_query',reply='本次只查询，原转账保留为草稿。',evidence_refs=[ref])
    seen=sequence(monkeypatch,transfer(),turn('accounts','pause_query',tools=[ReadRequest(name='list_accounts')]),explain,
        turn('transfer','modify',amount_yuan='80'))
    state=create(db,session,'live','从活期账户转100元到储蓄账户'); old.append(state['proposal']['id'])
    state=send(db,session,state['id'],'先别转，查一下余额')
    assert state['proposal'] is None and state['status']=='gathering_info'
    assert '可用余额' in state['messages'][-1]['content']
    assert seen[-1]['dialogue_context']['drafts']['dialogue_slots']['amount_yuan']=='100'
    state=send(db,session,state['id'],'改成80元再准备')
    assert state['proposal']['summary']['amount_minor']==8000
    assert not db.scalar(select(func.count()).select_from(TransferOrder))


def test_multiple_goals_conditions_times_survive_model_tool_loop(bank,monkeypatch):
    db,_=bank; session=db.get(DemoSession,'s1')
    goals=[Goal(text='明天转100元给李文',kind='write'),Goal(text='再挂失尾号1234',kind='write')]
    sequence(monkeypatch,turn('transfer',goals=goals,conditions=['余额足够才'],time_requirements=['明天'],
        tools=[ReadRequest(name='list_accounts')]),
        turn('transfer',source='活期账户',target='李文',amount_yuan='100'))
    state=create(db,session,'live','余额足够才从活期账户明天转100元给李文，顺便再挂失尾号1234')
    assert len(state['requested_outcomes'])==2 and state['proposal'] is None
    memory=json.loads(db.get(AgentTask,state['id']).context_json)['dialogue_memory']
    assert memory['conditions']==['余额足够才'] and memory['time_requirements']==['明天']
    assert '逐' in state['messages'][-1]['content'] or '先办理' in state['messages'][-1]['content']


def test_old_receipt_query_never_creates_a_new_transfer(bank,monkeypatch):
    db,_=bank; session=db.get(DemoSession,'s1')
    sequence(monkeypatch,transfer(),turn('reply','result_query'))
    state=create(db,session,'live','从活期账户转100元到储蓄账户'); pid=state['proposal']['id']
    proof=issue_confirmation_token(db,session_id='s1',proposal_id=pid)['confirmation_token']
    receipt=confirm_proposal(db,session_id='s1',proposal_id=pid,confirmation_token=proof)
    result=create(db,session,'live','刚才那笔成功了吗')
    assert receipt['order_id'] in result['messages'][-1]['content']
    assert db.scalar(select(func.count()).select_from(TransferOrder))==1


def test_read_only_insert_query_does_not_lose_pending_transfer(bank,monkeypatch):
    db,_=bank; session=db.get(DemoSession,'s1')
    sequence(monkeypatch,transfer(),turn('accounts','insert_query'),turn('transfer','modify',amount_yuan='80'))
    state=create(db,session,'live','从活期账户转100元到储蓄账户'); old=state['proposal']['id']
    state=send(db,session,state['id'],'账户里还有多少')
    assert state['proposal']['id']==old and state['intent']=='transfer'
    state=send(db,session,state['id'],'改成80元')
    assert state['proposal']['summary']['amount_minor']==8000


def test_inferred_or_wrong_amount_cannot_revive_old_amount(bank,monkeypatch):
    db,_=bank; session=db.get(DemoSession,'s1')
    sequence(monkeypatch,transfer(),turn('transfer','modify',amount_yuan='900',field_sources=[
        FieldSource(field='amount_yuan',quote='改成800',origin='inferred')]))
    state=create(db,session,'live','从活期账户转100元到储蓄账户'); old=state['proposal']['id']
    state=send(db,session,state['id'],'改成800')
    assert state['proposal'] is None and '关键字段' in state['messages'][-1]['content']
    assert get_proposal(db,session_id='s1',proposal_id=old)['status']=='invalidated'


@pytest.mark.parametrize('name,args', [('confirm_proposal','{}'),('unknown','{}'),
    ('list_accounts','{"owner_id":"u2"}'),('list_transactions','{"limit":true}'),('list_accounts','[]')])
def test_invalid_tool_or_parameters_rejected_before_any_execution(bank,monkeypatch,name,args):
    db,_=bank
    invalid=turn(tools=[ReadRequest(name=name,arguments_json=args)])
    sequence(monkeypatch,invalid,invalid,invalid)
    state=create(db,db.get(DemoSession,'s1'),'live','帮我查询')
    assert not state['steps'] and not state['proposal']
    assert state['status']=='gathering_info'


def test_subscription_tool_cannot_bypass_yellow_confirmation(bank,monkeypatch):
    db,_=bank
    def explain(payload):
        e=payload['tool_results'][0]
        assert e['error']['code']=='SUBSCRIPTION_CONFIRMATION_REQUIRED' and e['data'] is None
        return turn('reply',reply='请先在订阅面板完成专项查询确认。',evidence_refs=[e['source_ref']])
    sequence(monkeypatch,turn(tools=[ReadRequest(name='read_subscriptions')]),explain)
    state=create(db,db.get(DemoSession,'s1'),'live','看看我的订阅')
    assert '确认' in state['messages'][-1]['content']
    assert not state['proposal']


def test_cross_user_and_other_session_task_refs_return_only_error(bank,monkeypatch):
    db,_=bank
    sequence(monkeypatch,turn(reply='你好。'),turn(tools=[ReadRequest(name='read_agent_status',arguments_json='{}')]))
    foreign=create(db,db.get(DemoSession,'other'),'live','你好')
    def first(payload): return turn(tools=[ReadRequest(name='read_agent_status',arguments_json=json.dumps({'task_id':foreign['id']}))])
    def second(payload):
        assert payload['tool_results'][0]['error']['code']=='RESOURCE_NOT_FOUND'
        assert payload['tool_results'][0]['data'] is None
        return turn('reply',reply='无法读取该任务。',evidence_refs=[payload['tool_results'][0]['source_ref']])
    sequence(monkeypatch,first,second)
    state=create(db,db.get(DemoSession,'s1'),'live','查另一用户任务')
    assert foreign['id'] not in state['messages'][-1]['content']


def test_tool_failure_is_evidence_and_does_not_produce_amount(bank,monkeypatch):
    db,_=bank
    from app.agent import registry
    execute=registry.execute
    def broken(*args,**kwargs):
        if kwargs.get('name')=='list_accounts':raise DomainError('READ_UNAVAILABLE','账户服务暂时不可用',503)
        return execute(*args,**kwargs)
    monkeypatch.setattr(registry,'execute',broken)
    sequence(monkeypatch,turn(tools=[ReadRequest(name='list_accounts')]),
        turn('reply',reply='余额是999999元。',evidence_refs=['invented']))
    state=create(db,db.get(DemoSession,'s1'),'live','余额多少')
    assert '账户服务暂时不可用' in state['messages'][-1]['content']
    assert '999999' not in state['messages'][-1]['content']


def test_knowledge_retrieval_returns_versioned_sources_without_dynamic_account_data(bank,monkeypatch):
    db,_=bank
    def explain(payload):
        row=payload['tool_results'][0]['data']['items'][0]
        assert row['source_ref'].startswith('kb:')
        return turn('reply','knowledge',reply='挂失需要独立验证，解冻不能解除挂失。',evidence_refs=[row['source_ref']])
    sequence(monkeypatch,turn('reply','knowledge',tools=[ReadRequest(name='search_knowledge',
        arguments_json=json.dumps({'query':'冻结与挂失有什么不同'}))]),explain)
    state=create(db,db.get(DemoSession,'s1'),'live','冻结与挂失有什么不同')
    assert 'kb:card' in state['messages'][-1]['content']


def test_latest_request_and_pause_discard_late_model_output(bank,monkeypatch):
    db,_=bank; session=db.get(DemoSession,'s1')
    def late(payload):
        task=db.scalar(select(AgentTask).order_by(AgentTask.created_at_utc.desc()))
        from app.agent.control import change
        change(db,task,'pause','暂停')
        return transfer()
    sequence(monkeypatch,late)
    state=create(db,session,'live','从活期账户转100元到储蓄账户')
    assert state['status']=='paused' and state['proposal'] is None


def test_model_outage_is_live_and_emergency_stop_remains_offline(bank,monkeypatch):
    db,_=bank; session=db.get(DemoSession,'s1')
    def unavailable(payload):raise DomainError('MODEL_UNAVAILABLE','DeepSeek连接失败',503)
    seen=sequence(monkeypatch,transfer(),unavailable)
    state=create(db,session,'live','从活期账户转100元到储蓄账户'); old=state['proposal']['id']
    state=send(db,session,state['id'],'改成80元')
    assert state['mode']=='live' and state['proposal']['id']==old and '未降级' in state['messages'][-1]['content']
    state=send(db,session,state['id'],'紧急停止')
    assert state['status']=='cancelled' and len(seen)==2


def test_model_call_budget_caps_repairs_and_total_timeout(monkeypatch):
    monkeypatch.setenv('MODEL_API_KEY','sk-test-only-nonsecret-configuration')
    calls=[]
    def bad(request):
        calls.append(request)
        return httpx.Response(200,json={'choices':[{'message':{'tool_calls':[]}}]})
    with httpx.Client(transport=httpx.MockTransport(bad)) as client, budget(1,1):
        with pytest.raises(DomainError) as error:language.interpret({'messages':[]},client)
    assert error.value.code=='MODEL_BUDGET_EXCEEDED' and len(calls)==1


def test_context_is_bounded_private_and_preserves_authoritative_draft(bank,monkeypatch):
    db,_=bank; session=db.get(DemoSession,'s1')
    seen=sequence(monkeypatch,transfer(),turn('reply',reply='请核对操作单。'))
    state=create(db,session,'live','从活期账户转100元到储蓄账户')
    proof=issue_confirmation_token(db,session_id='s1',proposal_id=state['proposal']['id'])['confirmation_token']
    state=send(db,session,state['id'],'这笔有什么要核对的')
    payload=seen[-1]; serialized=json.dumps(payload)
    assert proof not in serialized
    assert payload['dialogue_context']['drafts']['dialogue_slots']['amount_yuan']=='100'
    assert len(payload['messages'])<=12
    assert not state['proposal'].get('receipt')


def test_latest_full_user_message_is_not_cut_before_conditions_at_the_end(bank,monkeypatch):
    db,_=bank
    text='请完整理解这段补充。'*83+'只有人工核对后才准备，明天上午不办理。'
    assert 800<len(text)<=1000
    seen=sequence(monkeypatch,turn('reply',reply='我会保留你的全部条件。'))
    create(db,db.get(DemoSession,'s1'),'live',text)
    assert seen[0]['messages'][-1]['content']==text


def test_missing_field_semantics_keeps_already_explicit_recipient(bank,monkeypatch):
    db,_=bank; session=db.get(DemoSession,'s1')
    sequence(monkeypatch,turn('transfer',target='李文',needs_clarification=True,missing_fields=['source','amount_yuan']),
        turn('transfer','supplement',source='活期账户',target='李文',amount_yuan='60'))
    state=create(db,session,'live','帮我给李文转一笔钱')
    assert state['proposal'] is None
    state=send(db,session,state['id'],'从活期账户付，金额60元')
    assert state['proposal']['summary']['target']['id']=='r3'


def test_user_tail_choice_cannot_be_reinterpreted_as_saved_remark(bank,monkeypatch):
    db,_=bank;session=db.get(DemoSession,'s1')
    sequence(monkeypatch,turn('transfer',source='活期账户',target='张三',amount_yuan='100'),
        turn('transfer','supplement',target='张三',target_note='尾号3821'),
        turn('transfer','modify',clear_fields=['target','target_note']),
        turn('transfer','modify',target='张三',target_note='尾号9156'))
    state=create(db,session,'live','从活期账户转100元给张三')
    state=send(db,session,state['id'],'选尾号3821')
    assert state['proposal']['summary']['target']['id']=='r1'
    state=send(db,session,state['id'],'不是这个张三')
    assert state['proposal'] is None
    state=send(db,session,state['id'],'换成尾号9156')
    assert state['proposal']['summary']['target']['id']=='r2'


def test_portfolio_insert_query_preserves_pending_transfer_and_draft(bank,monkeypatch):
    db,_=bank;session=db.get(DemoSession,'s1')
    sequence(monkeypatch,transfer(),turn('wealth_status','insert_query'),turn('transfer','modify',amount_yuan='80'))
    state=create(db,session,'live','从活期账户转100元到储蓄账户');pid=state['proposal']['id']
    state=send(db,session,state['id'],'顺便查一下我的理财持仓')
    assert state['status']=='awaiting_confirmation' and state['proposal']['id']==pid and state['intent']=='transfer'
    state=send(db,session,state['id'],'改成80元')
    assert state['proposal']['summary']['amount_minor']==8000


def test_card_polling_does_not_invalidate_semantic_revision(bank,monkeypatch):
    db,_=bank; session=db.get(DemoSession,'s1')
    from app.agent.service import view
    def modify(payload):
        task=db.scalar(select(AgentTask).order_by(AgentTask.created_at_utc.desc()))
        stamp=task.updated_at_utc
        for _ in range(3):view(db,task)
        assert task.updated_at_utc==stamp
        return turn('card_management','modify',action='limit',card='1234',amount_yuan='500')
    sequence(monkeypatch,turn('card_management',action='online_block',card='1234'),modify)
    state=create(db,session,'live','尾号1234这张卡先别让它在网上花钱')
    state=send(db,session,state['id'],'还是刚才那张卡，把单笔限额改成500元')
    assert state['card_request']['kind']=='limit' and state['card_request']['summary']['limit_minor']==50000


def test_relative_bill_dates_grounded_on_demo_clock_not_model_wall_clock(bank,monkeypatch):
    db,_=bank
    def explained(payload):
        assert payload['tool_results'][0]['query']['start'] is None
        return turn('spending',reply='根据本次已返回数据核对餐饮消费。',
            evidence_refs=[payload['tool_results'][0]['source_ref']])
    sequence(monkeypatch,turn('spending',category='餐饮',period='this_month',
        tools=[ReadRequest(name='summarize_spending',arguments_json=json.dumps({'start':'2026-10-01','end':'2026-10-31','category':'餐饮'}))]),explained)
    state=create(db,db.get(DemoSession,'s1'),'live','看看本月餐饮花了多少')
    assert state['steps'][0]['result']['current']['period']['start_date']=='2026-09-01'


def test_knowledge_behavior_requires_retrieval_even_when_model_omits_tool(bank,monkeypatch):
    db,_=bank
    sequence(monkeypatch,turn('reply','knowledge',reply='取消银行代扣不等于商户会员取消。'),
        turn('reply','knowledge',reply=None))
    state=create(db,db.get(DemoSession,'s1'),'live','撤销银行代扣和商户会员的关系是什么')
    assert state['steps'][0]['tool_name']=='search_knowledge'
    assert 'kb:subscription' in state['messages'][-1]['content']


def test_timeout_and_malformed_json_are_not_a_silent_mock_fallback(bank,monkeypatch):
    db,_=bank
    monkeypatch.setenv('MODEL_API_KEY','sk-test-only-nonsecret-configuration')
    def timeout(*args,**kwargs):raise httpx.ReadTimeout('TEST-ONLY')
    monkeypatch.setattr(model_client,'measured_post',timeout)
    state=create(db,db.get(DemoSession,'s1'),'live','从活期账户转100元到储蓄账户')
    assert state['mode']=='live' and state['proposal'] is None and '未降级' in state['messages'][-1]['content']
    with pytest.raises(ValueError):language._understanding_arguments('{"intent":"accounts","dialogue":None}')
