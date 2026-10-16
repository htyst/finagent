"""Regression cases from the user's four reproduced review findings."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch
from datetime import datetime
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from test_m4 import demo, login, start, say
from test_conversation import live_start
from app.agent.language import Understanding
from app.data.db import get_db
from app.data.models import AgentTask, Proposal, BusinessResult
from app.domain.errors import DomainError
from app.main import app

STORY = '如果本月餐饮超过500元，就从活期转100元到储蓄账户'

@pytest.fixture
def production_demo(demo):
    client, engine = demo
    def override():
        with Session(engine, expire_on_commit=False) as db:
            yield db
    app.dependency_overrides[get_db] = override
    return client, engine

@pytest.mark.parametrize('mode', ['mock', 'live'])
@pytest.mark.parametrize('revision', ['改成低于500元才转', '低于500元才转，可以吗？'])
def test_condition_reversal_and_read_interruption(production_demo, mode, revision):
    client, engine = production_demo
    headers = login(client)
    with patch.dict('os.environ', {'FINAGENT_MODE':mode}), patch('app.agent.crossflow.model_check'):
        task = start(client, headers, STORY)
        original = task['proposal']['id']
        task = say(client, headers, task, '先查一下账户余额')
        assert task['proposal']['id'] == original
        assert '10,000.00' in task['messages'][-1]['content']
        assert len(task['steps']) == 2
        task = say(client, headers, task, revision)
    assert task['proposal'] is None
    assert task['status'] == 'completed'
    condition = task['steps'][0]['result']['condition']
    assert condition['type'] == 'spending_lt' and condition['threshold_minor'] == 50000
    assert task['steps'][0]['result']['met'] is False
    with Session(engine) as db:
        assert db.get(Proposal, original).status == 'invalidated'
        assert db.scalar(select(func.count()).select_from(BusinessResult)) == 0

@pytest.mark.parametrize('revision', ['改成不超过500元才转', '改成去年餐饮超过500元才转',
                                     '收款账户改成不存在的账户', '改成余额超过500元才转'])
def test_unknown_revision_disables_old_proposal_and_requires_restatement(production_demo, revision):
    client, engine = production_demo
    headers = login(client)
    task = start(client, headers, STORY)
    old = task['proposal']['id']
    task = say(client, headers, task, revision)
    assert task['status'] == 'gathering_info' and task['proposal'] is None
    assert '重新说明' in task['messages'][-1]['content']
    assert client.post(f'/api/proposals/{old}/confirmation-token', headers=headers).status_code == 409
    task = say(client, headers, task, '如果本月餐饮超过800元才转')
    assert task['proposal']['summary']['condition']['threshold_minor'] == 80000
    with Session(engine) as db:
        assert db.get(Proposal, old).status == 'invalidated'

@pytest.mark.parametrize('mode', ['mock', 'live'])
@pytest.mark.parametrize('condition', [STORY, '加个条件：本月餐饮超过500元'])
def test_existing_transfer_accepts_condition_and_revokes_old_token(production_demo, mode, condition):
    client, engine = production_demo
    headers = login(client)
    if mode == 'live':
        task = live_start(client, headers, '从活期转100元到储蓄',
                          dict(intent='transfer',source='活期',target='储蓄',amount_yuan='100'))
    else:
        task = start(client, headers, '从活期账户转到储蓄账户100元')
    old = task['proposal']['id']
    token = client.post(f'/api/proposals/{old}/confirmation-token', headers=headers).json()['data']['confirmation_token']
    with patch('app.agent.crossflow.model_check'):
        task = say(client, headers, task, condition)
    assert task['intent'] == 'conditional_transfer' and task['status'] == 'awaiting_confirmation'
    assert task['proposal']['id'] != old
    assert task['proposal']['summary']['amount_minor'] == 10000
    assert task['proposal']['summary']['target']['mask'] == '**6105'
    assert task['proposal']['summary']['condition']['type'] == 'spending_gt'
    response = client.post(f'/api/proposals/{old}/confirm', headers=headers, json={'confirmation_token':token})
    assert response.status_code == 409
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(BusinessResult)) == 0

@pytest.mark.parametrize('failure', [False, True])
@pytest.mark.parametrize('initial_pending', [False, True])
def test_cancel_while_model_waits_discards_late_success_and_error(production_demo, failure, initial_pending):
    client, engine = production_demo
    headers = login(client)
    slots = dict(intent='transfer',source='活期',amount_yuan='100')
    if initial_pending:
        slots['target'] = '储蓄'
    task = live_start(client, headers, '从活期转100元' + ('到储蓄' if initial_pending else ''), slots)
    entered, release = Event(), Event()
    def delayed(_payload):
        entered.set()
        assert release.wait(10)
        if failure:
            raise DomainError('MODEL_UNAVAILABLE', '模型暂时不可用', 503)
        return Understanding(intent='transfer',target='储蓄',follow_up=True)
    with TestClient(app) as other, ThreadPoolExecutor(max_workers=1) as pool, patch('app.agent.language.interpret', side_effect=delayed):
        other.cookies.update(client.cookies)
        pending = pool.submit(say, client, headers, task, '转到储蓄账户')
        try:
            assert entered.wait(5)
            response = other.post(f"/api/tasks/{task['id']}/cancel", headers=headers)
            assert response.json()['data']['status'] == 'cancelled'
        finally:
            release.set()
        assert pending.result(timeout=10)['status'] == 'cancelled'
    assert client.get(f"/api/tasks/{task['id']}").json()['data']['status'] == 'cancelled'
    with Session(engine) as db:
        assert db.get(AgentTask, task['id']).stop_requested
        assert db.scalar(select(func.count()).select_from(Proposal)) == int(initial_pending)
        if initial_pending:
            assert db.get(Proposal, task['proposal']['id']).status == 'cancelled'
        assert db.scalar(select(func.count()).select_from(BusinessResult)) == 0

def test_newer_message_wins_over_late_model_response(production_demo):
    client, engine = production_demo
    headers = login(client)
    task = live_start(client, headers, '从活期转100元', dict(intent='transfer',source='活期',amount_yuan='100'))
    entered, release = Event(), Event()
    def interpret(payload):
        if payload['messages'][-1]['content'] == '转到储蓄账户':
            entered.set()
            assert release.wait(10)
            return Understanding(intent='transfer',target='储蓄',follow_up=True)
        return Understanding(intent='transfer',source='活期',target='7072',amount_yuan='200')
    with TestClient(app) as other, ThreadPoolExecutor(max_workers=1) as pool, patch('app.agent.language.interpret', side_effect=interpret):
        other.cookies.update(client.cookies)
        pending = pool.submit(say, client, headers, task, '转到储蓄账户')
        try:
            assert entered.wait(5)
            newer = say(other, headers, task, '改为从活期给李文200元')
            assert newer['proposal']['summary']['amount_minor'] == 20000
        finally:
            release.set()
        assert pending.result(timeout=10)['proposal']['id'] == newer['proposal']['id']
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Proposal)) == 1

def test_transaction_evidence_keeps_query_filters(production_demo):
    client, _ = production_demo
    task = start(client, login(client), '查询上个月餐饮流水')
    step = task['steps'][0]
    assert step['tool_name'] == 'list_transactions'
    assert step['arguments']['start'] == '2026-08-01'
    assert step['arguments']['end'] == '2026-08-31'
    assert step['arguments']['category'] == '餐饮'
    assert {row['id'] for row in step['result']['items']} == {'t_food_prev_900','t_refund_prev_100'}
    restored = client.get(f"/api/tasks/{task['id']}").json()['data']
    assert restored['steps'] == task['steps']

def test_less_than_is_rechecked_at_confirmation(production_demo):
    from app.domain.ledger import post_entry
    client, engine = production_demo
    headers = login(client)
    task = start(client, headers, '如果本月餐饮低于1500元，就从活期转100元到储蓄账户')
    pid = task['proposal']['id']
    assert task['proposal']['summary']['condition']['type'] == 'spending_lt'
    with Session(engine) as db:
        # Add a genuine ledger entry, preserving account invariants.
        post_entry(db, owner_id='u1', account_id='a1c', kind='purchase', direction='debit', amount_minor=40000,
                   business_at=datetime.fromisoformat('2026-09-24T04:00:00+00:00'), category='餐饮', merchant='追加消费')
        db.commit()
    token = client.post(f'/api/proposals/{pid}/confirmation-token', headers=headers).json()['data']['confirmation_token']
    result = client.post(f'/api/proposals/{pid}/confirm', headers=headers,json={'confirmation_token':token})
    assert result.status_code == 409
    assert result.json()['error_code'] == 'CONDITION_NOT_MET'
