"""Owner-scoped workflow projections and explicit read-only review consent."""
from typing import Literal
from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, StrictInt
from sqlalchemy.orm import Session
from app.data.models import DemoSession
from app.domain import workflows


class ReviewControl(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    action: Literal['pause', 'resume', 'cancel']
    version: StrictInt


def register_routes(app, get_db, require_session, protect, envelope):
    @app.get('/api/workflows')
    def portfolio(session: DemoSession = Depends(require_session), db: Session = Depends(get_db)):
        return envelope(workflows.portfolio(db, session))

    @app.post('/api/workflows/reviews')
    def create(payload: workflows.ReviewInput, request: Request,
               session: DemoSession = Depends(require_session), db: Session = Depends(get_db)):
        protect(request, session)
        return envelope(workflows.create_review(db, session, payload))

    @app.post('/api/workflows/reviews/{identifier}/control')
    def control(identifier: str, payload: ReviewControl, request: Request,
                session: DemoSession = Depends(require_session), db: Session = Depends(get_db)):
        protect(request, session)
        return envelope(workflows.change_review(db, session, identifier, payload.action, payload.version))
