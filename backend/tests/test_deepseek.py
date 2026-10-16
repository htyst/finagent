"""Offline provider contracts and Live business integration; no real API calls."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agent import language, model_client, service
from app.agent.privacy import scrub_auth
from app.agent.telemetry import collect
from app.data.db import get_db
from app.data.models import Account, AgentMessage, Card, DemoSession
from app.domain.errors import DomainError
from app.main import app

TEST_KEY = 'sk-test-only-never-a-real-credential'


@pytest.fixture(autouse=True)
def config(monkeypatch):
    monkeypatch.setenv('MODEL_PROVIDER', 'deepseek')
    monkeypatch.setenv('MODEL_API_KEY', TEST_KEY)
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    monkeypatch.setenv('MODEL_BASE_URL', 'https://api.deepseek.com')
    monkeypatch.setenv('MODEL_NAME', 'deepseek-flash')


def tool_reply(args, name='understand_request'):
    return {'choices': [{'message': {'tool_calls': [{'function': {
        'name': name, 'arguments': json.dumps(args)}}]}}],
        'usage': {'prompt_tokens': 12, 'completion_tokens': 5, 'total_tokens': 17}}


def test_standard_and_beta_tools_validate_locally(monkeypatch):
    for suffix, strict in [('', False), ('/beta', True)]:
        monkeypatch.setenv('MODEL_BASE_URL', 'https://api.deepseek.com' + suffix)
        calls = []
        def respond(request):
            body = json.loads(request.content)
            assert request.url == 'https://api.deepseek.com' + suffix + '/chat/completions'
            assert request.headers['authorization'] == 'Bearer ' + TEST_KEY
            assert body['thinking'] == {'type': 'disabled'}
            assert body['stream'] is False
            assert body['tools'][0]['function']['strict'] is strict
            calls.append(body)
            return httpx.Response(200, json=tool_reply({'intent': 'accounts'}))
        with httpx.Client(transport=httpx.MockTransport(respond)) as client, collect() as metrics:
            decision = language.interpret({'messages': [{'role': 'user', 'content': '我有多少钱'}]}, client)
        assert decision.intent == 'accounts' and len(calls) == 1
        assert metrics[0]['usage']['total_tokens'] == 17
        assert TEST_KEY not in json.dumps(metrics)


@pytest.mark.parametrize('status,code', [(401, 'MODEL_AUTH_FAILED'), (402, 'MODEL_BALANCE_INSUFFICIENT'),
    (404, 'MODEL_REQUEST_INVALID'), (422, 'MODEL_REQUEST_INVALID'), (429, 'MODEL_RATE_LIMITED'), (503, 'MODEL_UNAVAILABLE')])
def test_provider_failure_is_actionable_and_does_not_echo_secrets(status, code):
    requests = []
    def reject(request):
        requests.append(request)
        return httpx.Response(status, json={'error': {'message': TEST_KEY}})
    with httpx.Client(transport=httpx.MockTransport(reject)) as client:
        with pytest.raises(DomainError) as error:
            language.interpret({'messages': []}, client)
    assert error.value.code == code
    assert TEST_KEY not in str(error.value)
    assert len(requests) == (2 if status in (429, 503) else 1)


@pytest.mark.parametrize('base', ['https://api.deepseek.com.evil.example', 'http://api.deepseek.com',
    'https://someone:password@api.deepseek.com', 'https://api.deepseek.com?key=private'])
def test_configuration_cannot_send_key_to_another_host(monkeypatch, base):
    monkeypatch.setenv('MODEL_BASE_URL', base)
    with pytest.raises(DomainError) as error:
        model_client.configuration()
    assert error.value.code == 'MODEL_CONFIG_INVALID'
    assert 'private' not in error.value.message


def test_blank_defaults_alias_and_safe_config_repr(monkeypatch):
    monkeypatch.setenv('MODEL_NAME', '')
    monkeypatch.setenv('MODEL_BASE_URL', '')
    assert model_client.configuration().model == 'deepseek-flash'
    assert TEST_KEY not in repr(model_client.configuration())
    assert TEST_KEY not in json.dumps(model_client.public_status())
    monkeypatch.setenv('MODEL_API_KEY', '')
    assert model_client.public_status()['configured'] is False
    with pytest.raises(DomainError) as error:
        model_client.configuration()
    assert error.value.code == 'MODEL_NOT_CONFIGURED'
    monkeypatch.setenv('DEEPSEEK_API_KEY', TEST_KEY)
    assert model_client.configuration().api_key == TEST_KEY


def test_malformed_or_injected_model_output_does_not_reach_tools():
    responses = []
    def invalid(request):
        responses.append(request)
        return httpx.Response(200, json=tool_reply({'intent': 'transfer', 'authorized': True, 'amount_yuan': '100'}))
    with httpx.Client(transport=httpx.MockTransport(invalid)) as client:
        with pytest.raises(DomainError) as error:
            language.interpret({'messages': []}, client)
    assert error.value.code == 'MODEL_OUTPUT_MISMATCH'
    assert len(responses) == 2


def test_key_pasted_into_chat_is_scrubbed_before_storage_and_model(bank, monkeypatch):
    db, _ = bank
    seen = []
    def interpret(payload):
        seen.append(json.dumps(payload))
        return language.Understanding(intent='reply', reply='请只在本地配置密钥。')
    monkeypatch.setattr(language, 'interpret', interpret)
    service.create(db, db.get(DemoSession, 's1'), 'live', '我的 API Key 是 ' + TEST_KEY)
    assert len(seen) == 1 and TEST_KEY not in seen[0]
    assert all(TEST_KEY not in row.content for row in db.scalars(select(AgentMessage)))
    assert scrub_auth({'api_key': TEST_KEY})['api_key'] != TEST_KEY


@pytest.mark.parametrize('text,args,expected', [
    ('我之前买的产品还有哪些在手里', {'intent': 'wealth_status'}, 'read_wealth'),
    ('测测我的投资风险承受能力', {'intent': 'wealth_assessment'}, None),
    ('我名下的银行卡都是什么情况', {'intent': 'card_management_status'}, 'read_card_management'),
    ('尾号1234这张卡先别让它在网上花钱', {'intent': 'card_management', 'action': 'online_block', 'card': '1234'}, 'prepare_card_management'),
    ('尾号1234这张卡重新允许在网上花钱', {'intent': 'card_management', 'action': 'online_unblock', 'card': '1234'}, 'prepare_card_management'),
    ('上次给家里人庆生的事情怎么样了', {'intent': 'birthday_status'}, None),
    ('我那些每个月固定扣的钱现在什么情况', {'intent': 'subscription_status'}, None),
    ('以后某个时间帮我汇一笔钱', {'intent': 'schedule_transfer'}, None),
    ('这顿饭的钱帮我向朋友们分摊一下', {'intent': 'aa_collection'}, None),
])
def test_semantic_fallback_reaches_business_once_without_network_under_write_lock(bank, monkeypatch, text, args, expected):
    db, _ = bank
    calls = []
    def interpret(payload):
        assert not db.info.get('atomic_write'), 'model must not hold the SQLite write lock'
        calls.append(payload)
        assert len(calls) == 1, 'specialized flow must reuse the validated decision'
        return language.Understanding(**args)
    monkeypatch.setattr(language, 'interpret', interpret)
    balances = [(r.id, r.balance_minor) for r in db.scalars(select(Account))]
    result = service.create(db, db.get(DemoSession, 's1'), 'live', text)
    assert len(calls) == 1, result
    assert sum(r['role'] == 'user' and r['content'] == text for r in result['messages']) == 1
    if expected:
        assert expected in [r['tool_name'] for r in result['steps']], result
    assert balances == [(r.id, r.balance_minor) for r in db.scalars(select(Account))]
    from app.domain.card_management import controls
    assert controls(db, db.get(Card, 'c1a'))['online_enabled'] is True
    if args['intent'] in {'schedule_transfer', 'aa_collection'}:
        assert result['semantic_validation']['ok'] is False
        assert result['intent'] == args['intent']
        assert result['status'] == 'gathering_info'
    if args.get('action') == 'online_unblock':
        # Loosening permissions stays red, even when the model calls it ordinary chat.
        from app.domain.security import describe
        from app.domain.card_management import issue_token, confirm
        request_id = result['card_request']['id']
        assert describe(db, 's1', 'card', request_id)['level'] == 'red'
        token = issue_token(db, session_id='s1', request_id=request_id)['confirmation_token']
        with pytest.raises(DomainError) as error:
            confirm(db, session_id='s1', request_id=request_id, confirmation_token=token)
        assert error.value.code == 'MFA_REQUIRED'


def test_probe_requires_session_csrf_and_returns_no_key(bank, monkeypatch):
    _, factory = bank
    def database():
        with factory() as db:
            yield db
    calls = []
    def probe():
        calls.append(True)
        return dict(model_client.public_status(), connected=True)
    monkeypatch.setattr(model_client, 'probe', probe)
    app.dependency_overrides[get_db] = database
    try:
        with TestClient(app) as client:
            assert TEST_KEY not in client.get('/api/demo/info').text
            assert client.post('/api/model/probe').status_code == 401
            client.post('/api/demo/session', json={'user_id': 'u1'})
            assert client.post('/api/model/probe').status_code == 403
            token = client.get('/api/demo/csrf').json()['data']['csrf_token']
            response = client.post('/api/model/probe', headers={'Origin': 'http://testserver', 'X-FinAgent-CSRF': token})
            assert response.status_code == 200 and response.json()['data']['connected']
            assert TEST_KEY not in response.text
        assert calls == [True]
    finally:
        app.dependency_overrides.clear()


def test_live_http_dialogue_uses_verified_facts_and_preserves_confirmation_and_mfa(bank, monkeypatch):
    _, factory = bank
    monkeypatch.setenv('FINAGENT_MODE', 'live')
    def database():
        with factory() as db:
            yield db
    decisions = iter([{'intent': 'accounts', 'dialogue': {'behavior':'new_task'}},
        {'intent': 'transfer', 'dialogue': {'behavior':'new_task'}, 'source': '活期账户', 'target': '储蓄账户', 'amount_yuan': '100'},
        {'intent': 'reply', 'dialogue': {'behavior': 'confirmation'}},
        {'intent': 'transfer', 'dialogue': {'behavior':'new_task'}, 'source': '活期账户', 'target': '储蓄账户', 'amount_yuan': '2000'}])
    calls = []
    def model_response(http, url, **kwargs):
        body = kwargs['json']
        assert body['tool_choice']['function']['name'] == 'understand_request'
        assert body['thinking'] == {'type': 'disabled'}
        calls.append(True)
        return httpx.Response(200, json=tool_reply(next(decisions)))
    monkeypatch.setattr(model_client, 'measured_post', model_response)
    app.dependency_overrides[get_db] = database
    try:
        with TestClient(app) as client:
            client.post('/api/demo/session', json={'user_id': 'u1'})
            csrf = client.get('/api/demo/csrf').json()['data']['csrf_token']
            protected = {'Origin': 'http://testserver', 'X-FinAgent-CSRF': csrf}
            before = client.get('/api/accounts').json()['data']
            query = client.post('/api/tasks', headers=protected, json={'message': '我名下现在有多少钱'})
            assert query.status_code == 200
            result = query.json()['data']
            assert result['mode'] == 'live' and result['status'] == 'completed'
            assert result['steps'][0]['result'] == before
            small = client.post('/api/tasks', headers=protected, json={'message': '从活期账户转100元到储蓄账户'}).json()['data']
            assert small['status'] == 'awaiting_confirmation'
            assert small['proposal']['summary']['amount_minor'] == 10000
            assert client.get('/api/accounts').json()['data'] == before
            pid = small['proposal']['id']
            assert client.get(f'/api/security/operations/proposal/{pid}').json()['data']['level'] == 'yellow'
            reply = client.post(f"/api/tasks/{small['id']}/messages", headers=protected, json={'message': '确认'})
            assert reply.status_code == 200 and reply.json()['data']['proposal']['status'] == 'pending_confirmation'
            token = client.post(f'/api/proposals/{pid}/confirmation-token', headers=protected).json()['data']['confirmation_token']
            confirmed = client.post(f'/api/proposals/{pid}/confirm', headers=protected, json={'confirmation_token': token})
            assert confirmed.status_code == 200
            large = client.post('/api/tasks', headers=protected, json={'message': '从活期账户转2000元到储蓄账户'}).json()['data']
            pid = large['proposal']['id']
            assert client.get(f'/api/security/operations/proposal/{pid}').json()['data']['level'] == 'red'
            token = client.post(f'/api/proposals/{pid}/confirmation-token', headers=protected).json()['data']['confirmation_token']
            denied = client.post(f'/api/proposals/{pid}/confirm', headers=protected, json={'confirmation_token': token})
            assert denied.status_code == 403 and denied.json()['error_code'] == 'MFA_REQUIRED'
            after = client.get('/api/accounts').json()['data']
            assert next(a for a in after if a['id'] == 'a1c')['balance_minor'] == next(a for a in before if a['id'] == 'a1c')['balance_minor'] - 10000
        assert len(calls) == 4  # Ordinary chat confirmation is understood, never authorized.
    finally:
        app.dependency_overrides.clear()


def test_model_auth_failure_is_not_repaired_as_a_business_plan(monkeypatch):
    from app.agent.orchestrator import generate_plan
    calls = []
    def deny(*args, **kwargs):
        calls.append(True)
        raise DomainError('MODEL_AUTH_FAILED', 'test authentication failure', 503)
    monkeypatch.setattr(model_client, 'post_chat', deny)
    with pytest.raises(DomainError) as error:
        generate_plan([{'id': 'g1', 'tool': 'list_accounts', 'params': {}}], 'live')
    assert error.value.code == 'MODEL_AUTH_FAILED' and len(calls) == 1
