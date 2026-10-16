from __future__ import annotations

import json

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.data.models import Account, Card, Recipient, RecipientCredit, Transaction
from app.domain.clock import SHANGHAI, local_midnight_utc
from app.domain.errors import DomainError
from app.domain.money import MAX_MINOR, require_cny, validate_minor, validate_transfer_amount

POSTED = "posted"
KINDS = {
    "purchase": {"debit"}, "refund": {"credit"}, "income": {"credit"},
    "internal_transfer": {"debit", "credit"}, "external_transfer": {"debit"},
    "wealth_buy": {"debit"}, "wealth_redeem": {"credit"},
}


def reserved_balance(db: Session, account_id: str) -> int:
    from app.data.birthday_models import BirthdayReservation
    return db.scalar(select(func.coalesce(func.sum(BirthdayReservation.remaining_minor), 0)).where(
        BirthdayReservation.account_id == account_id, BirthdayReservation.status == "active")) or 0


def available_balance(db: Session, account: Account) -> int:
    return account.balance_minor - reserved_balance(db, account.id)


def _reserved_payment(db, account, plan_id, business_id, amount, kind, direction, status):
    from app.data.birthday_models import BirthdayPlan, BirthdayOrder, BirthdayReservation
    from app.domain.birthdays import validate_authorized_plan, _config
    plan = db.get(BirthdayPlan, plan_id)
    order = db.get(BirthdayOrder, business_id) if business_id else None
    if kind != "purchase" or direction != "debit" or status != POSTED or not plan or not order or (
        plan.owner_id != account.owner_id or plan.source_account_id != account.id or plan.status != "ordering"
        or order.plan_id != plan.id or order.owner_id != account.owner_id or order.status != "pending"
        or order.payment_status != "unpaid" or order.transaction_id or order.amount_minor != amount
        or order.item_key not in json.loads(plan.item_keys_json)
        or order.address != plan.address
        or order.recipient != plan.recipient or order.delivery_date != plan.delivery_date):
        raise DomainError("RESERVATION_AUTHORIZATION_INVALID", "专款付款须绑定已授权生日订单", 409)
    validate_authorized_plan(db, plan)
    contract = next((item for item in _config(plan)["items"] if item["item_key"] == order.item_key), None)
    if not contract or order.supplier != contract["supplier"] or order.amount_minor != contract["amount_minor"]:
        raise DomainError("RESERVATION_AUTHORIZATION_INVALID", "专款付款须匹配已确认的店铺和固定报价", 409)
    hold = db.scalar(select(BirthdayReservation).where(BirthdayReservation.plan_id == plan.id))
    if not hold or hold.status != "active" or hold.remaining_minor < amount or account.balance_minor < amount:
        raise DomainError("INSUFFICIENT_RESERVED_FUNDS", "生日专款不足，不能付款", 409)
    if plan.spent_minor + amount > plan.budget_minor:
        raise DomainError("BUDGET_EXCEEDED", "生日累计支出超出授权预算", 409)
    return plan, hold, order


def owned_account(db: Session, owner_id: str, account_id: str, *, active: bool = True) -> Account:
    account = db.scalar(select(Account).where(Account.id == account_id, Account.owner_id == owner_id))
    if account is None:
        raise DomainError("RESOURCE_NOT_FOUND", "资源不存在", 404)
    require_cny(account.currency)
    if active and account.status != "active":
        raise DomainError("INVALID_ACCOUNT_STATE", "账户当前不可用于该操作")
    return account


def daily_transfer_outflow(db: Session, account_id: str, business_date: date) -> int:
    start = local_midnight_utc(business_date)
    end = local_midnight_utc(business_date + timedelta(days=1))
    return db.scalar(select(func.coalesce(func.sum(Transaction.amount_minor), 0)).where(
        Transaction.account_id == account_id,
        Transaction.type.in_(["internal_transfer", "external_transfer"]),
        Transaction.direction == "debit", Transaction.status == POSTED,
        Transaction.posted_at_utc >= start, Transaction.posted_at_utc < end,
    )) or 0


def validate_refund(db: Session, owner_id: str, original_id: str, account_id: str, amount_minor: int) -> Transaction:
    original = db.scalar(select(Transaction).where(Transaction.id == original_id, Transaction.owner_id == owner_id))
    if original is None or original.type != "purchase" or original.status != POSTED:
        raise DomainError("INVALID_REFUND", "退款必须关联本人已入账消费")
    if original.account_id != account_id:
        raise DomainError("INVALID_REFUND", "退款账户须与原消费一致")
    require_cny(original.currency)
    already = db.scalar(select(func.coalesce(func.sum(Transaction.amount_minor), 0)).where(
        Transaction.original_transaction_id == original_id, Transaction.type == "refund", Transaction.status == POSTED,
    )) or 0
    if already + amount_minor > original.amount_minor:
        raise DomainError("REFUND_LIMIT_EXCEEDED", "累计退款不能超过原消费")
    return original


def post_entry(db: Session, *, owner_id: str, account_id: str, kind: str, direction: str,
               amount_minor: int, business_at: datetime, status: str = POSTED,
               card_id: str | None = None, category: str | None = None, merchant: str | None = None,
               business_id: str | None = None, original_id: str | None = None,
               system_now: datetime | None = None, channel: str = 'offline',
               reservation_plan_id: str | None = None) -> Transaction:
    """Internal domain method. Caller owns transaction/commit and authorization."""
    amount = validate_minor(amount_minor)
    if direction == 'debit':
        from app.domain.security import assert_unlocked
        assert_unlocked(db, owner_id)
    if kind not in KINDS or direction not in KINDS[kind] or status not in {POSTED, "pending", "void"}:
        raise DomainError("INVALID_INPUT", "无效流水类型、方向或状态")
    if business_at.tzinfo is None:
        raise DomainError("INVALID_INPUT", "业务时间必须包含时区")
    account = owned_account(db, owner_id, account_id, active=kind not in {"refund", "income"})
    if card_id:
        card = db.scalar(select(Card).where(Card.id == card_id, Card.owner_id == owner_id, Card.account_id == account_id))
        if card is None:
            raise DomainError("RESOURCE_NOT_FOUND", "资源不存在", 404)
        if kind == 'purchase' and status != 'void':
            from app.domain.card_management import check_purchase
            if channel not in ('online', 'offline'):
                raise DomainError('INVALID_INPUT','未知消费渠道')
            check_purchase(db,card,amount,channel)
        if kind == "purchase" and status != "void" and card.status != "active":
            raise DomainError("INVALID_CARD_STATE", "卡片已冻结，不能新增模拟消费")
    if kind == "refund":
        if not original_id:
            raise DomainError("INVALID_REFUND", "退款缺少原消费引用")
        original = validate_refund(db, owner_id, original_id, account_id, amount)
        category = original.category
        card_id = original.card_id
    elif original_id:
        raise DomainError("INVALID_INPUT", "仅退款可以引用原消费")
    if status == POSTED and direction == "credit" and account.balance_minor + amount > MAX_MINOR:
        raise DomainError("INVALID_INPUT", "余额超出允许范围")
    reserved = None
    if reservation_plan_id is not None:
        reserved = _reserved_payment(db, account, reservation_plan_id, business_id, amount, kind, direction, status)
    elif status == POSTED and direction == "debit" and available_balance(db, account) < amount:
        raise DomainError("INSUFFICIENT_FUNDS", "账户一般可用余额不足，预留专款不能用于普通支出")
    created = system_now or datetime.now(timezone.utc)
    if created.tzinfo is None:
        raise DomainError("INVALID_INPUT", "实际时间必须包含时区")
    row = Transaction(
        id=uuid4().hex, owner_id=owner_id, account_id=account_id, card_id=card_id,
        type=kind, direction=direction, amount_minor=amount, currency="CNY",
        category=category, merchant=merchant,
        posted_at_utc=business_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
        created_at_utc=created.astimezone(timezone.utc).isoformat(timespec="seconds"),
        status=status, business_id=business_id, original_transaction_id=original_id,
    )
    db.add(row)
    if status == POSTED:
        if reserved:
            plan, hold, order = reserved
            hold.remaining_minor -= amount
            if hold.remaining_minor == 0:
                hold.status = "released"
            plan.spent_minor += amount
        account.balance_minor += amount if direction == "credit" else -amount
        account.version += 1
    db.flush()
    if reserved:
        from app.domain.birthdays import _fund
        _fund(db, plan, "spend", amount, order.id + ":spend", order_id=order.id, transaction_id=row.id)
        db.flush()
    return row


def post_refund(db: Session, *, owner_id: str, original_id: str, amount_minor: int,
                business_at: datetime, system_now: datetime | None = None, channel: str = 'offline') -> Transaction:
    original = db.scalar(select(Transaction).where(Transaction.id == original_id, Transaction.owner_id == owner_id))
    if original is None:
        raise DomainError("RESOURCE_NOT_FOUND", "资源不存在", 404)
    return post_entry(db, owner_id=owner_id, account_id=original.account_id, kind="refund",
                      direction="credit", amount_minor=amount_minor, business_at=business_at,
                      original_id=original_id, merchant=original.merchant, system_now=system_now)


def post_internal_transfer(db: Session, *, owner_id: str, source_id: str, target_id: str,
                           amount_minor: int, business_at: datetime, business_id: str | None = None,
                           system_now: datetime | None = None) -> tuple[Transaction, Transaction]:
    if business_at.tzinfo is None:
        raise DomainError("INVALID_INPUT", "业务时间必须包含时区")
    if source_id == target_id:
        raise DomainError("INVALID_INPUT", "转出和转入账户不能相同")
    source = owned_account(db, owner_id, source_id)
    owned_account(db, owner_id, target_id)
    day = business_at.astimezone(SHANGHAI).date()
    amount = validate_transfer_amount(amount_minor, daily_transfer_outflow(db, source.id, day))
    if available_balance(db, source) < amount:
        raise DomainError("INSUFFICIENT_FUNDS", "账户一般可用余额不足")
    reference = business_id or uuid4().hex
    debit = post_entry(db, owner_id=owner_id, account_id=source_id, kind="internal_transfer",
                       direction="debit", amount_minor=amount, business_at=business_at,
                       business_id=reference, system_now=system_now)
    credit = post_entry(db, owner_id=owner_id, account_id=target_id, kind="internal_transfer",
                        direction="credit", amount_minor=amount, business_at=business_at,
                        business_id=reference, system_now=system_now)
    return debit, credit


def post_external_transfer(db: Session, *, owner_id: str, source_id: str, recipient_id: str,
                           amount_minor: int, business_at: datetime, business_id: str | None = None,
                           system_now: datetime | None = None, channel: str = 'offline') -> Transaction:
    if business_at.tzinfo is None:
        raise DomainError("INVALID_INPUT", "业务时间必须包含时区")
    source = owned_account(db, owner_id, source_id)
    recipient = db.scalar(select(Recipient).where(Recipient.id == recipient_id, Recipient.owner_id == owner_id))
    if recipient is None:
        raise DomainError("RESOURCE_NOT_FOUND", "资源不存在", 404)
    if recipient.status not in {"active","one_time"}:
        raise DomainError("INVALID_RECIPIENT_STATE", "收款人不可用")
    day = business_at.astimezone(SHANGHAI).date()
    amount = validate_transfer_amount(amount_minor, daily_transfer_outflow(db, source.id, day))
    if available_balance(db, source) < amount:
        raise DomainError("INSUFFICIENT_FUNDS", "账户一般可用余额不足")
    debit = post_entry(db, owner_id=owner_id, account_id=source_id, kind="external_transfer",
                       direction="debit", amount_minor=amount, business_at=business_at,
                       business_id=business_id or uuid4().hex, merchant=recipient.name, system_now=system_now)
    db.add(RecipientCredit(id=uuid4().hex, owner_id=owner_id, recipient_id=recipient.id,
                          business_id=debit.business_id, source_transaction_id=debit.id,
                          amount_minor=amount, currency="CNY", posted_at_utc=debit.posted_at_utc,
                          created_at_utc=debit.created_at_utc))
    db.flush()
    return debit


def ledger_errors(db: Session) -> list[str]:
    errors: list[str] = []
    accounts = {row.id: row for row in db.scalars(select(Account)).all()}
    all_rows = db.scalars(select(Transaction)).all()
    cards = {row.id: row for row in db.scalars(select(Card)).all()}
    by_id = {row.id: row for row in all_rows}
    balance_delta = defaultdict(int)
    refunds = defaultdict(int)
    internal = defaultdict(list)
    for row in all_rows:
        account = accounts.get(row.account_id)
        if account is None or row.owner_id != account.owner_id or row.currency != account.currency:
            errors.append(f"transaction_owner_or_currency:{row.id}")
        if row.card_id:
            card = cards.get(row.card_id)
            if card is None or card.owner_id != row.owner_id or card.account_id != row.account_id:
                errors.append(f"transaction_card:{row.id}")
        if row.type not in KINDS or row.direction not in KINDS.get(row.type, set()) or row.amount_minor <= 0:
            errors.append(f"transaction_shape:{row.id}")
        if row.status == POSTED:
            balance_delta[row.account_id] += row.amount_minor if row.direction == "credit" else -row.amount_minor
        if row.type == "refund" and row.status == POSTED:
            original = by_id.get(row.original_transaction_id)
            if original is None or original.type != "purchase" or original.status != POSTED or original.account_id != row.account_id or original.category != row.category:
                errors.append(f"refund_reference:{row.id}")
            else:
                refunds[original.id] += row.amount_minor
        if row.type == "internal_transfer" and row.status == POSTED:
            internal[row.business_id].append(row)
    for account in accounts.values():
        if account.opening_balance_minor + balance_delta[account.id] != account.balance_minor:
            errors.append(f"balance:{account.id}")
    for original_id, amount in refunds.items():
        if amount > by_id[original_id].amount_minor:
            errors.append(f"refund_limit:{original_id}")
    for business_id, legs in internal.items():
        if not business_id or len(legs) != 2 or {leg.direction for leg in legs} != {"debit", "credit"} or len({leg.account_id for leg in legs}) != 2 or len({leg.owner_id for leg in legs}) != 1 or legs[0].amount_minor != legs[1].amount_minor:
            errors.append(f"internal_pair:{business_id}")
    recipients = {row.id: row for row in db.scalars(select(Recipient)).all()}
    for credit in db.scalars(select(RecipientCredit)).all():
        debit = by_id.get(credit.source_transaction_id)
        recipient = recipients.get(credit.recipient_id)
        if (debit is None or recipient is None or recipient.owner_id != credit.owner_id
                or debit.owner_id != credit.owner_id or debit.type != "external_transfer"
                or debit.direction != "debit" or debit.status != POSTED
                or debit.amount_minor != credit.amount_minor or debit.currency != credit.currency
                or debit.business_id != credit.business_id):
            errors.append(f"recipient_credit:{credit.id}")
    return errors
