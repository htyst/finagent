"""Protected schedule routes; confirmation never enters the LLM tool registry."""
from __future__ import annotations

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from app.domain.scheduled_transfers import (cancel_schedule, confirm_schedule, get_schedule,
    issue_schedule_confirmation_token, list_schedules, prepare_schedule)


class PrepareSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_account_id: str = Field(min_length=1, max_length=120)
    target: dict[str, str]
    amount_minor: StrictInt
    execute_at_utc: str = Field(min_length=1, max_length=64)
    timezone: str = Field(default="Asia/Shanghai", max_length=64)
    currency: str = Field(default="CNY", max_length=3)
    note: str | None = Field(default=None, max_length=120)


class ConfirmSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation_token: str = Field(min_length=1, max_length=200)


def register_routes(app, get_db, require_demo_session, require_write_protection, envelope):
    @app.get("/api/schedules")
    def schedules(db=Depends(get_db), session=Depends(require_demo_session)):
        return envelope(list_schedules(db, session_id=session.id))

    @app.post("/api/schedules/prepare")
    def prepare(payload: PrepareSchedule, request: Request, db=Depends(get_db),
                session=Depends(require_demo_session)):
        require_write_protection(request, session)
        return envelope(prepare_schedule(db, session_id=session.id,
            source_account_id=payload.source_account_id, target=payload.target,
            amount_minor=payload.amount_minor, execute_at_utc=payload.execute_at_utc,
            timezone_name=payload.timezone, currency=payload.currency, note=payload.note))

    @app.get("/api/schedules/{schedule_id}")
    def get(schedule_id: str, db=Depends(get_db), session=Depends(require_demo_session)):
        return envelope(get_schedule(db, session_id=session.id, schedule_id=schedule_id))

    @app.post("/api/schedules/{schedule_id}/token")
    def token(schedule_id: str, request: Request, db=Depends(get_db),
              session=Depends(require_demo_session)):
        require_write_protection(request, session)
        return envelope(issue_schedule_confirmation_token(db, session_id=session.id, schedule_id=schedule_id))

    @app.post("/api/schedules/{schedule_id}/confirm")
    def confirm(schedule_id: str, payload: ConfirmSchedule, request: Request, db=Depends(get_db),
                session=Depends(require_demo_session)):
        require_write_protection(request, session)
        return envelope(confirm_schedule(db, session_id=session.id, schedule_id=schedule_id,
                                         confirmation_token=payload.confirmation_token))

    @app.post("/api/schedules/{schedule_id}/cancel")
    def cancel(schedule_id: str, request: Request, db=Depends(get_db), session=Depends(require_demo_session)):
        require_write_protection(request, session)
        return envelope(cancel_schedule(db, session_id=session.id, schedule_id=schedule_id))
