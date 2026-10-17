"""Frozen accounts reject every debit path while keeping recovery and credits."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.data.atomic import atomic_write
from app.data.models import Account, Card, TransferOrder, Transaction
from app.data.birthday_models import BirthdayOrder, BirthdayPlan, BirthdayReservation
from app.data.security_models import SecurityAuthorization
from app.data.subscription_models import SubscriptionAuthorization
from app.domain import birthdays, card_management, security, subscriptions, wealth
from app.domain.directory import AddCard, add_card
from app.domain.errors import DomainError
from app.domain.ledger import account_restrictions, post_entry, post_refund
from app.domain.operations import (prepare_card_action, prepare_transfer, issue_confirmation_token,
                                   confirm_proposal, get_proposal)
from app.main import accounts
from test_background import birthday, schedule
from test_security import mfa


def card_action(db, card='c1a', action='freeze', sid='s2'):
    pid = prepare_card_action(db, session_id=sid, card_id=card, action=action)['proposal']['id']
    if action == 'unfreeze':
        mfa(db, pid, sid=sid)
    return confirm(db, pid, sid)


def confirm(db, pid, sid='s1'):
    token = issue_confirmation_token(db, session_id=sid, proposal_id=pid)['confirmation_token']
    return confirm_proposal(db, session_id=sid, proposal_id=pid, confirmation_token=token)


@pytest.mark.parametrize('target', [
    {'type': 'own_account', 'account_id': 'a1s'},
    {'type': 'saved_recipient', 'recipient_id': 'r3'},
    {'type': 'external_account', 'name': '合成收款人', 'bank_name': '合成银行', 'account_number': '6222000000007788'},
])
def test_frozen_source_rejects_all_transfer_targets(bank, target):
    db, _ = bank
    card_action(db)
    before = db.get(Account, 'a1c').balance_minor
    with pytest.raises(DomainError) as error:
        prepare_transfer(db, session_id='s1', source_account_id='a1c', target=target, amount_minor=1000)
    assert error.value.code == 'ACCOUNT_FROZEN'
    assert db.get(Account, 'a1c').balance_minor == before
    assert db.scalar(select(func.count()).select_from(TransferOrder)) == 0


@pytest.mark.parametrize('legacy_freeze', [False, True])
def test_prepared_transfer_and_token_cannot_bypass_a_later_freeze(bank, legacy_freeze):
    db, _ = bank
    pid = prepare_transfer(db, session_id='s1', source_account_id='a1c',
        target={'type': 'own_account', 'account_id': 'a1s'}, amount_minor=1000)['proposal']['id']
    token = issue_confirmation_token(db, session_id='s1', proposal_id=pid)['confirmation_token']
    before = db.get(Account, 'a1c').balance_minor
    if legacy_freeze:
        # Older freezes did not change Account.version. They must still block
        # a cached proposal whose account snapshot continues to match.
        with atomic_write(db):
            db.get(Card, 'c1a').status = 'frozen'
            db.get(Card, 'c1a').version += 1
    else:
        card_action(db)
    with pytest.raises(DomainError) as error:
        confirm_proposal(db, session_id='s1', proposal_id=pid, confirmation_token=token)
    assert error.value.code == 'ACCOUNT_FROZEN'
    assert get_proposal(db, session_id='s1', proposal_id=pid)['status'] == 'failed'
    assert db.get(Account, 'a1c').balance_minor == before
    assert db.scalar(select(func.count()).select_from(TransferOrder)) == 0


def test_freeze_then_unfreeze_does_not_revive_old_transfer_authority(bank):
    db, _ = bank
    pid = prepare_transfer(db, session_id='s1', source_account_id='a1c',
        target={'type': 'own_account', 'account_id': 'a1s'}, amount_minor=1000)['proposal']['id']
    token = issue_confirmation_token(db, session_id='s1', proposal_id=pid)['confirmation_token']
    card_action(db); card_action(db, action='unfreeze')
    with pytest.raises(DomainError) as error:
        confirm_proposal(db, session_id='s1', proposal_id=pid, confirmation_token=token)
    assert error.value.code == 'STALE_RESOURCE'
    fresh = prepare_transfer(db, session_id='s1', source_account_id='a1c',
        target={'type': 'own_account', 'account_id': 'a1s'}, amount_minor=1000)['proposal']['id']
    assert confirm(db, fresh)['status'] == 'executed'


@pytest.mark.parametrize('kind', ['purchase', 'internal_transfer', 'external_transfer', 'wealth_buy'])
def test_final_ledger_gate_rejects_debits_without_a_card_identifier(bank, kind):
    db, _ = bank
    card_action(db)
    before = db.get(Account, 'a1c').balance_minor
    count = db.scalar(select(func.count()).select_from(Transaction))
    with pytest.raises(DomainError) as error:
        with atomic_write(db):
            post_entry(db, owner_id='u1', account_id='a1c', kind=kind, direction='debit',
                       amount_minor=1000, business_at=datetime.now(timezone.utc))
    assert error.value.code == 'ACCOUNT_FROZEN'
    assert db.get(Account, 'a1c').balance_minor == before
    assert db.scalar(select(func.count()).select_from(Transaction)) == count


def test_frozen_account_reports_zero_spendable_but_can_receive_transfers_and_refunds(bank):
    db, _ = bank
    card_action(db)
    before = db.get(Account, 'a1c').balance_minor
    frozen = next(row for row in accounts(owner_id='u1', db=db)['data'] if row['id'] == 'a1c')
    assert frozen['status'] == 'frozen' and frozen['debit_blocked']
    assert frozen['available_balance_minor'] == 0 and frozen['balance_minor'] == before
    pid = prepare_transfer(db, session_id='s1', source_account_id='a1s',
        target={'type': 'own_account', 'account_id': 'a1c'}, amount_minor=1000)['proposal']['id']
    confirm(db, pid)
    with atomic_write(db):
        post_refund(db, owner_id='u1', original_id='t_food_1000', amount_minor=500,
                    business_at=datetime.now(timezone.utc))
    assert db.get(Account, 'a1c').balance_minor == before + 1500
    assert account_restrictions(db, db.get(Account, 'a1c'))['status'] == 'frozen'


def test_another_or_newly_added_card_cannot_bypass_account_freeze(bank):
    db, _ = bank
    card_action(db)
    new = add_card(db, 's1', AddCard(alias='副卡', bank_name='合成银行', card_number='6222000000008899', account_id='a1c'))
    result = card_management.payment(db, session_id='s1', card_id=new['id'], amount_minor=1000,
                                    channel='offline', idempotency_key='frozen-account-other-card')
    assert result['status'] == 'rejected' and result['error_code'] == 'ACCOUNT_FROZEN'
    card_action(db, card=new['id'])
    card_action(db, action='unfreeze')
    assert account_restrictions(db, db.get(Account, 'a1c'))['status'] == 'frozen'
    card_action(db, card=new['id'], action='unfreeze')
    assert account_restrictions(db, db.get(Account, 'a1c'))['status'] == 'active'
    result = card_management.payment(db, session_id='s1', card_id=new['id'], amount_minor=1000,
                                    channel='offline', idempotency_key='unfrozen-account-other-card')
    assert result['status'] == 'posted'


def test_frozen_account_rejects_new_and_existing_wealth_buys(bank):
    db, _ = bank
    wealth.submit_assessment(db, session_id='s1', answers=[0, 0, 0, 0])
    order = wealth.prepare_order(db, session_id='s1', kind='buy', product_id='P1', account_id='a1c', amount_minor=10000)
    mfa(db, order['id'], kind='wealth')
    token = wealth.issue_token(db, session_id='s1', order_id=order['id'])['confirmation_token']
    card_action(db)
    with pytest.raises(DomainError) as error:
        wealth.confirm_order(db, session_id='s1', order_id=order['id'], confirmation_token=token)
    assert error.value.code == 'ACCOUNT_FROZEN'
    with pytest.raises(DomainError) as error:
        wealth.prepare_order(db, session_id='s2', kind='buy', product_id='P1', account_id='a1c', amount_minor=10000)
    assert error.value.code == 'ACCOUNT_FROZEN'


def test_previously_authorized_schedule_cannot_pay_after_freeze(bank):
    from app.domain.scheduled_transfers import _execute_due
    db, _ = bank
    sid, due = schedule(db)
    before = db.get(Account, 'a1c').balance_minor
    card_action(db)
    result = _execute_due(db, sid, due)
    assert result['status'] == 'failed' and result['error_code'] == 'ACCOUNT_FROZEN'
    assert db.get(Account, 'a1c').balance_minor == before
    assert db.scalar(select(func.count()).select_from(TransferOrder)) == 0


def test_previously_authorized_subscription_cannot_pay_after_freeze(bank):
    db, _ = bank
    subscriptions.seed_test_authorizations(db, owner_id='u1', account_id='a1c'); db.commit()
    auth = db.scalar(select(SubscriptionAuthorization).where(SubscriptionAuthorization.merchant == '云音乐'))
    security.grant(db, 'u1', 'subscription:' + auth.id, security.subscription_terms(auth), 'yellow',
                   (security.now() + timedelta(days=366)).isoformat(), 12); db.commit()
    before = db.get(Account, 'a1c').balance_minor
    card_action(db)
    result = subscriptions._charge_due(db, auth.id, datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert result['status'] == 'failed' and result['error_code'] == 'ACCOUNT_FROZEN'
    assert db.get(Account, 'a1c').balance_minor == before
    grant = db.scalar(select(SecurityAuthorization).where(SecurityAuthorization.resource == 'subscription:' + auth.id))
    assert grant.remaining == 12


def test_frozen_birthday_plan_never_orders_or_pays_but_can_cancel_and_release_reservation(bank):
    db, _ = bank
    plan_id = birthday(db)
    before = db.get(Account, 'a1c').balance_minor
    card_action(db)
    result = birthdays._execute_due(db, plan_id, datetime(2026, 10, 18, 2, tzinfo=timezone.utc))
    assert result['status'] == 'blocked' and db.get(BirthdayPlan, plan_id).error_code == 'ACCOUNT_FROZEN'
    assert db.get(Account, 'a1c').balance_minor == before
    assert db.scalar(select(func.count()).select_from(BirthdayOrder)) == 0
    request = birthdays.prepare(db, session_id='s1', kind='cancel', plan_id=plan_id, terms={},
                                business_now=datetime(2026, 10, 18, 2, tzinfo=timezone.utc))
    token = birthdays.issue_token(db, session_id='s1', request_id=request['id'])['confirmation_token']
    birthdays.confirm(db, session_id='s1', request_id=request['id'], confirmation_token=token,
                      business_now=datetime(2026, 10, 18, 2, tzinfo=timezone.utc))
    hold = db.scalar(select(BirthdayReservation).where(BirthdayReservation.plan_id == plan_id))
    assert hold.remaining_minor == 0 and hold.status == 'released'
    assert account_restrictions(db, db.get(Account, 'a1c'))['status'] == 'frozen'


def test_frozen_account_can_receive_wealth_redemption(bank):
    db, _ = bank
    wealth.submit_assessment(db, session_id='s1', answers=[0, 0, 0, 0])
    bought = wealth.prepare_order(db, session_id='s1', kind='buy', product_id='P1', account_id='a1c', amount_minor=10000)
    mfa(db, bought['id'], kind='wealth')
    token = wealth.issue_token(db, session_id='s1', order_id=bought['id'])['confirmation_token']
    wealth.confirm_order(db, session_id='s1', order_id=bought['id'], confirmation_token=token)
    card_action(db)
    before = db.get(Account, 'a1c').balance_minor
    redeem = wealth.prepare_order(db, session_id='s1', kind='redeem', product_id='P1', account_id='a1c', units_hundredths=10000)
    token = wealth.issue_token(db, session_id='s1', order_id=redeem['id'])['confirmation_token']
    result = wealth.confirm_order(db, session_id='s1', order_id=redeem['id'], confirmation_token=token)
    assert result['status'] == 'settled'
    assert db.get(Account, 'a1c').balance_minor == before + 10000
    assert account_restrictions(db, db.get(Account, 'a1c'))['status'] == 'frozen'


def test_transfer_waiting_for_freeze_commit_observes_new_restriction(bank):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    db, factory = bank
    pid = prepare_card_action(db, session_id='s2', card_id='c1a', action='freeze')['proposal']['id']
    token = issue_confirmation_token(db, session_id='s2', proposal_id=pid)['confirmation_token']
    holding, release, trying = Event(), Event(), Event()

    def before_commit():
        holding.set()
        assert release.wait(10), 'Test did not release freeze transaction'

    def freeze():
        with factory() as connection:
            return confirm_proposal(connection, session_id='s2', proposal_id=pid, confirmation_token=token, before_commit=before_commit)

    def transfer():
        with factory() as connection:
            trying.set()
            try:
                prepare_transfer(connection, session_id='s1', source_account_id='a1c',
                                 target={'type': 'own_account', 'account_id': 'a1s'}, amount_minor=1000)
            except DomainError as error:
                return error.code
            return 'unexpectedly_prepared'

    with ThreadPoolExecutor(max_workers=2) as pool:
        frozen = pool.submit(freeze)
        try:
            assert holding.wait(10)
            waiting = pool.submit(transfer)
            assert trying.wait(10)
        finally:
            release.set()
        assert frozen.result(timeout=15)['status'] == 'executed'
        assert waiting.result(timeout=15) == 'ACCOUNT_FROZEN'
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(TransferOrder)) == 0
