"""UI-only confirmation grants authority; production API has no clock controls."""
from fastapi import Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.data.models import DemoSession
from app.domain.subscriptions import cancel, cancel_reminder, confirm, get_portfolio, issue_token


class ConfirmPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation_token: str = Field(min_length=1, max_length=200)


def register_routes(app, get_db, require_demo_session, require_write_protection, envelope):
    @app.get("/api/subscriptions")
    def read(session: DemoSession = Depends(require_demo_session), db: Session = Depends(get_db)):
        return envelope(get_portfolio(db, session.id))

    @app.post("/api/subscriptions/requests/{rid}/confirmation-token")
    def token(rid: str, request: Request, response: Response,
              session: DemoSession = Depends(require_demo_session), db: Session = Depends(get_db)):
        require_write_protection(request, session)
        response.headers["Cache-Control"] = "no-store"
        return envelope(issue_token(db, session_id=session.id, request_id=rid))

    @app.post("/api/subscriptions/requests/{rid}/confirm")
    def confirm_route(rid: str, payload: ConfirmPayload, request: Request,
                      session: DemoSession = Depends(require_demo_session), db: Session = Depends(get_db)):
        require_write_protection(request, session)
        return envelope(confirm(db, session_id=session.id, request_id=rid,
            confirmation_token=payload.confirmation_token))

    @app.post("/api/subscriptions/requests/{rid}/cancel")
    def cancel_route(rid: str, request: Request,
                     session: DemoSession = Depends(require_demo_session), db: Session = Depends(get_db)):
        require_write_protection(request, session)
        return envelope(cancel(db, session_id=session.id, request_id=rid))

    @app.post("/api/subscriptions/reminders/{rid}/cancel")
    def cancel_reminder_route(rid: str, request: Request,
                              session: DemoSession = Depends(require_demo_session), db: Session = Depends(get_db)):
        require_write_protection(request, session)
        return envelope(cancel_reminder(db, session_id=session.id, reminder_id=rid))
