import base64
from datetime import datetime, timedelta, timezone

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.cloud import ChatBudget, create_cloud_app

PASSWORD = 'TEST-ONLY-visitor-access-password'
AUTH = {'Authorization': 'Basic ' + base64.b64encode(('guest:' + PASSWORD).encode()).decode()}


@pytest.fixture
def cloud(tmp_path, monkeypatch):
    monkeypatch.setenv('FINAGENT_ACCESS_PASSWORD', PASSWORD)
    monkeypatch.setenv('FINAGENT_ACCESS_USER', 'guest')
    monkeypatch.setenv('CLOUD_DAILY_CHAT_LIMIT', '2')
    monkeypatch.setenv('CLOUD_CHAT_PER_MINUTE', '2')
    static = tmp_path / 'dist'
    (static / 'assets').mkdir(parents=True)
    (static / 'index.html').write_text('<html>FinAgent TEST-ONLY</html>', encoding='utf-8')
    (static / 'assets/test.js').write_text('/* TEST-ONLY */', encoding='utf-8')
    calls = []

    async def api(request):
        calls.append(request.url.path)
        return JSONResponse({'path': request.url.path, 'method': request.method})

    banking = Starlette(routes=[Route('/api/{path:path}', api, methods=['GET', 'POST'])])
    factory = lambda: create_cloud_app(banking, static_dir=static, data_dir=tmp_path / 'state')
    return factory, calls


def test_cloud_access_is_required_for_pages_assets_api_and_writes(cloud):
    factory, calls = cloud
    with TestClient(factory()) as client:
        for path in ['/', '/assets/test.js', '/api/demo/info', '/api/health/']:
            assert client.get(path, follow_redirects=False).status_code == 401
        assert client.post('/api/proposals/x/confirm').status_code == 401
        assert client.get('/api/health').status_code == 200
        assert calls == ['/api/health']


def test_cloud_authentication_cannot_be_forged(cloud):
    factory, calls = cloud
    with TestClient(factory()) as client:
        for header in ['Bearer anything', 'Basic broken!', 'Basic ' + base64.b64encode(b'guest:wrong').decode()]:
            assert client.get('/api/accounts', headers={'Authorization': header}).status_code == 401
        assert not calls


def test_cloud_routes_keep_api_path_and_static_spa_are_separate(cloud):
    factory, calls = cloud
    with TestClient(factory(), headers=AUTH) as client:
        assert client.get('/').text.startswith('<html>')
        assert client.get('/assets/test.js').status_code == 200
        assert client.get('/assets/missing.js').status_code == 404
        assert client.get('/api/accounts').json()['path'] == '/api/accounts'
        assert client.get('/some-client-route').text.startswith('<html>')
        assert client.get('/api/unknown').json()['path'] == '/api/unknown'
        assert client.get('/api/accounts').headers['cache-control'] == 'no-store'


def test_cloud_budget_survives_restart_but_does_not_block_receipt_or_confirmation(cloud):
    factory, calls = cloud
    with TestClient(factory(), headers=AUTH) as client:
        assert client.post('/api/tasks').status_code == 200
        assert client.post('/api/tasks/t/messages').status_code == 200
    with TestClient(factory(), headers=AUTH) as client:
        assert client.post('/api/tasks').status_code == 429
        assert client.post('/api/model/probe').status_code == 429
        assert client.get('/api/tasks/t').status_code == 200
        assert client.post('/api/proposals/p/confirm').status_code == 200
        assert calls.count('/api/tasks') == 1


def test_budget_rollover_does_not_recreate_bank_data(tmp_path):
    bank = tmp_path / 'finagent.sqlite3'
    bank.write_bytes(b'TEST-ONLY durable bank sentinel')
    budget = ChatBudget(tmp_path / 'budget.sqlite3', daily=1, minute=1)
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    assert budget.consume(now)
    assert not budget.consume(now + timedelta(minutes=1))
    assert budget.consume(now + timedelta(days=1))
    assert bank.read_bytes() == b'TEST-ONLY durable bank sentinel'


def test_cloud_missing_or_weak_password_fails_closed(cloud, monkeypatch):
    factory, _ = cloud
    for password in ['', 'short']:
        monkeypatch.setenv('FINAGENT_ACCESS_PASSWORD', password)
        with pytest.raises(RuntimeError, match='访问用户名'):
            factory()


def test_cloud_missing_frontend_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv('FINAGENT_ACCESS_PASSWORD', PASSWORD)
    with pytest.raises(RuntimeError, match='前端生产构建'):
        create_cloud_app(Starlette(), static_dir=tmp_path)


def test_real_bank_permissions_and_secure_cookie_survive_cloud_gateway(bank, tmp_path, monkeypatch):
    from app.main import app
    from app.data.db import get_db
    _, factory = bank
    def database():
        with factory() as db:
            yield db
    monkeypatch.setenv('FINAGENT_ACCESS_PASSWORD', PASSWORD)
    monkeypatch.setenv('FINAGENT_ACCESS_USER', 'guest')
    monkeypatch.setenv('FINAGENT_SECURE_COOKIE', '1')
    static = tmp_path / 'dist'
    (static / 'assets').mkdir(parents=True)
    (static / 'index.html').write_text('TEST-ONLY', encoding='utf-8')
    app.dependency_overrides[get_db] = database
    try:
        with TestClient(create_cloud_app(app, static_dir=static, data_dir=tmp_path),
                        base_url='https://testserver', headers=AUTH) as client:
            session = client.post('/api/demo/session', json={'user_id': 'u1'})
            assert session.status_code == 200
            assert '; Secure' in session.headers['set-cookie']
            assert client.get('/api/accounts').status_code == 200
            assert client.get('/api/subscriptions').status_code == 403
            assert client.post('/api/transfers/prepare', json={
                'source_account_id': 'a1c', 'target': {'type': 'own_account', 'account_id': 'a1s'},
                'amount_minor': 10000}).status_code == 403
            csrf = client.get('/api/demo/csrf').json()['data']['csrf_token']
            headers = {'Origin': 'https://testserver', 'X-FinAgent-CSRF': csrf}
            prepared = client.post('/api/transfers/prepare', headers=headers, json={
                'source_account_id': 'a1c', 'target': {'type': 'own_account', 'account_id': 'a1s'}, 'amount_minor': 200000})
            assert prepared.status_code == 200
            pid = prepared.json()['data']['proposal']['id']
            assert client.get(f'/api/security/operations/proposal/{pid}').json()['data']['level'] == 'red'
            issued = client.post(f'/api/proposals/{pid}/confirmation-token', headers=headers)
            assert issued.status_code == 200
            confirmed = client.post(f'/api/proposals/{pid}/confirm', headers=headers,
                                    json={'confirmation_token': issued.json()['data']['confirmation_token']})
            assert confirmed.status_code == 403
    finally:
        app.dependency_overrides.clear()
