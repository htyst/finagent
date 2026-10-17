"""Durable bank mandates, confirmed settings, inbox notifications and debit outcomes."""
from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.data.models import Base


class SubscriptionAuthorization(Base):
    __tablename__ = "subscription_authorizations"
    __table_args__ = (
        UniqueConstraint("owner_id", "dataset_version", "account_id", "merchant"),
        CheckConstraint("amount_minor > 0"),
        CheckConstraint("period = 'monthly'"),
        CheckConstraint("charge_day BETWEEN 1 AND 31"),
        CheckConstraint("status IN ('active','revoked')"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    dataset_version: Mapped[str] = mapped_column(String, nullable=False)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    payment_card_id: Mapped[str | None] = mapped_column(String, nullable=True)
    merchant: Mapped[str] = mapped_column(String, nullable=False)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    period: Mapped[str] = mapped_column(String, nullable=False, default="monthly")
    charge_day: Mapped[int] = mapped_column(Integer, nullable=False)
    next_charge_at_utc: Mapped[str] = mapped_column(String, nullable=False, index=True)
    timezone: Mapped[str] = mapped_column(String, nullable=False, default="Asia/Shanghai")
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at_utc: Mapped[str] = mapped_column(String, nullable=False)
    revoked_at_utc: Mapped[str | None] = mapped_column(String)


class SubscriptionRequest(Base):
    __tablename__ = "subscription_requests"
    __table_args__ = (
        CheckConstraint("kind IN ('reminder','revoke')"),
        CheckConstraint("status IN ('pending_confirmation','executed','cancelled','invalidated','expired')"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    dataset_version: Mapped[str] = mapped_column(String, nullable=False)
    agent_task_id: Mapped[str | None] = mapped_column(ForeignKey("agent_tasks.id"))
    authorization_id: Mapped[str] = mapped_column(ForeignKey("subscription_authorizations.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    terms_json: Mapped[str] = mapped_column(String, nullable=False)
    terms_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    token_hash: Mapped[str | None] = mapped_column(String(64))
    receipt_json: Mapped[str | None] = mapped_column(String)
    created_at_utc: Mapped[str] = mapped_column(String, nullable=False)
    expires_at_utc: Mapped[str] = mapped_column(String, nullable=False)
    completed_at_utc: Mapped[str | None] = mapped_column(String)


class SubscriptionReminder(Base):
    __tablename__ = "subscription_reminders"
    __table_args__ = (
        CheckConstraint("amount_minor > 0"),
        CheckConstraint("days_before BETWEEN 1 AND 31"),
        CheckConstraint("hour BETWEEN 0 AND 23"),
        CheckConstraint("minute BETWEEN 0 AND 59"),
        CheckConstraint("status IN ('scheduled','delivered','cancelled')"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    authorization_id: Mapped[str] = mapped_column(ForeignKey("subscription_authorizations.id"), nullable=False)
    request_id: Mapped[str] = mapped_column(ForeignKey("subscription_requests.id"), nullable=False, unique=True)
    charge_at_utc: Mapped[str] = mapped_column(String, nullable=False)
    fire_at_utc: Mapped[str] = mapped_column(String, nullable=False, index=True)
    days_before: Mapped[int] = mapped_column(Integer, nullable=False)
    hour: Mapped[int] = mapped_column(Integer, nullable=False)
    minute: Mapped[int] = mapped_column(Integer, nullable=False)
    timezone: Mapped[str] = mapped_column(String, nullable=False)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="scheduled")
    created_at_utc: Mapped[str] = mapped_column(String, nullable=False)
    delivered_at_utc: Mapped[str | None] = mapped_column(String)


class SubscriptionNotification(Base):
    __tablename__ = "subscription_notifications"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    reminder_id: Mapped[str] = mapped_column(ForeignKey("subscription_reminders.id"), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(String, nullable=False)
    scheduled_at_utc: Mapped[str] = mapped_column(String, nullable=False)
    delivered_at_utc: Mapped[str] = mapped_column(String, nullable=False)
    recorded_at_utc: Mapped[str] = mapped_column(String, nullable=False)


class SubscriptionCharge(Base):
    __tablename__ = "subscription_charges"
    __table_args__ = (
        UniqueConstraint("authorization_id", "due_at_utc"),
        CheckConstraint("amount_minor > 0"),
        CheckConstraint("status IN ('posted','blocked','failed')"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    authorization_id: Mapped[str] = mapped_column(ForeignKey("subscription_authorizations.id"), nullable=False)
    due_at_utc: Mapped[str] = mapped_column(String, nullable=False)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String)
    transaction_id: Mapped[str | None] = mapped_column(ForeignKey("transactions.id"), unique=True)
    balance_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at_utc: Mapped[str] = mapped_column(String, nullable=False)
