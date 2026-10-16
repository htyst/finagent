from pathlib import Path
import json
import subprocess
import sys
from time import perf_counter
from datetime import date

import pytest
from sqlalchemy import select
from alembic import command
from alembic.config import Config

from app.agent.privacy import model_payload
from app.data.db import make_engine
from app.data.models import Account, BusinessResult, DemoSession, Recipient, User
from app.domain import card_management as cards
from app.domain.errors import DomainError
from test_http import client  # register the isolated HTTP fixture


def test_separate_user_operator_and_approver_roles(bank, client):
    import app.config
    db, _ = bank
    record = cards.prepare(db, session_id='s1', kind='apply', account_id='a1c', applicant=db.get(User, 'u1').display_name)
    rid = record['id']
    client.cookies.set('finagent_demo_session', 's1')
    headers = {'Origin': 'http://testserver', 'X-FinAgent-CSRF': 'test-csrf'}
    assert client.post(f'/api/card-management/requests/{rid}/approve', headers=headers).status_code == 403
    token = client.post(f'/api/card-management/requests/{rid}/confirmation-token', headers=headers).json()['data']['confirmation_token']
    result = client.post(f'/api/card-management/requests/{rid}/confirm', headers=headers, json={'confirmation_token': token}).json()['data']
    assert result['status'] == 'submitted' and not result['receipt'].get('card_mask')
    directory = app.config.ROOT / 'var'; directory.mkdir(exist_ok=True)
    (directory / 'approver.key').write_text('independent-approver-test-key', encoding='utf-8')
    assert client.post(f'/api/card-management/requests/{rid}/approve', headers=headers | {'X-FinAgent-Operator': 'independent-approver-test-key'}).status_code == 403
    assert client.post(f'/api/card-management/requests/{rid}/approve', headers=headers | {'X-FinAgent-Approver': 'wrong'}).status_code == 403
    headers['X-FinAgent-Approver'] = 'independent-approver-test-key'
    first = client.post(f'/api/card-management/requests/{rid}/approve', headers=headers).json()['data']
    second = client.post(f'/api/card-management/requests/{rid}/approve', headers=headers).json()['data']
    assert first['status'] == 'issued' and first['receipt'] == second['receipt']
    client.cookies.set('finagent_demo_session', 'other')
    assert client.post(f'/api/card-management/requests/{rid}/approve', headers=headers).status_code == 404


def test_model_payload_redacts_known_contacts_accounts_and_auth():
    original = {'birthday_address': '合成测试市梧桐路42号', 'birthday_recipient': '测试收件人',
        'summary': {'address': '合成测试市梧桐路42号', 'account_number': '6222000000000000000'},
        'messages': [{'role': 'user', 'content': '收货人测试收件人，地址合成测试市梧桐路42号，账号6222000000000000000，验证码123456'}]}
    safe = json.dumps(model_payload(original), ensure_ascii=False)
    for private in ['合成测试市梧桐路42号', '测试收件人', '6222000000000000000', '123456']:
        assert private not in safe
    assert original['birthday_address'] == '合成测试市梧桐路42号'
    assert 'DE89370400440532013000' not in model_payload('收款账号DE89370400440532013000，其他信息留在本地')


def test_task_audit_is_scoped_and_contains_rules_and_resource_evidence(bank, client):
    from app.agent.service import create
    db, _ = bank
    result = create(db, db.get(DemoSession, 's1'), 'mock', '从活期账户转100元到储蓄账户')
    client.cookies.set('finagent_demo_session', 's1')
    report = client.get(f"/api/tasks/{result['id']}/audit").json()['data']
    assert report['trace_id'] == result['id']
    assert report['decision']['policy_version'] and report['steps']
    assert report['resources'][0]['id'] == result['proposal']['id']
    assert len(report['steps'][0]['parameters_sha256']) == 64
    assert 'confirmation_token' not in json.dumps(report)
    client.cookies.set('finagent_demo_session', 'other')
    assert client.get(f"/api/tasks/{result['id']}/audit").status_code == 404


def test_actual_subprocess_timeout_terminates_and_reaps_child(tmp_path):
    # This is a real parent timeout/kill/reap, not a mocked TimeoutExpired.
    # It proves subprocess.run's termination path; not a full OS resource sandbox.
    marker = tmp_path / 'should-not-be-written.txt'
    started = perf_counter()
    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run([sys.executable, '-I', '-S', '-c',
            'import time,pathlib; time.sleep(10); pathlib.Path(__import__("sys").argv[1]).write_text("late")', str(marker)],
            timeout=.3, capture_output=True, check=True)
    assert perf_counter() - started < 5 and not marker.exists()


def test_0006_to_head_preserves_recipients_balances_and_historical_receipts(tmp_path, monkeypatch):
    from sqlalchemy.orm import Session
    from app.data.seed import reset_demo, check_ledger
    from app.domain.operations import prepare_transfer, issue_confirmation_token, confirm_proposal
    path = tmp_path / '0006-TEST-ONLY.sqlite3'
    monkeypatch.setenv('FINAGENT_DB_PATH', str(path))
    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend / 'alembic.ini'))
    config.set_main_option('script_location', str(backend / 'migrations'))
    command.upgrade(config, 'head')
    engine = make_engine(path)
    with Session(engine) as db:
        reset_demo(db, date(2026, 9, 24))
        from datetime import datetime, timedelta, timezone
        from app.data.models import DemoSetting
        db.add(DemoSession(id='legacy', owner_id='u1', dataset_version=db.get(DemoSetting, 1).dataset_version,
            expires_at_utc=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat(), csrf_token='legacy'))
        db.commit()
        pid = prepare_transfer(db, session_id='legacy', source_account_id='a1c', target={'type': 'own_account', 'account_id': 'a1s'}, amount_minor=10000)['proposal']['id']
        token = issue_confirmation_token(db, session_id='legacy', proposal_id=pid)['confirmation_token']
        confirm_proposal(db, session_id='legacy', proposal_id=pid, confirmation_token=token)
        balances = db.execute(select(Account.id, Account.balance_minor)).all()
        recipients = db.execute(select(Recipient.id, Recipient.name, Recipient.phone, Recipient.account_last4)).all()
        receipts = db.execute(select(BusinessResult.id, BusinessResult.receipt_json)).all()
        assert receipts
    # Reconstruct the 0006 schema by the project's official reverse migrations.
    # This is not an archived original 0006 database supplied by the user.
    command.downgrade(config, '0006_recipient_phone')
    command.upgrade(config, 'head'); command.upgrade(config, 'head')
    with Session(engine) as db:
        assert db.execute(select(Account.id, Account.balance_minor)).all() == balances
        assert db.execute(select(Recipient.id, Recipient.name, Recipient.phone, Recipient.account_last4)).all() == recipients
        assert db.execute(select(BusinessResult.id, BusinessResult.receipt_json)).all() == receipts
        assert check_ledger(db) == []
    engine.dispose()
