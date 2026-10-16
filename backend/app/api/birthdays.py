"""Page confirmation is the sole authorization channel; no production clock routes."""
from fastapi import Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from app.data.models import DemoSession
from app.domain.birthdays import get_portfolio, issue_token, confirm, cancel, simulate_delivery


class ConfirmPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation_token: str = Field(min_length=1, max_length=200)


def register_routes(app, get_db, require_demo_session, require_write_protection, envelope):
    @app.get("/api/birthdays")
    def read(session: DemoSession = Depends(require_demo_session), db: Session = Depends(get_db)):
        return envelope(get_portfolio(db, session.id))

    @app.post("/api/birthdays/requests/{request_id}/confirmation-token")
    def token(request_id: str, request: Request, response: Response,
              session: DemoSession = Depends(require_demo_session), db: Session = Depends(get_db)):
        require_write_protection(request, session)
        response.headers["Cache-Control"] = "no-store"
        return envelope(issue_token(db, session_id=session.id, request_id=request_id))

    @app.post("/api/birthdays/requests/{request_id}/confirm")
    def confirm_route(request_id: str, payload: ConfirmPayload, request: Request,
                      session: DemoSession = Depends(require_demo_session), db: Session = Depends(get_db)):
        require_write_protection(request, session)
        return envelope(confirm(db, session_id=session.id, request_id=request_id,
            confirmation_token=payload.confirmation_token))

    @app.post("/api/birthdays/requests/{request_id}/cancel")
    def cancel_route(request_id: str, request: Request,
                     session: DemoSession = Depends(require_demo_session), db: Session = Depends(get_db)):
        require_write_protection(request, session)
        return envelope(cancel(db, session_id=session.id, request_id=request_id))

    @app.post("/api/birthdays/orders/{order_id}/simulate-delivery")
    def delivery_route(order_id: str, request: Request,
                       session: DemoSession = Depends(require_demo_session), db: Session = Depends(get_db)):
        require_write_protection(request, session)
        return envelope(simulate_delivery(db, session_id=session.id, order_id=order_id))
