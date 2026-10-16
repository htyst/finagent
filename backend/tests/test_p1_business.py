"""P1 boundary, race, restart and conservation checks on isolated ledgers."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select

from app.data.models import Account, Card, DataCoverage, DemoSession, Recipient, TransferOrder, Transaction
from app.data.birthday_models import BirthdayOrder, BirthdayPlan, BirthdayReservation
from app.data.collection_models import CollectionPayment
from app.data.subscription_models import SubscriptionAuthorization, SubscriptionCharge, SubscriptionNotification, SubscriptionReminder
from app.data.wealth_models import RiskAssessment, WealthPosition
from app.domain import birthdays, card_management as cards, collections, security, subscriptions, wealth
from app.domain.errors import DomainError
from app.domain.ledger import available_balance, post_entry, post_refund
from app.domain.operations import prepare_transfer
from app.domain.scheduled_transfers import _execute_due, cancel_schedule, run_due_schedules
from app.domain.statement import generate_statement, inspect_anomalies
from app.domain.spending import summarize_spending
from test_background import birthday, schedule
from test_security import mfa

UTC = timezone.utc
BUSINESS = datetime(2026, 10, 5, tzinfo=UTC)
DUE = datetime(2026, 10, 18, 2, tzinfo=UTC)


def settle(db, module, request, *, kind=None, business_now=None):
    rid = request['id']
    if kind:
        mfa(db, rid, kind=kind)
    token = module.issue_token(db, session_id='s1', **({'order_id': rid} if module is wealth else {'request_id': rid}))['confirmation_token']
    params = {'session_id': 's1', 'confirmation_token': token, **({'order_id': rid} if module is wealth else {'request_id': rid})}
    if business_now is not None: params['business_now'] = business_now
    return module.confirm(db, **params)


def parallel(factory, functions):
    def run(fn):
        with factory() as connection:
            try: return fn(connection)
            except DomainError as error: return {'error_code': error.code}
    with ThreadPoolExecutor(max_workers=2) as executor:
        return list(executor.map(run, functions))


@pytest.mark.parametrize('target,amount,source', [
    ({'type': 'saved_recipient', 'recipient_id': 'r5'}, 10000, 'a1c'),
    ({'type': 'saved_recipient', 'recipient_id': 'r4'}, 10000, 'a1c'),
    ({'type': 'own_account', 'account_id': 'a2c'}, 10000, 'a1c'),
    ({'type': 'own_account', 'account_id': 'a1s'}, 500001, 'a1c'),
    ({'type': 'own_account', 'account_id': 'a1s'}, 0, 'a1c'),
])
def test_transfer_rejections_do_not_post(bank, target, amount, source):
    db, _ = bank
    before = db.scalar(select(func.count()).select_from(Transaction))
    with pytest.raises(DomainError):
        prepare_transfer(db, session_id='s1', source_account_id=source, target=target, amount_minor=amount)
    assert db.scalar(select(func.count()).select_from(Transaction)) == before


def test_unknown_phone_and_ambiguous_name_do_not_guess(bank):
    from app.agent.service import create, cancel
    db, _ = bank
    session = db.get(DemoSession, 's1')
    for text in ['从活期账户转100元给张三', '从活期账户转100元给13999999999']:
        result = create(db, session, 'mock', text)
        assert result['status'] == 'gathering_info' and result['proposal'] is None
        cancel(db, session, result['id'])
    assert db.scalar(select(func.count()).select_from(TransferOrder)) == 0


@pytest.mark.parametrize('failure', ['insufficient', 'target_changed', 'cancelled'])
def test_schedule_restart_and_failure_never_invents_receipt(bank, failure):
    db, factory = bank
    sid, due = schedule(db)
    if failure == 'insufficient':
        post_entry(db, owner_id='u1', account_id='a1c', kind='purchase', direction='debit', amount_minor=995000, business_at=BUSINESS)
    elif failure == 'target_changed': db.get(Account, 'a1s').account_last4 = '9999'
    else: cancel_schedule(db, session_id='s1', schedule_id=sid)
    db.commit()
    # New sessions simulate reloading all state after service/worker restart.
    for _ in range(2): run_due_schedules(factory, now=due)
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(TransferOrder)) == 0


def test_schedule_cancel_execution_race_and_multiple_workers(bank):
    db, factory = bank
    sid, due = schedule(db)
    db.commit()
    parallel(factory, [lambda c: cancel_schedule(c, session_id='s1', schedule_id=sid), lambda c: _execute_due(c, sid, due)])
    run_due_schedules(factory, now=due)
    run_due_schedules(factory, now=due)
    db.expire_all()
    from app.data.scheduled_models import ScheduledTransfer
    row = db.get(ScheduledTransfer, sid)
    count = db.scalar(select(func.count()).select_from(TransferOrder))
    assert (row.status == 'cancelled' and count == 0) or (row.status == 'executed' and count == 1 and row.receipt_json)


def launch_collection(db):
    request = collections.prepare_collection(db, session_id='s1', destination_account_id='a1c', total_minor=10001, recipient_ids=['r3', 'r_phone_demo'])
    rid = request['id']
    token = collections.issue_collection_confirmation_token(db, session_id='s1', collection_id=rid)['confirmation_token']
    return collections.confirm_collection(db, session_id='s1', collection_id=rid, confirmation_token=token)


def payment_args(db, record, member):
    token = collections.issue_member_payment_token(db, session_id='s1', collection_id=record['id'], member_id=member['id'])['payment_token']
    return dict(session_id='s1', collection_id=record['id'], member_id=member['id'], payment_token=token, expected_amount_minor=member['share_minor'], simulated=True)


def test_aa_all_paid_replayed_and_completed_cannot_be_recalled(bank):
    db, _ = bank
    record = launch_collection(db)
    before = db.get(Account, 'a1c').balance_minor
    total = 0
    for member in record['members']:
        if member['status'] == 'self_share': continue
        args = payment_args(db, record, member)
        first = collections.simulate_member_payment(db, **args)
        replay = collections.simulate_member_payment(db, **args)
        assert first['receipt']['id'] == replay['receipt']['id'] and replay['replayed']
        total += member['share_minor']
    assert replay['collection']['status'] == 'completed'
    assert db.get(Account, 'a1c').balance_minor == before + total
    assert collections.collection_ledger_errors(db) == []
    with pytest.raises(DomainError, match='不能撤销'):
        collections.cancel_collection(db, session_id='s1', collection_id=record['id'])


def test_aa_invalid_amount_and_cancel_payment_race(bank):
    db, factory = bank
    record = launch_collection(db)
    member = next(m for m in record['members'] if m['status'] == 'unpaid')
    args = payment_args(db, record, member)
    for changed in [{'expected_amount_minor': 1}, {'simulated': False}, {'session_id': 'other'}]:
        with pytest.raises(DomainError): collections.simulate_member_payment(db, **(args | changed))
    db.commit()
    parallel(factory, [lambda c: collections.cancel_collection(c, session_id='s1', collection_id=record['id']),
                       lambda c: collections.simulate_member_payment(c, **args)])
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(CollectionPayment)) <= 1
    assert collections.collection_ledger_errors(db) == []


def full_coverage(db, year):
    db.execute(delete(DataCoverage).where(DataCoverage.owner_id == 'u1'))
    from app.domain.clock import local_midnight_utc
    from datetime import date
    for account in ('a1c', 'a1s'):
        db.add(DataCoverage(owner_id='u1', account_id=account, start_utc=local_midnight_utc(date(year, 1, 1)), end_utc=local_midnight_utc(date(year + 1, 1, 1)), completeness='complete'))
    db.commit()


def test_annual_missing_months_are_not_zero_and_complete_zero_months_tie(bank):
    db, _ = bank
    args = dict(owner_id='u1', report_kind='annual', start='2025-01-01', end='2025-12-31')
    missing = generate_statement(db, **args)
    assert len(missing['missing_months']) == 12 and all(m['totals'] is None for m in missing['months'])
    assert missing['highest_months'] == []
    full_coverage(db, 2025)
    complete = generate_statement(db, **args)
    assert len(complete['highest_months']) == 12
    assert all(m['totals']['net_minor'] == 0 for m in complete['months'])


def test_refund_over_current_month_spending_keeps_negative_net_and_evidence(bank):
    db, _ = bank
    full_coverage(db, 2025)
    original = post_entry(db, owner_id='u1', account_id='a1c', kind='purchase', direction='debit', amount_minor=2000, business_at=datetime(2024, 12, 20, tzinfo=UTC), category='购物')
    post_refund(db, owner_id='u1', original_id=original.id, amount_minor=2000, business_at=datetime(2025, 1, 2, tzinfo=UTC))
    db.commit()
    result = summarize_spending(db, owner_id='u1', start='2025-01-01', end='2025-01-31')['current']
    assert result['totals']['net_minor'] == -2000
    assert sum(row['signed_minor'] for row in result['evidence']['items']) == -2000
    assert result['totals']['is_complete']


def test_custom_cross_year_and_anomaly_conclusions(bank):
    db, _ = bank
    result = summarize_spending(db, owner_id='u1', start='2025-12-25', end='2026-01-05')
    assert result['current']['period']['start_date'] == '2025-12-25'
    assert not result['current']['totals']['is_complete']
    report = inspect_anomalies(db, owner_id='u1', start='2026-09-01', end='2026-09-24')
    assert '未标记也不代表绝对安全' in report['conclusion'] and '尚未确定' in report['conclusion']
    assert all(db.get(Transaction, row['transaction_id']).amount_minor == row['amount_minor'] for row in report['candidates'])


def test_every_annual_month_total_and_category_has_ledger_evidence(bank):
    db, _ = bank
    report = generate_statement(db, owner_id='u1', report_kind='annual', start='2026-01-01', end='2026-12-31')
    for month in report['months']:
        rows = db.scalars(select(Transaction).where(Transaction.owner_id=='u1', Transaction.status=='posted',
            Transaction.type.in_(['purchase','refund']),Transaction.posted_at_utc>=month['period']['start_utc'],
            Transaction.posted_at_utc<month['period']['end_utc'])).all()
        purchase=sum(r.amount_minor for r in rows if r.type=='purchase')
        refunds=sum(r.amount_minor for r in rows if r.type=='refund')
        assert month['observed_totals']['purchase_minor']==purchase
        assert month['observed_totals']['refund_minor']==refunds
        assert month['observed_totals']['net_minor']==purchase-refunds
        assert sum(c['net_minor'] for c in month['categories'])==purchase-refunds
        for category in month['categories']:
            assert category['net_minor']==sum(r.amount_minor*(1 if r.type=='purchase' else -1) for r in rows if (r.category or '未分类')==category['category'])


@pytest.mark.parametrize('case,code', [('risk', 'RISK_MISMATCH'), ('expired', 'RISK_ASSESSMENT_REQUIRED'), ('minimum', 'BELOW_MINIMUM'), ('holding', 'INSUFFICIENT_HOLDING'), ('locked', 'PRODUCT_LOCKED')])
def test_wealth_suitability_and_redemption_rejections(bank, case, code):
    db, _ = bank
    wealth.submit_assessment(db, session_id='s1', answers=[0, 0, 0, 0] if case == 'risk' else [2, 2, 2, 2])
    if case == 'expired':
        db.scalar(select(RiskAssessment).where(RiskAssessment.owner_id == 'u1')).expires_at_utc = (security.now() - timedelta(seconds=1)).isoformat(); db.commit()
    if case == 'locked':
        db.add(WealthPosition(id='test-position', owner_id='u1', product_id='P2', units_hundredths=100000, acquired_at_utc=security.now().isoformat(), version=1)); db.commit()
    params = dict(kind='redeem', product_id='P2' if case == 'locked' else 'P1', units_hundredths=10000) if case in ('holding', 'locked') else dict(kind='buy', product_id='P3' if case == 'risk' else 'P1', amount_minor=1 if case == 'minimum' else 10000)
    with pytest.raises(DomainError) as error: wealth.prepare_order(db, session_id='s1', account_id='a1c', **params)
    assert error.value.code == code


def test_assessment_change_invalidates_draft_and_buy_replay_conserves_units(bank):
    db, _ = bank
    wealth.submit_assessment(db, session_id='s1', answers=[2, 2, 2, 2])
    request = wealth.prepare_order(db, session_id='s1', kind='buy', product_id='P1', account_id='a1c', amount_minor=10000)
    mfa(db, request['id'], kind='wealth')
    token = wealth.issue_token(db, session_id='s1', order_id=request['id'])['confirmation_token']
    args = dict(session_id='s1', order_id=request['id'], confirmation_token=token)
    first, second = wealth.confirm_order(db, **args), wealth.confirm_order(db, **args)
    assert first['receipt'] == second['receipt']
    assert db.scalar(select(WealthPosition).where(WealthPosition.owner_id == 'u1')).units_hundredths == request['units_hundredths']
    request = wealth.prepare_order(db, session_id='s1', kind='buy', product_id='P1', account_id='a1c', amount_minor=10000)
    wealth.submit_assessment(db, session_id='s1', answers=[0, 0, 0, 0])
    with pytest.raises(DomainError): wealth.issue_token(db, session_id='s1', order_id=request['id'])


@pytest.mark.parametrize('kind,params', [('online_block', {}), ('limit', {'limit_minor': 500}), ('loss', {})])
def test_card_restrictions_and_idempotent_payments(bank, kind, params):
    db, _ = bank
    request = cards.prepare(db, session_id='s1', kind=kind, card_id='c1a', **params)
    settle(db, cards, request, kind='card' if kind == 'loss' else None)
    args = dict(session_id='s1', card_id='c1a', amount_minor=1000, channel='online', idempotency_key='one-attempt')
    first, second = cards.payment(db, **args), cards.payment(db, **args)
    assert first['id'] == second['id'] and first['status'] == 'rejected'
    if kind == 'online_block':
        result = cards.payment(db, **(args | {'channel': 'offline', 'idempotency_key': 'offline-attempt'}))
        assert result['status'] == 'posted'
    if kind == 'loss':
        from app.domain.operations import prepare_card_action
        with pytest.raises(DomainError): prepare_card_action(db, session_id='s1', card_id='c1a', action='unfreeze')


@pytest.mark.parametrize('channel', ['transfer', 'buy', 'card', 'subscription'])
def test_birthday_reserved_money_cannot_fund_unrelated_business(bank, channel):
    db, _ = bank
    birthday(db)
    # Leave 500 yuan outside the 1000-yuan reservation using a legitimate ledger debit.
    post_entry(db, owner_id='u1', account_id='a1c', kind='purchase', direction='debit', amount_minor=850000, business_at=BUSINESS)
    db.commit()
    before = db.get(Account, 'a1c').balance_minor
    assert available_balance(db, db.get(Account, 'a1c')) == 50000
    if channel == 'card':
        assert cards.payment(db, session_id='s1', card_id='c1a', amount_minor=60000, channel='offline', idempotency_key='reserved')['status'] == 'rejected'
    elif channel == 'subscription':
        auth = authorized_subscription(db)
        auth.amount_minor = 60000
        security.grant(db, 'u1', 'subscription:' + auth.id, security.subscription_terms(auth), 'yellow', (security.now() + timedelta(days=366)).isoformat(), 12); db.commit()
        assert subscriptions._charge_due(db, auth.id, datetime.fromisoformat(auth.next_charge_at_utc))['status'] == 'failed'
    else:
        with pytest.raises(DomainError):
            if channel == 'transfer': prepare_transfer(db, session_id='s1', source_account_id='a1c', target={'type': 'own_account', 'account_id': 'a1s'}, amount_minor=60000)
            else:
                wealth.submit_assessment(db, session_id='s1', answers=[2, 2, 2, 2])
                wealth.prepare_order(db, session_id='s1', kind='buy', product_id='P1', account_id='a1c', amount_minor=60000)
    assert db.get(Account, 'a1c').balance_minor == before


def authorized_subscription(db):
    subscriptions.seed_test_authorizations(db, owner_id='u1', account_id='a1c'); db.flush()
    auth = db.scalar(select(SubscriptionAuthorization).where(SubscriptionAuthorization.merchant == '云音乐'))
    security.grant(db, 'u1', 'subscription:' + auth.id, security.subscription_terms(auth), 'yellow', (security.now() + timedelta(days=366)).isoformat(), 12)
    db.commit(); return auth


def test_subscription_month_end_reminder_restart_revoke_and_no_catchup_replay(bank):
    db, factory = bank
    security.confirm_subscription_access(db, 's1')
    auth = authorized_subscription(db)
    for month in (7, 8, 9):
        post_entry(db, owner_id='u1', account_id='a1c', kind='purchase', direction='debit', amount_minor=3000,
                   business_at=datetime(2026, month, 15, 1, tzinfo=UTC), merchant='云音乐', category='订阅服务')
    db.commit()
    assert subscriptions.identify(db, 's1')
    auth.charge_day = 31
    from app.domain.clock import SHANGHAI
    auth.next_charge_at_utc = datetime(2026, 10, 31, 9, tzinfo=SHANGHAI).astimezone(UTC).isoformat(timespec='seconds')
    security.grant(db, 'u1', 'subscription:' + auth.id, security.subscription_terms(auth), 'yellow', (security.now() + timedelta(days=366)).isoformat(), 12); db.commit()
    request = subscriptions.prepare(db, session_id='s1', kind='reminder', authorization_id=auth.id, business_now=BUSINESS)
    settle(db, subscriptions, request, business_now=BUSINESS)
    reminder = db.scalar(select(SubscriptionReminder).where(SubscriptionReminder.authorization_id == auth.id))
    with factory() as restarted:
        subscriptions._notify_due(restarted, reminder.id, datetime(2026, 10, 30, tzinfo=UTC))
        subscriptions._notify_due(restarted, reminder.id, datetime(2026, 10, 30, tzinfo=UTC))
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(SubscriptionNotification)) == 1
    due = datetime.fromisoformat(auth.next_charge_at_utc)
    subscriptions._charge_due(db, auth.id, due)
    db.refresh(auth)
    assert datetime.fromisoformat(auth.next_charge_at_utc).astimezone(SHANGHAI).day == 30
    request = subscriptions.prepare(db, session_id='s1', kind='revoke', authorization_id=auth.id, business_now=BUSINESS)
    settle(db, subscriptions, request, business_now=BUSINESS)
    before = db.get(Account, 'a1c').balance_minor
    result = subscriptions._charge_due(db, auth.id, datetime(2026, 12, 31, tzinfo=UTC))
    assert result['status'] == 'blocked' and result['error_code'] == 'AUTHORIZATION_REVOKED'
    assert db.get(Account, 'a1c').balance_minor == before


@pytest.mark.parametrize('supplier_failure', ['rejected', 'price_changed'])
def test_birthday_partial_retry_delivery_and_remainder_release(bank, monkeypatch, supplier_failure):
    db, factory = bank
    pid = birthday(db)
    directory = Path(db.get_bind().url.database).parent
    supplier = directory / 'suppliers.json'
    overrides = {'flowers': {'price_minor': 30000, 'reject_order': False}, 'cake': {'price_minor': 21000 if supplier_failure == 'price_changed' else 20000, 'reject_order': supplier_failure == 'rejected'}}
    supplier.write_text(json.dumps(overrides), encoding='utf-8')
    monkeypatch.setenv('FINAGENT_BIRTHDAYS_TEST_CLOCK_ENABLED', '1')
    monkeypatch.setenv('FINAGENT_BIRTHDAYS_TEST_DB', str(Path(db.get_bind().url.database).resolve()))
    monkeypatch.setenv('FINAGENT_BIRTHDAYS_TEST_SUPPLIER_FILE', str(supplier.resolve()))
    birthdays._execute_due(db, pid, DUE)
    assert db.get(BirthdayPlan, pid).status == 'partial_failure'
    flowers = db.scalar(select(BirthdayOrder).where(BirthdayOrder.plan_id == pid, BirthdayOrder.item_key == 'flowers'))
    txid = flowers.transaction_id
    overrides['cake'] = {'price_minor': 20000, 'reject_order': False}; supplier.write_text(json.dumps(overrides), encoding='utf-8')
    request = birthdays.prepare(db, session_id='s1', kind='retry', terms={'item_key': 'cake'}, plan_id=pid, business_now=DUE)
    settle(db, birthdays, request, business_now=DUE)
    birthdays.run_due_birthdays(factory, now=DUE)
    db.expire_all()
    orders = db.scalars(select(BirthdayOrder).where(BirthdayOrder.plan_id == pid)).all()
    assert len(orders) == 2 and db.get(BirthdayOrder, flowers.id).transaction_id == txid
    assert all(o.payment_status == 'paid' for o in orders)
    for order in orders:
        with pytest.raises(DomainError): birthdays.simulate_delivery(db, session_id='s1', order_id=order.id, business_now=DUE)
        birthdays.simulate_delivery(db, session_id='s1', order_id=order.id, business_now=datetime(2026, 10, 20, 2, tzinfo=UTC))
        birthdays.simulate_delivery(db, session_id='s1', order_id=order.id, business_now=datetime(2026, 10, 20, 2, tzinfo=UTC))
    assert db.get(BirthdayPlan, pid).status == 'completed'
    assert db.scalar(select(BirthdayReservation).where(BirthdayReservation.plan_id == pid)).remaining_minor == 0
    assert all(o.delivery_receipt_json for o in orders)


def test_frozen_card_refuses_consumption(bank):
    from app.domain.operations import prepare_card_action, issue_confirmation_token, confirm_proposal
    db, _ = bank
    pid = prepare_card_action(db, session_id='s1', card_id='c1a', action='freeze')['proposal']['id']
    token = issue_confirmation_token(db, session_id='s1', proposal_id=pid)['confirmation_token']
    confirm_proposal(db, session_id='s1', proposal_id=pid, confirmation_token=token)
    result = cards.payment(db, session_id='s1', card_id='c1a', amount_minor=1000, channel='offline', idempotency_key='frozen-attempt')
    assert result['status'] == 'rejected' and result['error_code'] == 'INVALID_CARD_STATE'


def test_subscription_revoke_charge_race_keeps_postings_and_stops_future_debits(bank):
    db, factory = bank
    security.confirm_subscription_access(db, 's1')
    auth = authorized_subscription(db)
    due = datetime.fromisoformat(auth.next_charge_at_utc)
    request = subscriptions.prepare(db, session_id='s1', kind='revoke', authorization_id=auth.id, business_now=BUSINESS)
    token = subscriptions.issue_token(db, session_id='s1', request_id=request['id'])['confirmation_token']
    before = db.get(Account, 'a1c').balance_minor
    db.commit()
    parallel(factory, [lambda c: subscriptions._charge_due(c, auth.id, due),
        lambda c: subscriptions.confirm(c, session_id='s1', request_id=request['id'], confirmation_token=token, business_now=BUSINESS)])
    db.expire_all()
    auth = db.get(SubscriptionAuthorization, auth.id)
    charge = db.scalar(select(SubscriptionCharge).where(SubscriptionCharge.authorization_id == auth.id))
    assert charge.status in ('posted', 'blocked')
    assert db.get(Account, 'a1c').balance_minor == before - (auth.amount_minor if charge.status == 'posted' else 0)
    if auth.status != 'revoked':
        # A charge that won the race invalidates the stale revocation snapshot.
        subscriptions.cancel(db, session_id='s1', request_id=request['id'])
        request = subscriptions.prepare(db, session_id='s1', kind='revoke', authorization_id=auth.id, business_now=BUSINESS)
        settle(db, subscriptions, request, business_now=BUSINESS)
    before = db.get(Account, 'a1c').balance_minor
    assert subscriptions._charge_due(db, auth.id, datetime(2026,12,31,tzinfo=UTC))['status'] == 'blocked'
    assert db.get(Account, 'a1c').balance_minor == before


def test_birthday_change_and_cancel_order_race_cannot_undo_paid_orders(bank):
    db, factory = bank
    pid = birthday(db)
    change = birthdays.prepare(db, session_id='s1', kind='change', plan_id=pid,
        terms={'budget_minor':60000,'item_keys':['flowers','cake']}, business_now=BUSINESS)
    settle(db, birthdays, change, business_now=BUSINESS)
    with pytest.raises(DomainError):
        birthdays.prepare(db, session_id='s1', kind='change', plan_id=pid,
            terms={'budget_minor':60000,'item_keys':['flowers','cake'],'address':'替换地址'}, business_now=BUSINESS)
    cancel = birthdays.prepare(db, session_id='s1', kind='cancel', terms={}, plan_id=pid, business_now=BUSINESS)
    token = birthdays.issue_token(db, session_id='s1', request_id=cancel['id'])['confirmation_token']
    db.commit()
    parallel(factory, [lambda c: birthdays._execute_due(c,pid,DUE),
        lambda c: birthdays.confirm(c, session_id='s1', request_id=cancel['id'], confirmation_token=token, business_now=DUE)])
    db.expire_all()
    plan = db.get(BirthdayPlan, pid)
    orders = db.scalars(select(BirthdayOrder).where(BirthdayOrder.plan_id == pid)).all()
    assert (plan.status == 'cancelled' and not orders) or (plan.status == 'awaiting_delivery' and len(orders)==2 and all(o.payment_status=='paid' for o in orders))
    if orders:
        with pytest.raises(DomainError) as error:
            birthdays.prepare(db, session_id='s1', kind='cancel', terms={}, plan_id=pid, business_now=DUE)
        assert error.value.code == 'IRREVERSIBLE_ORDER'


def test_birthday_reservation_race_with_transfer_never_spends_reserved_money(bank):
    from app.domain.operations import issue_confirmation_token, confirm_proposal
    db, factory = bank
    request = birthdays.prepare(db, session_id='s2', kind='create', business_now=BUSINESS,
        terms={'birthday_date':'2026-10-20','source_account_id':'a1c','budget_minor':960000,
               'recipient':'合成收件人','address':'合成测试地址','item_keys':['flowers','cake']})
    birthday_token = birthdays.issue_token(db, session_id='s2', request_id=request['id'])['confirmation_token']
    pid = prepare_transfer(db, session_id='s1', source_account_id='a1c', target={'type':'own_account','account_id':'a1s'}, amount_minor=50000)['proposal']['id']
    token = issue_confirmation_token(db, session_id='s1', proposal_id=pid)['confirmation_token']; db.commit()
    outcomes = parallel(factory, [lambda c: birthdays.confirm(c,session_id='s2',request_id=request['id'],confirmation_token=birthday_token,business_now=BUSINESS),
        lambda c: confirm_proposal(c,session_id='s1',proposal_id=pid,confirmation_token=token)])
    db.expire_all()
    assert sum('error_code' not in row for row in outcomes) == 1
    assert available_balance(db,db.get(Account,'a1c')) >= 0


def test_insufficient_reserved_funds_never_pays_and_can_be_cancelled(bank):
    db, _ = bank
    pid = birthday(db)
    hold = db.scalar(select(BirthdayReservation).where(BirthdayReservation.plan_id==pid))
    # Fault injection represents a damaged reservation. Restore it before the
    # ledger invariant check; never inject such a fault into the daily demo DB.
    original = hold.remaining_minor; hold.remaining_minor = 1; db.commit()
    before = db.get(Account,'a1c').balance_minor
    birthdays._execute_due(db,pid,DUE)
    assert db.get(Account,'a1c').balance_minor == before and db.get(BirthdayPlan,pid).spent_minor == 0
    hold = db.get(BirthdayReservation,hold.id); hold.remaining_minor=original; db.commit()
    request=birthdays.prepare(db,session_id='s1',kind='cancel',terms={},plan_id=pid,business_now=DUE)
    settle(db,birthdays,request,business_now=DUE)
    assert db.get(BirthdayPlan,pid).status=='cancelled'
