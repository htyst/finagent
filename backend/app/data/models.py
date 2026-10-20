from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    profile: Mapped[str] = mapped_column(String, nullable=False, default="")


class DemoSetting(Base):
    __tablename__ = "demo_settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    demo_date: Mapped[str] = mapped_column(String(10), nullable=False)
    dataset_version: Mapped[str] = mapped_column(String, nullable=False)
    seed_version: Mapped[str] = mapped_column(String, nullable=False)


class DemoSession(Base):
    __tablename__ = "demo_sessions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    dataset_version: Mapped[str] = mapped_column(String, nullable=False)
    expires_at_utc: Mapped[str] = mapped_column(String, nullable=False)
    csrf_token: Mapped[str] = mapped_column(String, nullable=False, default="")


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint("opening_balance_minor >= 0"),
        CheckConstraint("balance_minor >= 0"),
        CheckConstraint("currency = 'CNY'"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    alias: Mapped[str] = mapped_column(String, nullable=False)
    account_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    opening_balance_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class Recipient(Base):
    __tablename__ = "recipients"
    __table_args__ = (Index("uq_recipient_number", "owner_id", "number_fingerprint", unique=True),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    bank_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    number_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    account_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(11), nullable=True)
    remark: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class Card(Base):
    __tablename__ = "cards"
    __table_args__ = (CheckConstraint("status IN ('active', 'frozen')"),
                      Index("uq_card_number", "owner_id", "number_fingerprint", unique=True))
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    alias: Mapped[str] = mapped_column(String, nullable=False)
    brand: Mapped[str] = mapped_column(String, nullable=False)
    card_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    number_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint("amount_minor > 0"),
        CheckConstraint("direction IN ('debit', 'credit')"),
        CheckConstraint("currency = 'CNY'"),
        UniqueConstraint("business_id", "account_id", "type", name="uq_transaction_business_leg"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    card_id: Mapped[str | None] = mapped_column(ForeignKey("cards.id"))
    type: Mapped[str] = mapped_column(String, nullable=False)
    direction: Mapped[str] = mapped_column(String, nullable=False)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    category: Mapped[str | None] = mapped_column(String)
    merchant: Mapped[str | None] = mapped_column(String)
    posted_at_utc: Mapped[str] = mapped_column(String, nullable=False)
    created_at_utc: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    business_id: Mapped[str | None] = mapped_column(String)
    original_transaction_id: Mapped[str | None] = mapped_column(ForeignKey("transactions.id"))


class DataCoverage(Base):
    __tablename__ = "data_coverage"
    __table_args__ = (CheckConstraint("end_utc > start_utc"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    start_utc: Mapped[str] = mapped_column(String, nullable=False)
    end_utc: Mapped[str] = mapped_column(String, nullable=False)
    completeness: Mapped[str] = mapped_column(String, nullable=False)


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    session_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    active_session_id: Mapped[str | None] = mapped_column(String, unique=True)
    dataset_version: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, index=True)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at_utc: Mapped[str] = mapped_column(String, nullable=False)
    updated_at_utc: Mapped[str] = mapped_column(String, nullable=False)


class Proposal(Base):
    __tablename__ = "proposals"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), nullable=False, unique=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    session_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    dataset_version: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, index=True)
    params_json: Mapped[str] = mapped_column(String, nullable=False)
    snapshot_json: Mapped[str] = mapped_column(String, nullable=False)
    params_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    token_hash: Mapped[str | None] = mapped_column(String(64))
    validation_json: Mapped[str | None] = mapped_column(String)
    created_at_utc: Mapped[str] = mapped_column(String, nullable=False)
    expires_at_utc: Mapped[str] = mapped_column(String, nullable=False)
    updated_at_utc: Mapped[str] = mapped_column(String, nullable=False)


class RecipientCredit(Base):
    """Simulated receiving ledger; never represents an external bank balance."""
    __tablename__ = "recipient_credits"
    __table_args__ = (CheckConstraint("amount_minor > 0"), CheckConstraint("currency = 'CNY'"))
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    recipient_id: Mapped[str] = mapped_column(ForeignKey("recipients.id"), nullable=False)
    business_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    source_transaction_id: Mapped[str] = mapped_column(ForeignKey("transactions.id"), nullable=False, unique=True)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    posted_at_utc: Mapped[str] = mapped_column(String, nullable=False)
    created_at_utc: Mapped[str] = mapped_column(String, nullable=False)


class TransferOrder(Base):
    __tablename__ = "transfer_orders"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("proposals.id"), nullable=False, unique=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    business_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    source_account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    target_account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"))
    recipient_id: Mapped[str | None] = mapped_column(ForeignKey("recipients.id"))
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    created_at_utc: Mapped[str] = mapped_column(String, nullable=False)


class CardActionOrder(Base):
    __tablename__ = "card_action_orders"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("proposals.id"), nullable=False, unique=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    card_id: Mapped[str] = mapped_column(ForeignKey("cards.id"), nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    before_status: Mapped[str] = mapped_column(String, nullable=False)
    after_status: Mapped[str] = mapped_column(String, nullable=False)
    created_at_utc: Mapped[str] = mapped_column(String, nullable=False)


class BusinessResult(Base):
    __tablename__ = "business_results"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("proposals.id"), nullable=False, unique=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    receipt_json: Mapped[str] = mapped_column(String, nullable=False)
    created_at_utc: Mapped[str] = mapped_column(String, nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    proposal_id: Mapped[str | None] = mapped_column(ForeignKey("proposals.id"))
    event: Mapped[str] = mapped_column(String, nullable=False)
    detail_code: Mapped[str | None] = mapped_column(String)
    created_at_utc: Mapped[str] = mapped_column(String, nullable=False)


class AgentTask(Base):
    __tablename__ = "agent_tasks"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    session_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    dataset_version: Mapped[str] = mapped_column(String, nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    intent: Mapped[str | None] = mapped_column(String)
    context_json: Mapped[str] = mapped_column(String, nullable=False, default="{}")
    plan_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    plan_json: Mapped[str] = mapped_column(String, nullable=False, default="[]", server_default="[]")
    requested_outcomes_json: Mapped[str] = mapped_column(String, nullable=False, default="[]", server_default="[]")
    stop_requested: Mapped[bool] = mapped_column(Integer, nullable=False, default=False, server_default="0")
    proposal_id: Mapped[str | None] = mapped_column(ForeignKey("proposals.id"))
    created_at_utc: Mapped[str] = mapped_column(String, nullable=False)
    updated_at_utc: Mapped[str] = mapped_column(String, nullable=False)


class AgentMessage(Base):
    __tablename__ = "agent_messages"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_tasks.id"), nullable=False, index=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(String, nullable=False)
    created_at_utc: Mapped[str] = mapped_column(String, nullable=False)


class AgentStep(Base):
    __tablename__ = "agent_steps"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_tasks.id"), nullable=False, index=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    plan_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    depends_on_json: Mapped[str] = mapped_column(String, nullable=False, default="[]", server_default="[]")
    on_failure: Mapped[str] = mapped_column(String, nullable=False, default="stop", server_default="stop")
    result_ref: Mapped[str | None] = mapped_column(String)
    tool_name: Mapped[str] = mapped_column(String, nullable=False)
    args_json: Mapped[str] = mapped_column(String, nullable=False)
    result_json: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    created_at_utc: Mapped[str] = mapped_column(String, nullable=False)


# Module imports register extended tables and remain safe when a model module imports Base.
from app.data import scheduled_models as _scheduled_models
from app.data import collection_models as _collection_models
from app.data import wealth_models as _wealth_models

from app.data import card_models as _card_models
from app.data import subscription_models as _subscription_models
from app.data import birthday_models as _birthday_models
from app.data import financial_models as _financial_models
from app.data import workflow_models as _workflow_models
