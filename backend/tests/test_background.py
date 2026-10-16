from datetime import datetime, timedelta, timezone
import json
import pytest
from sqlalchemy import func, select
from app.data.models import Account, TransferOrder
from app.data.security_models import SecurityAuthorization
from app.domain import security
from app.domain.scheduled_transfers import prepare_schedule, issue_schedule_confirmation_token, confirm_schedule, _execute_due


def schedule(db, amount=10000):
    due = datetime.now(timezone.utc) + timedelta(minutes=10)
    record = prepare_schedule(db, session_id='s1', source_account_id='a1c',
        target={'type': 'own_account', 'account_id': 'a1s'}, amount_minor=amount,
        execute_at_utc=due.isoformat())['schedule']
    token = issue_schedule_confirmation_token(db, session_id='s1', schedule_id=record['id'])['confirmation_token']
    confirm_schedule(db, session_id='s1', schedule_id=record['id'], confirmation_token=token)
    return record['id'], due


def test_schedule_contract_executes_once(bank):
    db, _ = bank
    sid, due = schedule(db)
    assert _execute_due(db, sid, due)['status'] == 'executed'
    assert _execute_due(db, sid, due)['status'] == 'executed'
    assert db.scalar(select(func.count()).select_from(TransferOrder)) == 1
    grant = db.scalar(select(SecurityAuthorization).where(SecurityAuthorization.resource == 'schedule:' + sid))
    assert grant.remaining == 0


def test_schedule_stops_when_user_locked(bank):
    db, _ = bank
    sid, due = schedule(db)
    for _ in range(5):
        security.failure(db, 'u1', 'MFA_INVALID')
    result = _execute_due(db, sid, due)
    assert result['status'] == 'failed'
    assert result['error_code'] == 'SECURITY_LOCKED'
    assert db.scalar(select(func.count()).select_from(TransferOrder)) == 0


def test_schedule_does_not_upgrade_its_own_authorization(bank):
    from test_security import transfer, confirm
    db, _ = bank
    sid, due = schedule(db, 60000)
    confirm(db, transfer(db, 50000, sid='s2'), sid='s2')
    result = _execute_due(db, sid, due)
    assert result['status'] == 'failed'
    assert result['error_code'] == 'REAUTHORIZATION_REQUIRED'
    assert db.scalar(select(func.count()).select_from(TransferOrder)) == 1


def birthday(db):
    from app.domain.birthdays import prepare, issue_token, confirm
    business = datetime(2026, 10, 5, tzinfo=timezone.utc)
    request = prepare(db, session_id='s1', kind='create', business_now=business,
        terms={'birthday_date': '2026-10-20', 'source_account_id': 'a1c', 'budget_minor': 100000,
               'recipient': '合成收件人', 'address': '合成测试地址', 'item_keys': ['flowers', 'cake']})
    token = issue_token(db, session_id='s1', request_id=request['id'])['confirmation_token']
    result = confirm(db, session_id='s1', request_id=request['id'], confirmation_token=token, business_now=business)
    return result['plan_id']


def test_birthday_locked_before_due_never_pays(bank):
    from app.domain.birthdays import _execute_due as execute_birthday
    from app.data.birthday_models import BirthdayOrder
    db, _ = bank
    pid = birthday(db)
    before = db.get(Account, 'a1c').balance_minor
    for _ in range(5):
        security.failure(db, 'u1', 'MFA_INVALID')
    execute_birthday(db, pid, datetime(2026, 10, 18, 2, tzinfo=timezone.utc))
    assert db.get(Account, 'a1c').balance_minor == before
    assert db.scalar(select(func.count()).select_from(BirthdayOrder)) == 0


def test_birthday_pays_only_once_and_never_invents_delivery(bank):
    from app.domain.birthdays import _execute_due as execute_birthday
    from app.data.birthday_models import BirthdayOrder, BirthdayPlan
    db, _ = bank
    pid = birthday(db)
    due = datetime(2026, 10, 18, 2, tzinfo=timezone.utc)
    execute_birthday(db, pid, due)
    execute_birthday(db, pid, due)
    assert db.get(BirthdayPlan, pid).status == 'awaiting_delivery'
    assert db.scalar(select(func.count()).select_from(BirthdayOrder)) == 2


@pytest.mark.parametrize('locked', [False, True])
def test_subscription_requires_bounded_contract_and_obeys_lock(bank, locked):
    from app.domain.subscriptions import seed_test_authorizations, _charge_due
    from app.data.subscription_models import SubscriptionAuthorization
    db, _ = bank
    seed_test_authorizations(db, owner_id='u1', account_id='a1c')
    db.commit()
    auth = db.scalar(select(SubscriptionAuthorization).where(SubscriptionAuthorization.merchant == '云音乐'))
    if locked:
        security.grant(db, 'u1', 'subscription:' + auth.id, security.subscription_terms(auth), 'yellow',
                       (security.now() + timedelta(days=366)).isoformat(), 12)
        db.commit()
        for _ in range(5):
            security.failure(db, 'u1', 'MFA_INVALID')
    before = db.get(Account, 'a1c').balance_minor
    result = _charge_due(db, auth.id, datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert result['status'] == 'failed'
    assert result['error_code'] == ('SECURITY_LOCKED' if locked else 'REAUTHORIZATION_REQUIRED')
    assert db.get(Account, 'a1c').balance_minor == before
