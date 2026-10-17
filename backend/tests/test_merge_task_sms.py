"""Merged task-bound simulator access must preserve A's authorization checks."""
from fastapi.testclient import TestClient
from app.main import app
from app.data.models import Account, DemoSession, TransferOrder
from app.agent.control import change
from sqlalchemy import select, func
from test_chat_authorization import prepare
from test_http import client  # noqa: F401


def test_task_sms_needs_csrf_matching_action_and_explicit_verification(client, bank):
    db, _ = bank
    task, action = prepare(db, amount=200000)
    client.cookies.set('finagent_demo_session', 's1')
    base = action['authorization_path']
    headers = {'Origin': 'http://testserver', 'X-FinAgent-CSRF': 'test-csrf'}
    body = {'action_id': action['action_id']}
    issued = client.post(base + '/challenge', headers=headers, json=body)
    assert issued.status_code == 200
    payload = {**body, 'challenge_id': issued.json()['data']['challenge_id']}
    assert client.post(base + '/simulated-sms', json=payload).status_code == 403
    assert client.post(base + '/simulated-sms', headers=headers,
                       json={**payload, 'action_id': '0' * 64}).status_code == 409
    before = [(a.id, a.balance_minor) for a in db.scalars(select(Account))]
    response = client.post(base + '/simulated-sms', headers=headers, json=payload)
    assert response.status_code == 200, response.text
    assert response.headers['Cache-Control'] == 'no-store'
    code = response.json()['data']['code']
    assert len(code) == 6 and code.isdigit()
    token = client.post(base + '/confirmation-token', headers=headers, json=body).json()['data']['confirmation_token']
    confirmation = {**body, 'confirmation_token': token}
    assert client.post(base + '/confirm', headers=headers, json=confirmation).status_code == 403
    db.expire_all()
    assert before == [(a.id, a.balance_minor) for a in db.scalars(select(Account))]
    assert db.scalar(select(func.count()).select_from(TransferOrder)) == 0
    assert client.post(base + '/verify', headers=headers, json={**payload, 'code': code}).status_code == 200
    assert client.post(base + '/simulated-sms', headers=headers, json=payload).status_code == 403
    assert client.post(base + '/confirm', headers=headers, json=confirmation).status_code == 200
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(TransferOrder)) == 1


def test_task_sms_rejects_other_session_paused_task_and_remote_client(client, bank):
    db, _ = bank
    task, action = prepare(db, amount=200000)
    client.cookies.set('finagent_demo_session', 's1')
    base = action['authorization_path']
    headers = {'Origin': 'http://testserver', 'X-FinAgent-CSRF': 'test-csrf'}
    body = {'action_id': action['action_id']}
    issued = client.post(base + '/challenge', headers=headers, json=body).json()['data']
    payload = {**body, 'challenge_id': issued['challenge_id']}
    client.cookies.set('finagent_demo_session', 's2')
    assert client.post(base + '/simulated-sms', headers=headers, json=payload).status_code == 404
    client.cookies.set('finagent_demo_session', 's1')
    with TestClient(app, client=('198.51.100.1', 12345)) as remote:
        remote.cookies.set('finagent_demo_session', 's1')
        denied = remote.post(base + '/simulated-sms', headers=headers, json=payload)
        assert denied.status_code == 403
        assert denied.json()['error_code'] == 'LOCAL_SIMULATOR_ONLY'
    change(db, task, 'pause')
    assert client.post(base + '/simulated-sms', headers=headers, json=payload).status_code == 409
