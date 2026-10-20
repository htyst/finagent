"""Durable orchestration; these records never grant financial authority."""
from sqlalchemy import CheckConstraint, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from app.data.models import Base


class WorkflowJob(Base):
    __tablename__ = 'workflow_jobs'
    __table_args__ = (CheckConstraint("status IN ('pending','running','completed','cancelled','blocked','failed')"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    event_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    task_id: Mapped[str] = mapped_column(ForeignKey('agent_tasks.id'), nullable=False, index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey('users.id'), nullable=False)
    session_id: Mapped[str] = mapped_column(ForeignKey('demo_sessions.id'), nullable=False)
    dataset_version: Mapped[str] = mapped_column(String, nullable=False)
    payload_json: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default='pending')
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_token: Mapped[str | None] = mapped_column(String)
    lease_until_utc: Mapped[str | None] = mapped_column(String)
    next_run_at_utc: Mapped[str] = mapped_column(String, nullable=False, index=True)
    error_code: Mapped[str | None] = mapped_column(String)
    created_at_utc: Mapped[str] = mapped_column(String, nullable=False)
    updated_at_utc: Mapped[str] = mapped_column(String, nullable=False)


class WorkflowReview(Base):
    __tablename__ = 'workflow_reviews'
    __table_args__ = (CheckConstraint("status IN ('scheduled','paused','completed','cancelled','blocked')"),
                     CheckConstraint('interval_days >= 0 AND interval_days <= 90'),
                     CheckConstraint('target_minor IS NULL OR target_minor > 0'))
    id: Mapped[str] = mapped_column(String, primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey('agent_tasks.id'), nullable=False, index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey('users.id'), nullable=False)
    dataset_version: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default='scheduled')
    next_run_at_utc: Mapped[str] = mapped_column(String, nullable=False, index=True)
    end_at_utc: Mapped[str] = mapped_column(String, nullable=False)
    interval_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    target_minor: Mapped[int | None] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    consent_json: Mapped[str] = mapped_column(String, nullable=False)
    latest_result_json: Mapped[str] = mapped_column(String, nullable=False, default='{}')
    error_code: Mapped[str | None] = mapped_column(String)
    created_at_utc: Mapped[str] = mapped_column(String, nullable=False)
    updated_at_utc: Mapped[str] = mapped_column(String, nullable=False)


class WorkflowReviewRun(Base):
    __tablename__ = 'workflow_review_runs'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    review_id: Mapped[str] = mapped_column(ForeignKey('workflow_reviews.id'), nullable=False, index=True)
    event_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    result_json: Mapped[str] = mapped_column(String, nullable=False)
    created_at_utc: Mapped[str] = mapped_column(String, nullable=False)
