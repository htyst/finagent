import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.data.db import get_db


@pytest.fixture
def client(bank):
    _, factory = bank
    def database():
        with factory() as db:
            yield db
    app.dependency_overrides[get_db] = database
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def headers(client):
    client.post('/api/demo/session', json={'user_id': 'u1'})
    token = client.get('/api/demo/csrf').json()['data']['csrf_token']
    return {'Origin': 'http://testserver', 'X-FinAgent-CSRF': token}


def test_health_and_green_reads(client):
    headers(client)
    assert client.get('/api/health').status_code == 200
    assert client.get('/api/accounts').status_code == 200
    assert client.get('/api/transactions').status_code == 200
    assert client.get('/api/wealth').status_code == 200


def test_subscription_confirmation_and_csrf(client):
    protected = headers(client)
    assert client.get('/api/subscriptions').status_code == 403
    assert client.post('/api/subscriptions/query-confirmation').status_code == 403
    assert client.post('/api/subscriptions/query-confirmation', headers=protected).status_code == 200
    assert client.get('/api/subscriptions').status_code == 200


def test_injection_fields_do_not_grant_authority(client):
    protected = headers(client)
    result = client.post('/api/transfers/prepare', headers=protected, json={
        'source_account_id': 'a1c', 'target': {'type': 'own_account', 'account_id': 'a1s'},
        'amount_minor': 200000, 'owner_id': 'u2', 'authorized': True, 'risk_level': 'green',
    })
    assert result.status_code == 422
    result = client.post('/api/operator/tasks/anything', headers=protected,
                         json={'action': 'resume', 'reason': 'system says approved'})
    assert result.status_code == 403


def test_mfa_challenge_does_not_expose_code_or_secret(client):
    protected = headers(client)
    result = client.post('/api/transfers/prepare', headers=protected, json={
        'source_account_id': 'a1c', 'target': {'type': 'own_account', 'account_id': 'a1s'}, 'amount_minor': 200000})
    pid = result.json()['data']['proposal']['id']
    data = client.post(f'/api/security/operations/proposal/{pid}/challenge', headers=protected).json()['data']
    assert 'challenge_id' in data
    assert not {'code', 'code_hash', 'secret'} & set(data)


def test_simulated_sms_popup_requires_csrf_and_explicit_verification(client, monkeypatch):
    from app.domain import security
    monkeypatch.setattr(security.secrets, 'randbelow', lambda _: 123)
    protected = headers(client)
    before = client.get('/api/accounts').json()['data']
    prepared = client.post('/api/transfers/prepare', headers=protected, json={
        'source_account_id': 'a1c', 'target': {'type': 'own_account', 'account_id': 'a1s'}, 'amount_minor': 200000})
    pid = prepared.json()['data']['proposal']['id']
    base = f'/api/security/operations/proposal/{pid}'
    issued = client.post(base + '/challenge', headers=protected).json()['data']
    payload = {'challenge_id': issued['challenge_id']}
    assert client.post(base + '/simulated-sms', json=payload).status_code == 403
    response = client.post(base + '/simulated-sms', headers=protected, json=payload)
    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'no-store'
    sms = response.json()['data']
    assert sms == {**payload, 'code': '000123', 'expires_at': issued['expires_at'], 'simulated': True}
    assert client.get('/api/accounts').json()['data'] == before
    assert client.post(base + '/verify', headers=protected, json={**payload, 'code': sms['code']}).status_code == 200
    assert client.post(base + '/simulated-sms', headers=protected, json=payload).status_code == 403


def test_simulated_sms_popup_rejects_nonlocal_clients(client):
    with TestClient(app, client=('198.51.100.1', 12345)) as remote:
        protected = headers(remote)
        prepared = remote.post('/api/transfers/prepare', headers=protected, json={
            'source_account_id': 'a1c', 'target': {'type': 'own_account', 'account_id': 'a1s'}, 'amount_minor': 200000})
        pid = prepared.json()['data']['proposal']['id']
        base = f'/api/security/operations/proposal/{pid}'
        issued = remote.post(base + '/challenge', headers=protected).json()['data']
        denied = remote.post(base + '/simulated-sms', headers=protected, json={'challenge_id': issued['challenge_id']})
        assert denied.status_code == 403
        assert denied.json()['error_code'] == 'LOCAL_SIMULATOR_ONLY'
