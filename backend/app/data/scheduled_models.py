"""Durable, explicitly authorized future transfers; imported by schema registration."""
from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.data.models import Base


class ScheduledTransfer(Base):
    __tablename__ = "scheduled_transfers"
    __table_args__ = (
        CheckConstraint("status IN ('pending_confirmation', 'scheduled', 'executed', 'cancelled', 'expired', 'failed')"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    agent_task_id: Mapped[str | None] = mapped_column(ForeignKey("agent_tasks.id"), nullable=True, index=True)
    dataset_version: Mapped[str] = mapped_column(String, nullable=False)
    params_json: Mapped[str] = mapped_column(String, nullable=False)
    params_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    target_binding_json: Mapped[str] = mapped_column(String, nullable=False)
    execute_at_utc: Mapped[str] = mapped_column(String, nullable=False, index=True)
    timezone: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, index=True)
    token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at_utc: Mapped[str] = mapped_column(String, nullable=False)
    confirmed_at_utc: Mapped[str | None] = mapped_column(String, nullable=True)
    authorization_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    execution_proposal_id: Mapped[str | None] = mapped_column(ForeignKey("proposals.id"), nullable=True, unique=True)
    receipt_json: Mapped[str | None] = mapped_column(String, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at_utc: Mapped[str] = mapped_column(String, nullable=False)
    updated_at_utc: Mapped[str] = mapped_column(String, nullable=False)
