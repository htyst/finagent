from unittest.mock import patch
import json
import httpx
import pytest
from sqlalchemy.orm import Session
from app.agent.language import Understanding, interpret, payload_for
from app.agent.dialogue_schema import DialogueTurn
from app.data.models import AgentTask, Proposal, TransferOrder
from app.domain.errors import DomainError
from test_m4 import demo, login, start, say, count


def semantic_decision(text, decision):
    behavior = ('confirmation' if text == '确认' else 'cancel' if decision['intent']=='cancel' else
                ('modify' if '改' in text else 'supplement') if decision.get('follow_up') else
                'insert_query' if decision['intent']=='accounts' else 'new_task')
    return Understanding(**decision, dialogue=DialogueTurn(behavior=behavior))


def live_start(client, headers, text, decision):
    with patch.dict('os.environ', {'FINAGENT_MODE': 'live'}), patch('app.agent.language.interpret', return_value=semantic_decision(text, decision)):
        return start(client, headers, text)


def live_say(client, headers, task, text, decision):
    with patch('app.agent.language.interpret', return_value=semantic_decision(text, decision)):
        return say(client, headers, task, text)


def test_reported_examples_do_not_query_ledger(demo):
    client, engine = demo
    headers = login(client)
    for text, expected in [('介绍一下你的功能', '余额'), ('查询', '你想查'), ('我想去日本玩，你在资金消费方面有什么建议', '预算')]:
        task = start(client, headers, text)
        assert not task['steps'] and not task['proposal']
        assert expected in task['messages'][-1]['content']
    with Session(engine) as db:
        assert count(db, TransferOrder) == 0


def test_follow_up_preserves_history_dates_and_category(demo):
    client, engine = demo
    headers = login(client)
    first = live_start(client, headers, '吃饭花了多少', dict(intent='spending', category='餐饮', period='this_month'))
    second = live_start(client, headers, '那上个月呢', dict(intent='spending', period='last_month', follow_up=True))
    assert first['messages'][0] in second['messages']
    assert second['steps'][0]['result']['current']['period']['start_date'] == '2026-08-01'
    assert '800.00' in second['messages'][-1]['content']
    third = live_start(client, headers, '那交通呢', dict(intent='spending', category='交通', follow_up=True))
    assert third['steps'][0]['result']['current']['period']['start_date'] == '2026-08-01'
    with Session(engine) as db:
        assert json.loads(db.get(AgentTask, third['id']).context_json)['last_query']['args']['category'] == '交通'


def test_live_reply_and_budget_followup_keep_dialogue_without_tools(demo):
    client, _ = demo
    headers = login(client)
    first = live_start(client, headers, '想去日本玩，有什么消费建议', dict(intent='reply', reply='可以先拆分交通、住宿和日常预算。计划几个人、几天？'))
    second = live_start(client, headers, '两个人7天', dict(intent='reply', reply='两个人7天，可以先扣除机票和住宿，再按天分配。总预算多少？'))
    assert len(second['messages']) == 4 and not second['steps']
    assert first['messages'][0] in second['messages']


def test_live_transfer_ambiguity_revision_read_and_cancel(demo):
    client, engine = demo
    headers = login(client)
    task = live_start(client, headers, '从活期给张三打一百块', dict(intent='transfer', source='活期', target='张三', amount_yuan='100'))
    assert not task['proposal'] and '3821' in task['messages'][-1]['content'] and '9156' in task['messages'][-1]['content']
    task = live_say(client, headers, task, '3821那个', dict(intent='transfer', target='3821', follow_up=True))
    old = task['proposal']['id']
    task = live_say(client, headers, task, '先看看余额', dict(intent='accounts'))
    assert task['proposal']['id'] == old and task['status'] == 'awaiting_confirmation'
    assert '10,000.00' in task['messages'][-1]['content']
    task = live_say(client, headers, task, '改成两百', dict(intent='transfer', amount_yuan='200', follow_up=True))
    assert task['proposal']['id'] != old and task['proposal']['summary']['amount_minor'] == 20000
    task = live_say(client, headers, task, '确认', dict(intent='reply', reply='请核对页面操作单后确认。'))
    assert task['status'] == 'awaiting_confirmation'
    task = live_say(client, headers, task, '算了', dict(intent='cancel'))
    assert task['status'] == 'cancelled'
    with Session(engine) as db:
        assert db.get(Proposal, old).status == 'invalidated'
        assert count(db, TransferOrder) == 0


def test_no_other_user_references_or_history(demo):
    client, engine = demo
    headers = login(client)
    first = live_start(client, headers, '从活期给张三100元', dict(intent='transfer', source='活期', target='张三', amount_yuan='100'))
    headers = login(client, 'u2')
    second = live_start(client, headers, '从活期给张三100元', dict(intent='transfer', source='活期', target='张三', amount_yuan='100'))
    assert len(second['messages']) == 2 and not second['proposal']
    assert '日常账户' in second['messages'][-1]['content']
    assert client.get(f"/api/tasks/{first['id']}").status_code == 404


def test_model_failure_keeps_pending_and_never_falls_back(demo):
    client, engine = demo
    headers = login(client)
    task = live_start(client, headers, '活期到储蓄100元', dict(intent='transfer', source='活期', target='储蓄', amount_yuan='100'))
    old = task['proposal']['id']
    with patch('app.agent.language.interpret', side_effect=DomainError('MODEL_UNAVAILABLE', '模型超时', 503)):
        task = say(client, headers, task, '改两百')
    assert task['proposal']['id'] == old and task['status'] == 'awaiting_confirmation'
    with Session(engine) as db:
        assert count(db, TransferOrder) == 0


def test_adapter_rejects_extra_privileges_and_uses_minimal_payload(demo):
    client, engine = demo
    headers = login(client)
    task = live_start(client, headers, '你好', dict(intent='reply', reply='你好，想了解什么？'))
    with Session(engine) as db:
        payload = payload_for(db, db.get(AgentTask, task['id']), task)
        assert set(payload) == {'demo_date', 'messages', 'pending_operation', 'current_time', 'now_utc',
                                'schedule_time_zone', 'requirement', 'pending_schedule', 'pending_collection',
                                'pending_subscription', 'pending_birthday', 'birthday_business_now_utc', 'clarification'}
        assert 'csrf' not in json.dumps(payload) and 'accounts' not in payload
    malformed = Understanding(intent='accounts').model_dump() | {'user_id': 'u2'}
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={'choices': [{'message': {'tool_calls': [{'function': {'name': 'understand_request', 'arguments': json.dumps(malformed)}}]}}]}))
    with patch.dict('os.environ', {'MODEL_API_KEY': 'test-only'}), httpx.Client(transport=transport) as http:
        with pytest.raises(DomainError) as error:
            interpret(payload, http)
        assert error.value.code == 'MODEL_OUTPUT_MISMATCH'


def test_conditional_write_cannot_become_unconditional(demo):
    client, engine = demo
    headers = login(client)
    task = live_start(client, headers, '余额低于两万才从活期给张三100元', dict(intent='transfer', source='活期', target='3821', amount_yuan='100'))
    assert not task['proposal'] and not task['steps']
    with Session(engine) as db:
        assert count(db, Proposal) == 0


def test_budget_arithmetic_and_follow_up(demo):
    client, engine = demo
    headers = login(client)
    first = live_start(client, headers, '两个人7天预算一万', dict(intent='budget', amount_yuan='10000', people=2, days=7))
    reply = first['messages'][-1]['content']
    assert '214.29' in reply and '合计 10,000.00' in reply and not first['steps']
    second = live_start(client, headers, '改成5天呢', dict(intent='budget', days=5, follow_up=True))
    assert '300.00' in second['messages'][-1]['content'] and '2 人、5 天' in second['messages'][-1]['content']
    with Session(engine) as db:
        assert count(db, Proposal) == 0


def test_balance_question_does_not_lose_unfinished_transfer(demo):
    client, _ = demo
    headers = login(client)
    task = live_start(client, headers, '活期转一百元给张三', dict(intent='transfer', source='活期', target='张三', amount_yuan='100'))
    task = live_say(client, headers, task, '先查余额', dict(intent='accounts'))
    assert task['status'] == 'gathering_info'
    task = live_say(client, headers, task, '3821那个', dict(intent='transfer', target='3821', follow_up=True))
    assert task['proposal']['summary']['amount_minor'] == 10000


def test_completed_receipt_is_carried_as_fact_not_write_slots(demo):
    client, engine = demo
    headers = login(client)
    task = live_start(client, headers, '活期转储蓄100元', dict(intent='transfer', source='活期', target='储蓄', amount_yuan='100'))
    proposal_id = task['proposal']['id']
    token = client.post(f'/api/proposals/{proposal_id}/confirmation-token', headers=headers).json()['data']['confirmation_token']
    response = client.post(f'/api/proposals/{proposal_id}/confirm', headers=headers, json={'confirmation_token':token})
    assert response.status_code == 200
    next_task = live_start(client, headers, '完成了吗', dict(intent='reply', reply='上一笔模拟转账已成功。'))
    assert any('上一步操作回执：模拟转账成功' in row['content'] for row in next_task['messages'])
    with Session(engine) as db:
        assert 'dialogue_slots' not in json.loads(db.get(AgentTask,next_task['id']).context_json)
        assert count(db,TransferOrder) == 1
