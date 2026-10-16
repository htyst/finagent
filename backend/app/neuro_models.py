"""Additive tables only; no ALTER TABLE and no changes to legacy schemas."""
from sqlalchemy import String, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.data.models import Base

class VerificationRecord(Base):
    __tablename__ = 'verification_records'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(String, index=True)
    proposal_id: Mapped[str | None] = mapped_column(String, index=True)
    phase: Mapped[str] = mapped_column(String)
    payload_json: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String)
class TraceRecord(Base):
    __tablename__ = 'neuro_traces'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(String, index=True)
    task_id: Mapped[str] = mapped_column(String, index=True)
    payload_json: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String)
class StandingOrder(Base):
    __tablename__ = 'standing_orders'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(String, index=True)
    dataset_version: Mapped[str] = mapped_column(String)
    action: Mapped[str] = mapped_column(String)
    params_json: Mapped[str] = mapped_column(String)
    schedule_json: Mapped[str] = mapped_column(String)
    intent_hash: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String)
    activated_at: Mapped[str | None] = mapped_column(String)
class StandingConfirmation(Base):
    __tablename__ = 'standing_confirmations'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    order_id: Mapped[str] = mapped_column(String, index=True)
    owner_id: Mapped[str] = mapped_column(String)
    session_id: Mapped[str] = mapped_column(String)
    operation: Mapped[str] = mapped_column(String)
    intent_hash: Mapped[str] = mapped_column(String)
    token_hash: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str] = mapped_column(String)
class StandingOccurrence(Base):
    __tablename__ = 'standing_occurrences'
    __table_args__ = (UniqueConstraint('order_id', 'period'),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    order_id: Mapped[str] = mapped_column(String, index=True)
    owner_id: Mapped[str] = mapped_column(String)
    period: Mapped[str] = mapped_column(String)
    due_date: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    proposal_id: Mapped[str | None] = mapped_column(String)
    message: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String)

ADDITIVE_TABLES = [cls.__table__ for cls in (VerificationRecord, TraceRecord, StandingOrder,
                                           StandingConfirmation, StandingOccurrence)]
def install(engine):
    Base.metadata.create_all(engine, tables=ADDITIVE_TABLES, checkfirst=True)
