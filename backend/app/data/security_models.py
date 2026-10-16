"""Persistent security state. Never copied into model prompts or public views."""
from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from app.data.models import Base


class SecurityState(Base):
    __tablename__ = 'security_states'
    owner_id: Mapped[str] = mapped_column(String, primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[str | None] = mapped_column(String)


class TaskControl(Base):
    __tablename__ = 'task_controls'
    task_id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(String, index=True)
    state: Mapped[str] = mapped_column(String)
    data_json: Mapped[str] = mapped_column(String)


class SecurityChallenge(Base):
    __tablename__ = 'security_challenges'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(String, index=True)
    session_id: Mapped[str] = mapped_column(String)
    binding: Mapped[str] = mapped_column(String, index=True)
    code_hash: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[str] = mapped_column(String)


class SecurityAuthorization(Base):
    __tablename__ = 'security_authorizations'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(String, index=True)
    resource: Mapped[str] = mapped_column(String, unique=True)
    digest: Mapped[str] = mapped_column(String)
    level: Mapped[str] = mapped_column(String)
    policy_version: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str] = mapped_column(String)
    remaining: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String)
    terms_json: Mapped[str] = mapped_column(String)
