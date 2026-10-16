"""Persistent models for the explicitly simulated wealth product workflow."""
from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.data.models import Base

class WealthProduct(Base):
    __tablename__ = 'wealth_products'
    __table_args__ = (CheckConstraint('risk_level IN (1,2,4)'), CheckConstraint('min_minor > 0'),
                     CheckConstraint('nav_minor > 0'), CheckConstraint('fee_minor >= 0'), CheckConstraint('lock_days >= 0'))
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    risk_level: Mapped[int] = mapped_column(Integer, nullable=False)
    min_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    nav_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    fee_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lock_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    yield_text: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

class RiskAssessment(Base):
    __tablename__ = 'risk_assessments'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey('users.id'), nullable=False, index=True)
    answers_json: Mapped[str] = mapped_column(String, nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    max_risk: Mapped[int] = mapped_column(Integer, nullable=False)
    rule_version: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    created_at_utc: Mapped[str] = mapped_column(String, nullable=False)
    expires_at_utc: Mapped[str] = mapped_column(String, nullable=False)

class WealthPosition(Base):
    __tablename__ = 'wealth_positions'
    __table_args__ = (UniqueConstraint('owner_id','product_id'), CheckConstraint('units_hundredths >= 0'))
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey('users.id'), nullable=False)
    product_id: Mapped[str] = mapped_column(ForeignKey('wealth_products.id'), nullable=False)
    units_hundredths: Mapped[int] = mapped_column(Integer, nullable=False)
    acquired_at_utc: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

class WealthOrder(Base):
    __tablename__ = 'wealth_orders'
    __table_args__ = (CheckConstraint("kind IN ('buy','redeem')"), CheckConstraint('amount_minor > 0'),
                     CheckConstraint('units_hundredths > 0'),
                     CheckConstraint("status IN ('pending_confirmation','accepted','settled','cancelled','expired','invalidated')"))
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey('users.id'), nullable=False)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    dataset_version: Mapped[str] = mapped_column(String, nullable=False)
    agent_task_id: Mapped[str | None] = mapped_column(ForeignKey('agent_tasks.id'), nullable=True)
    account_id: Mapped[str] = mapped_column(ForeignKey('accounts.id'), nullable=False)
    product_id: Mapped[str] = mapped_column(ForeignKey('wealth_products.id'), nullable=False)
    assessment_id: Mapped[str] = mapped_column(ForeignKey('risk_assessments.id'), nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    units_hundredths: Mapped[int] = mapped_column(Integer, nullable=False)
    terms_json: Mapped[str] = mapped_column(String, nullable=False)
    terms_digest: Mapped[str] = mapped_column(String, nullable=False)
    token_hash: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False)
    created_at_utc: Mapped[str] = mapped_column(String, nullable=False)
    expires_at_utc: Mapped[str] = mapped_column(String, nullable=False)
    accepted_at_utc: Mapped[str | None] = mapped_column(String)
    settled_at_utc: Mapped[str | None] = mapped_column(String)
    receipt_json: Mapped[str | None] = mapped_column(String)
