from fastapi import Depends, Request
from sqlalchemy.orm import Session
from app.data.models import DemoSession
from app.domain.directory import AddCard, AddRecipient, add_card, add_recipient


def register_routes(app, get_db, require_demo_session, require_write_protection, envelope):
    @app.post('/api/cards', status_code=201)
    def create_card(payload: AddCard, request: Request,
                    session: DemoSession = Depends(require_demo_session), db: Session = Depends(get_db)):
        require_write_protection(request, session)
        return envelope(add_card(db, session.id, payload))

    @app.post('/api/recipients', status_code=201)
    def create_recipient(payload: AddRecipient, request: Request,
                         session: DemoSession = Depends(require_demo_session), db: Session = Depends(get_db)):
        require_write_protection(request, session)
        return envelope(add_recipient(db, session.id, payload))
