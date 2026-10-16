"""Dedicated, CSRF protected AA controls; never registered as model tools."""
from __future__ import annotations
from fastapi import Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from sqlalchemy.orm import Session
from app.data.models import DemoSession
from app.domain.collections import (cancel_collection, confirm_collection, get_collection,
    issue_collection_confirmation_token, issue_member_payment_token, list_collections,
    prepare_collection, simulate_member_payment)


class CollectionPreparePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    destination_account_id: str = Field(min_length=1, max_length=100)
    total_minor: StrictInt = Field(gt=0)
    recipient_ids: list[str] = Field(min_length=1, max_length=19)
    title: str = Field(default="AA收款", min_length=1, max_length=80)
    request_key: str | None = Field(default=None, min_length=1, max_length=120)
    split_method: str = "equal"


class CollectionConfirmPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation_token: str = Field(min_length=1, max_length=200)


class MemberPaymentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    payment_token: str = Field(min_length=1, max_length=200)
    expected_amount_minor: StrictInt = Field(gt=0)
    simulated: StrictBool


def register_routes(app, get_db, require_demo_session, require_write_protection, envelope):
    @app.get("/api/collections")
    def collection_list(session: DemoSession = Depends(require_demo_session), db: Session = Depends(get_db)):
        return envelope(list_collections(db, session_id=session.id))

    @app.post("/api/collections")
    def collection_prepare(payload: CollectionPreparePayload, request: Request,
                           session: DemoSession = Depends(require_demo_session), db: Session = Depends(get_db)):
        require_write_protection(request, session)
        return envelope(prepare_collection(db, session_id=session.id, **payload.model_dump()))

    @app.get("/api/collections/{collection_id}")
    def collection_detail(collection_id: str, session: DemoSession = Depends(require_demo_session),
                          db: Session = Depends(get_db)):
        return envelope(get_collection(db, session_id=session.id, collection_id=collection_id))

    @app.post("/api/collections/{collection_id}/confirmation-token")
    def collection_token(collection_id: str, request: Request, response: Response,
                         session: DemoSession = Depends(require_demo_session), db: Session = Depends(get_db)):
        require_write_protection(request, session)
        response.headers["Cache-Control"] = "no-store"
        return envelope(issue_collection_confirmation_token(db, session_id=session.id, collection_id=collection_id))

    @app.post("/api/collections/{collection_id}/confirm")
    def collection_confirm(collection_id: str, payload: CollectionConfirmPayload, request: Request,
                           session: DemoSession = Depends(require_demo_session), db: Session = Depends(get_db)):
        require_write_protection(request, session)
        return envelope(confirm_collection(db, session_id=session.id, collection_id=collection_id,
                                           confirmation_token=payload.confirmation_token))

    @app.post("/api/collections/{collection_id}/cancel")
    def collection_cancel(collection_id: str, request: Request, session: DemoSession = Depends(require_demo_session),
                          db: Session = Depends(get_db)):
        require_write_protection(request, session)
        return envelope(cancel_collection(db, session_id=session.id, collection_id=collection_id))

    @app.post("/api/collections/{collection_id}/members/{member_id}/payment-token")
    def member_token(collection_id: str, member_id: str, request: Request, response: Response,
                     session: DemoSession = Depends(require_demo_session), db: Session = Depends(get_db)):
        require_write_protection(request, session)
        response.headers["Cache-Control"] = "no-store"
        return envelope(issue_member_payment_token(db, session_id=session.id, collection_id=collection_id,
                                                  member_id=member_id))

    @app.post("/api/collections/{collection_id}/members/{member_id}/simulate-payment")
    def member_pay(collection_id: str, member_id: str, payload: MemberPaymentPayload, request: Request,
                   session: DemoSession = Depends(require_demo_session), db: Session = Depends(get_db)):
        require_write_protection(request, session)
        return envelope(simulate_member_payment(db, session_id=session.id, collection_id=collection_id,
                                               member_id=member_id, **payload.model_dump()))
