"""Durable AA requests and receipts for the explicitly simulated payment channel."""
from __future__ import annotations
from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.data.models import Base


class CollectionRequest(Base):
    __tablename__ = "collection_requests"
    __table_args__ = (
        CheckConstraint("total_minor > 0"), CheckConstraint("self_share_minor > 0"),
        CheckConstraint("expected_minor > 0"),
        CheckConstraint("total_minor = self_share_minor + expected_minor"),
        CheckConstraint("status IN ('pending_confirmation', 'collecting', 'completed', 'cancelled', 'expired')"),
        UniqueConstraint("owner_id", "dataset_version", "request_key", name="uq_collection_request_key"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    dataset_version: Mapped[str] = mapped_column(String, nullable=False)
    agent_task_id: Mapped[str | None] = mapped_column(String)
    destination_account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    account_version: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(80), nullable=False)
    total_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    self_share_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, index=True)
    request_key: Mapped[str | None] = mapped_column(String(120))
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    token_hash: Mapped[str | None] = mapped_column(String(64))
    token_session_id: Mapped[str | None] = mapped_column(String)
    token_expires_at_utc: Mapped[str | None] = mapped_column(String)
    created_at_utc: Mapped[str] = mapped_column(String, nullable=False)
    expires_at_utc: Mapped[str] = mapped_column(String, nullable=False)
    launched_at_utc: Mapped[str | None] = mapped_column(String)
    updated_at_utc: Mapped[str] = mapped_column(String, nullable=False)


class CollectionMember(Base):
    __tablename__ = "collection_members"
    __table_args__ = (
        CheckConstraint("share_minor > 0"),
        CheckConstraint("status IN ('self_share', 'unpaid', 'paid', 'cancelled')"),
        UniqueConstraint("request_id", "ordinal", name="uq_collection_member_order"),
        UniqueConstraint("request_id", "recipient_id", name="uq_collection_member_recipient"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("collection_requests.id"), nullable=False, index=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    recipient_id: Mapped[str | None] = mapped_column(ForeignKey("recipients.id"))
    recipient_version: Mapped[int | None] = mapped_column(Integer)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    account_last4: Mapped[str | None] = mapped_column(String(4))
    share_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    token_hash: Mapped[str | None] = mapped_column(String(64))
    token_session_id: Mapped[str | None] = mapped_column(String)
    token_expires_at_utc: Mapped[str | None] = mapped_column(String)
    paid_at_utc: Mapped[str | None] = mapped_column(String)


class CollectionPayment(Base):
    __tablename__ = "collection_payments"
    __table_args__ = (CheckConstraint("amount_minor > 0"),
                      CheckConstraint("channel = 'simulated_member_payment'"))
    id: Mapped[str] = mapped_column(String, primary_key=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("collection_requests.id"), nullable=False, index=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("collection_members.id"), nullable=False, unique=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    transaction_id: Mapped[str] = mapped_column(ForeignKey("transactions.id"), nullable=False, unique=True)
    business_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    channel: Mapped[str] = mapped_column(String, nullable=False)
    authorization_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    authorization_session_id: Mapped[str] = mapped_column(String, nullable=False)
    created_at_utc: Mapped[str] = mapped_column(String, nullable=False)
