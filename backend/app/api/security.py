"""Dedicated UI security channel; not registered as model tools."""
from typing import Literal
from fastapi import Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from app.data.models import DemoSession
from app.data.security_models import SecurityState
from app.domain import security

Kind = Literal['proposal', 'wealth', 'card', 'birthday', 'subscription', 'schedule', 'collection']


class Verification(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    challenge_id: str = Field(min_length=1, max_length=64)
    code: str = Field(pattern=r'^\d{6}$')


class MandateConfirmation(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_version: int = Field(ge=1)


def register_routes(app, get_db, require_demo_session, protect, envelope):
    @app.get('/api/security/status')
    def status(response: Response, session: DemoSession = Depends(require_demo_session), db: Session = Depends(get_db)):
        response.headers['Cache-Control'] = 'no-store'
        row = db.get(SecurityState, session.owner_id)
        return envelope({'failures': row.failures if row else 0, 'locked_until': row.locked_until if row else None,
                         'failure_limit': security.FAILURE_LIMIT, 'lock_minutes': security.LOCK_MINUTES,
                         'policy_version': security.POLICY_VERSION, 'authentication': '本地模拟短信，非真实银行认证'})

    @app.get('/api/security/operations/{kind}/{rid}')
    def detail(kind: Kind, rid: str, response: Response, session: DemoSession = Depends(require_demo_session), db: Session = Depends(get_db)):
        response.headers['Cache-Control'] = 'no-store'
        return envelope(security.describe(db, session.id, kind, rid))

    @app.post('/api/security/operations/{kind}/{rid}/challenge')
    def challenge(kind: Kind, rid: str, request: Request, response: Response, session: DemoSession = Depends(require_demo_session), db: Session = Depends(get_db)):
        protect(request, session)
        response.headers['Cache-Control'] = 'no-store'
        return envelope(security.start_challenge(db, session.id, kind, rid))

    @app.post('/api/security/operations/{kind}/{rid}/verify')
    def verify(kind: Kind, rid: str, payload: Verification, request: Request, session: DemoSession = Depends(require_demo_session), db: Session = Depends(get_db)):
        protect(request, session)
        return envelope(security.verify_challenge(db, session.id, kind, rid, payload.challenge_id, payload.code))

    @app.post('/api/subscriptions/query-confirmation')
    def subscription_confirmation(request: Request, session: DemoSession = Depends(require_demo_session), db: Session = Depends(get_db)):
        protect(request, session)
        return envelope(security.confirm_subscription_access(db, session.id))

    @app.post('/api/subscriptions/authorizations/{rid}/authorize-background')
    def authorize_mandate(rid: str, payload: MandateConfirmation, request: Request, session: DemoSession = Depends(require_demo_session), db: Session = Depends(get_db)):
        from app.data.atomic import atomic_write
        from app.domain.subscriptions import _owned_authorization
        protect(request, session)
        with atomic_write(db):
            security.assert_unlocked(db, session.owner_id)
            auth = _owned_authorization(db, session, rid)
            if auth.version != payload.expected_version:
                from app.domain.errors import DomainError
                raise DomainError('STALE_RESOURCE', '代扣条款或周期已变化，请刷新后重新核对', 409)
            if auth.status != 'active':
                from app.domain.errors import DomainError
                raise DomainError('AUTHORIZATION_REVOKED', '授权已撤销', 409)
            security.grant(db, session.owner_id, 'subscription:' + auth.id, security.subscription_terms(auth), 'yellow',
                (security.now() + security.timedelta(days=366)).isoformat(), 12)
            security.audit(db, session.owner_id, 'subscription_background_authorized')
            return envelope({'authorized': True, 'scope': security.subscription_terms(auth),
                             'max_executions': 12, 'valid_days': 366, 'simulated': True})
