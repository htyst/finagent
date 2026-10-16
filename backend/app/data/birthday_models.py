"""Persistent authorized birthday arrangements, earmarked funds and supplier facts."""
from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.data.models import Base


class BirthdayPlan(Base):
    __tablename__ = "birthday_plans"
    __table_args__ = (CheckConstraint("budget_minor > 0"), CheckConstraint("spent_minor >= 0"),
        CheckConstraint("released_minor >= 0"), CheckConstraint("version > 0"),
        CheckConstraint("status IN ('scheduled','ordering','awaiting_delivery','partial_failure','completed','completed_with_changes','cancelled','blocked')"))
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    dataset_version: Mapped[str] = mapped_column(String, nullable=False)
    agent_task_id: Mapped[str | None] = mapped_column(ForeignKey("agent_tasks.id"))
    source_account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    birthday_date: Mapped[str] = mapped_column(String(10), nullable=False)
    birthday_source: Mapped[str] = mapped_column(String, nullable=False)
    order_at_utc: Mapped[str] = mapped_column(String, nullable=False, index=True)
    delivery_date: Mapped[str] = mapped_column(String(10), nullable=False)
    recipient: Mapped[str] = mapped_column(String(80), nullable=False)
    address: Mapped[str] = mapped_column(String(200), nullable=False)
    budget_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    item_keys_json: Mapped[str] = mapped_column(String, nullable=False)
    spent_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    released_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    items_cancelled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    authorized_terms_json: Mapped[str] = mapped_column(String, nullable=False)
    terms_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    authorization_request_id: Mapped[str] = mapped_column(String, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String)
    created_at_utc: Mapped[str] = mapped_column(String, nullable=False)
    updated_at_utc: Mapped[str] = mapped_column(String, nullable=False)


class BirthdayRequest(Base):
    __tablename__ = "birthday_requests"
    __table_args__ = (CheckConstraint("kind IN ('create','change','cancel','cancel_item','retry')"),
        CheckConstraint("status IN ('pending_confirmation','executed','cancelled','invalidated','expired')"))
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    dataset_version: Mapped[str] = mapped_column(String, nullable=False)
    agent_task_id: Mapped[str | None] = mapped_column(ForeignKey("agent_tasks.id"))
    plan_id: Mapped[str | None] = mapped_column(ForeignKey("birthday_plans.id"))
    kind: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    terms_json: Mapped[str] = mapped_column(String, nullable=False)
    terms_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    token_hash: Mapped[str | None] = mapped_column(String(64))
    receipt_json: Mapped[str | None] = mapped_column(String)
    created_at_utc: Mapped[str] = mapped_column(String, nullable=False)
    expires_at_utc: Mapped[str] = mapped_column(String, nullable=False)
    completed_at_utc: Mapped[str | None] = mapped_column(String)


class BirthdayReservation(Base):
    __tablename__ = "birthday_reservations"
    __table_args__ = (CheckConstraint("remaining_minor >= 0"), CheckConstraint("status IN ('active','released')"))
    id: Mapped[str] = mapped_column(String, primary_key=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("birthday_plans.id"), nullable=False, unique=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), nullable=False, index=True)
    dataset_version: Mapped[str] = mapped_column(String, nullable=False)
    remaining_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)


class BirthdayOrder(Base):
    __tablename__ = "birthday_orders"
    __table_args__ = (UniqueConstraint("plan_id", "item_key"), CheckConstraint("amount_minor > 0"),
        CheckConstraint("item_key IN ('flowers','cake')"),
        CheckConstraint("payment_status IN ('paid','unpaid')"),
        CheckConstraint("status IN ('pending','accepted','rejected','cancelled','delivered')"))
    id: Mapped[str] = mapped_column(String, primary_key=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("birthday_plans.id"), nullable=False, index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    item_key: Mapped[str] = mapped_column(String, nullable=False)
    supplier: Mapped[str] = mapped_column(String, nullable=False)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    recipient: Mapped[str] = mapped_column(String, nullable=False)
    address: Mapped[str] = mapped_column(String, nullable=False)
    delivery_date: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    payment_status: Mapped[str] = mapped_column(String, nullable=False)
    transaction_id: Mapped[str | None] = mapped_column(ForeignKey("transactions.id"), unique=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String)
    delivery_receipt_json: Mapped[str | None] = mapped_column(String)
    created_at_utc: Mapped[str] = mapped_column(String, nullable=False)
    updated_at_utc: Mapped[str] = mapped_column(String, nullable=False)


class BirthdayFundEvent(Base):
    __tablename__ = "birthday_fund_events"
    __table_args__ = (CheckConstraint("kind IN ('reserve','spend','release')"), CheckConstraint("amount_minor > 0"),
        CheckConstraint("remaining_minor >= 0"))
    id: Mapped[str] = mapped_column(String, primary_key=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("birthday_plans.id"), nullable=False, index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    event_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    delta_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    remaining_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    request_id: Mapped[str | None] = mapped_column(ForeignKey("birthday_requests.id"))
    order_id: Mapped[str | None] = mapped_column(ForeignKey("birthday_orders.id"))
    transaction_id: Mapped[str | None] = mapped_column(ForeignKey("transactions.id"), unique=True)
    created_at_utc: Mapped[str] = mapped_column(String, nullable=False)


class BirthdayNotification(Base):
    __tablename__ = "birthday_notifications"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("birthday_plans.id"), nullable=False, index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    event_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(String, nullable=False)
    created_at_utc: Mapped[str] = mapped_column(String, nullable=False)
