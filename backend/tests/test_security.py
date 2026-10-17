from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import func, select
import app.config
from app.data.models import Account, Proposal, TransferOrder, Transaction
from app.data.security_models import SecurityChallenge, SecurityState
from app.domain.errors import DomainError
from app.domain.operations import prepare_transfer, issue_confirmation_token, confirm_proposal, cancel_proposal
from app.domain import security


def transfer(db, amount=10000, sid='s1', source='a1c'):
    return prepare_transfer(db, session_id=sid, source_account_id=source,
        target={'type': 'own_account', 'account_id': 'a1s' if source == 'a1c' else 'a1c'}, amount_minor=amount)['proposal']['id']


def confirm(db, pid, sid='s1'):
    token = issue_confirmation_token(db, session_id=sid, proposal_id=pid)['confirmation_token']
    return confirm_proposal(db, session_id=sid, proposal_id=pid, confirmation_token=token)


def mfa(db, pid, sid='s1', kind='proposal'):
    issued = security.start_challenge(db, sid, kind, pid)
    delivered = json.loads((app.config.ROOT / 'var' / 'auth-simulator' / (issued['challenge_id'] + '.json')).read_text())
    security.verify_challenge(db, sid, kind, pid, issued['challenge_id'], delivered['code'])
    return issued, delivered


@pytest.mark.parametrize('amount,level', [(99999, 'yellow'), (100000, 'yellow'), (100001, 'red')])
def test_thresholds(bank, amount, level):
    db, _ = bank
    pid = transfer(db, amount)
    assert security.describe(db, 's1', 'proposal', pid)['level'] == level
    if level == 'red':
        with pytest.raises(DomainError, match='红色'):
            confirm(db, pid)
        mfa(db, pid)
    token = issue_confirmation_token(db, session_id='s1', proposal_id=pid)['confirmation_token']
    first = confirm_proposal(db, session_id='s1', proposal_id=pid, confirmation_token=token)
    second = confirm_proposal(db, session_id='s1', proposal_id=pid, confirmation_token=token)
    assert first['status'] == second['status'] == 'executed'
    assert first['order_id'] == second['order_id']
    with pytest.raises(DomainError):
        issue_confirmation_token(db, session_id='s1', proposal_id=pid)
    assert db.scalar(select(func.count()).select_from(TransferOrder)) == 1


def test_600_then_500_cross_account_and_session(bank):
    db, _ = bank
    confirm(db, transfer(db, 60000))
    pid = transfer(db, 50000, sid='s2', source='a1s')
    assert security.describe(db, 's2', 'proposal', pid)['including_this_minor'] == 110000
    with pytest.raises(DomainError) as error:
        confirm(db, pid, sid='s2')
    assert error.value.code == 'MFA_REQUIRED'


def test_concurrent_yellow_drafts_recheck_in_writer_transaction(bank):
    db, factory = bank
    first = transfer(db, 60000, source='a1c')
    second = transfer(db, 60000, sid='s2', source='a1s')
    def execute_one(item):
        with factory() as connection:
            try:
                confirm(connection, item[0], item[1])
                return 'executed'
            except DomainError as error:
                return error.code
    # Whichever wins, the other must be rejected (red upgrade or stale funds).
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(execute_one, [(first, 's1'), (second, 's2')]))
    assert statuses.count('executed') == 1
    assert statuses.count('MFA_REQUIRED') == 1


def test_plain_token_cannot_execute_red(bank):
    db, _ = bank
    pid = transfer(db, 200000)
    before = db.get(Account, 'a1c').balance_minor
    with pytest.raises(DomainError) as error:
        confirm(db, pid)
    assert error.value.code == 'MFA_REQUIRED'
    assert db.get(Account, 'a1c').balance_minor == before


def test_mfa_wrong_expired_replayed_and_bound_to_operation(bank):
    db, _ = bank
    pid = transfer(db, 200000)
    issued = security.start_challenge(db, 's1', 'proposal', pid)
    with pytest.raises(DomainError):
        security.verify_challenge(db, 's1', 'proposal', pid, issued['challenge_id'], 'wrong')
    mfa(db, pid)
    confirm(db, pid)
    with pytest.raises(DomainError):
        security.verify_challenge(db, 's1', 'proposal', pid, issued['challenge_id'], '000000')


def test_expired_proof(bank):
    db, _ = bank
    pid = transfer(db, 200000)
    issued, _ = mfa(db, pid)
    proof = db.get(SecurityChallenge, issued['challenge_id'])
    proof.expires_at = (security.now() - timedelta(seconds=1)).isoformat()
    db.commit()
    with pytest.raises(DomainError) as error:
        confirm(db, pid)
    assert error.value.code == 'MFA_REQUIRED'


def test_changed_terms_and_cross_user_rejected(bank):
    db, _ = bank
    pid = transfer(db, 200000)
    mfa(db, pid)
    with pytest.raises(DomainError) as error:
        security.describe(db, 'other', 'proposal', pid)
    assert error.value.code == 'RESOURCE_NOT_FOUND'
    row = db.get(Proposal, pid)
    params = json.loads(row.params_json)
    params['amount_minor'] = 210000
    row.params_json = json.dumps(params)
    db.commit()
    with pytest.raises(DomainError) as error:
        confirm(db, pid)
    assert error.value.code == 'MFA_REQUIRED'


def test_five_failures_persist_across_sessions_and_allow_cancel(bank):
    db, factory = bank
    pid = transfer(db)
    for _ in range(5):
        with pytest.raises(DomainError):
            confirm_proposal(db, session_id='s1', proposal_id=pid, confirmation_token='wrong')
    with factory() as connection:
        with pytest.raises(DomainError) as error:
            security.assert_unlocked(connection, 'u1')
        assert error.value.code == 'SECURITY_LOCKED'
        security.assert_unlocked(connection, 'u2')
    assert cancel_proposal(db, session_id='s1', proposal_id=pid)['status'] == 'cancelled'
    assert db.get(SecurityState, 'u1').failures == 5


def test_subscription_query_is_yellow(bank):
    from app.domain.subscriptions import get_portfolio
    db, _ = bank
    with pytest.raises(DomainError) as error:
        get_portfolio(db, 's1')
    assert error.value.code == 'SUBSCRIPTION_CONFIRMATION_REQUIRED'
    security.confirm_subscription_access(db, 's1')
    assert 'authorizations' in get_portfolio(db, 's1')
    with pytest.raises(DomainError):
        get_portfolio(db, 's2')


def test_small_wealth_purchase_is_red(bank):
    from app.domain.wealth import submit_assessment, prepare_order, issue_token, confirm_order
    db, _ = bank
    submit_assessment(db, session_id='s1', answers=[2, 2, 2, 2])
    order = prepare_order(db, session_id='s1', kind='buy', product_id='P1', account_id='a1c', amount_minor=10000)
    token = issue_token(db, session_id='s1', order_id=order['id'])['confirmation_token']
    with pytest.raises(DomainError) as error:
        confirm_order(db, session_id='s1', order_id=order['id'], confirmation_token=token)
    assert error.value.code == 'MFA_REQUIRED'
    mfa(db, order['id'], kind='wealth')
    assert confirm_order(db, session_id='s1', order_id=order['id'], confirmation_token=token)['status'] == 'settled'


def test_card_loss_is_red(bank):
    from app.domain.card_management import prepare, issue_token, confirm as confirm_card
    db, _ = bank
    request = prepare(db, session_id='s1', kind='loss', card_id='c1a')
    token = issue_token(db, session_id='s1', request_id=request['id'])['confirmation_token']
    with pytest.raises(DomainError) as error:
        confirm_card(db, session_id='s1', request_id=request['id'], confirmation_token=token)
    assert error.value.code == 'MFA_REQUIRED'
    mfa(db, request['id'], kind='card')
    assert confirm_card(db, session_id='s1', request_id=request['id'], confirmation_token=token)['status'] == 'executed'


def test_shanghai_security_day_counts_debits_once_and_never_nets_refunds(bank, monkeypatch):
    db, _ = bank
    stamp = datetime(2026, 10, 15, 15, 59, 59, tzinfo=timezone.utc)
    for identifier in ('t_internal_out', 't_internal_in', 't_refund_50'):
        db.get(Transaction, identifier).created_at_utc = stamp.isoformat()
    db.commit()
    monkeypatch.setattr(security, 'now', lambda: stamp)
    assert security.daily_total(db, 'u1') == 50000
    assert security.daily_total(db, 'u2') == 0
    monkeypatch.setattr(security, 'now', lambda: stamp + timedelta(seconds=1))
    assert security.daily_total(db, 'u1') == 0


def test_cooldown_allows_new_verification_and_resets_old_failures(bank, monkeypatch):
    db, _ = bank
    for _ in range(5):
        security.failure(db, 'u1', 'MFA_INVALID')
    locked = db.get(SecurityState, 'u1')
    later = datetime.fromisoformat(locked.locked_until) + timedelta(seconds=1)
    monkeypatch.setattr(security, 'now', lambda: later)
    security.assert_unlocked(db, 'u1')
    security.failure(db, 'u1', 'MFA_INVALID')
    assert locked.failures == 1
    assert locked.locked_until is None


def test_failed_snapshot_is_persisted_without_debit(bank):
    db, _ = bank
    pid = transfer(db)
    token = issue_confirmation_token(db, session_id='s1', proposal_id=pid)['confirmation_token']
    before = db.get(Account, 'a1c').balance_minor
    db.get(Account, 'a1c').version += 1
    db.commit()
    with pytest.raises(DomainError) as error:
        confirm_proposal(db, session_id='s1', proposal_id=pid, confirmation_token=token)
    assert error.value.code == 'STALE_RESOURCE'
    assert db.get(Proposal, pid).status == 'invalidated'
    assert db.get(Account, 'a1c').balance_minor == before
    assert db.scalar(select(func.count()).select_from(TransferOrder)) == 0


def test_simulated_sms_is_bound_to_pending_operation_and_session(bank):
    db, _ = bank
    pid = transfer(db, 200000)
    issued = security.start_challenge(db, 's1', 'proposal', pid)
    sms = security.read_simulated_sms(db, 's1', 'proposal', pid, issued['challenge_id'])
    assert len(sms['code']) == 6 and sms['code'].isdigit()
    assert db.get(SecurityChallenge, issued['challenge_id']).status == 'pending'
    with pytest.raises(DomainError):
        confirm(db, pid)
    for sid in ['s2', 'other']:
        with pytest.raises(DomainError):
            security.read_simulated_sms(db, sid, 'proposal', pid, issued['challenge_id'])
    with pytest.raises(DomainError):
        security.read_simulated_sms(db, 's1', 'proposal', 'another-operation', issued['challenge_id'])
    security.start_challenge(db, 's1', 'proposal', pid)
    with pytest.raises(DomainError, match='失效'):
        security.read_simulated_sms(db, 's1', 'proposal', pid, issued['challenge_id'])


@pytest.mark.parametrize('invalid', ['expired', 'cancelled', 'locked', 'missing_delivery', 'tampered_delivery'])
def test_simulated_sms_rejects_invalid_delivery_or_state(bank, invalid):
    db, _ = bank
    pid = transfer(db, 200000)
    issued = security.start_challenge(db, 's1', 'proposal', pid)
    inbox = app.config.ROOT / 'var' / 'auth-simulator' / (issued['challenge_id'] + '.json')
    if invalid == 'expired':
        db.get(SecurityChallenge, issued['challenge_id']).expires_at = (security.now() - timedelta(seconds=1)).isoformat()
        db.commit()
    elif invalid == 'cancelled':
        cancel_proposal(db, session_id='s1', proposal_id=pid)
    elif invalid == 'locked':
        db.add(SecurityState(owner_id='u1', failures=5, locked_until=(security.now() + timedelta(minutes=15)).isoformat()))
        db.commit()
    elif invalid == 'missing_delivery':
        inbox.unlink()
    else:
        inbox.write_text(json.dumps({'challenge_id': issued['challenge_id'], 'code': '1234567'}), encoding='utf-8')
    with pytest.raises(DomainError):
        security.read_simulated_sms(db, 's1', 'proposal', pid, issued['challenge_id'])
