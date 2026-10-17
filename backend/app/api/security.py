"""Dedicated UI security channel; not registered as model tools."""
from typing import Literal
from fastapi import BackgroundTasks,Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy.orm import Session
from app.data.models import DemoSession
from app.data.security_models import SecurityState
from app.domain import security
from app.config import is_demo_admin_request
from app.domain.errors import DomainError

Kind = Literal['proposal', 'wealth', 'card', 'birthday', 'subscription', 'schedule', 'collection', 'service']

class QueryContinuation(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    pending_id:str=Field(min_length=1,max_length=64)


class TaskAuthorization(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    action_id:str=Field(min_length=64,max_length=64)


class TaskConfirmation(TaskAuthorization):
    confirmation_token:str=Field(min_length=1,max_length=200)


class TaskVerification(TaskAuthorization):
    challenge_id:str=Field(min_length=1,max_length=64)
    code:str=Field(pattern=r'^\d{6}$')


class TaskAssessment(QueryContinuation):
    answers:list[StrictInt]=Field(min_length=4,max_length=4)


class Verification(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    challenge_id: str = Field(min_length=1, max_length=64)
    code: str = Field(pattern=r'^\d{6}$')


class SimulatedSMS(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    challenge_id: str = Field(pattern=r'^[0-9a-f]{32}$')


class TaskSimulatedSMS(TaskAuthorization):
    challenge_id: str = Field(pattern=r'^[0-9a-f]{32}$')


class MandateConfirmation(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_version: int = Field(ge=1)


def register_routes(app, get_db, require_demo_session, protect, envelope):
    @app.get('/api/tasks/{task_id}/authorizations/{kind}/{rid}')
    def task_operation(task_id:str,kind:Kind,rid:str,response:Response,session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db)):
        from app.agent import authorization
        from app.agent.service import _task
        response.headers['Cache-Control']='no-store'
        return envelope(authorization.describe(db,session,_task(db,session,task_id),kind,rid))

    @app.post('/api/tasks/{task_id}/authorizations/{kind}/{rid}/confirmation-token')
    def task_token(task_id:str,kind:Kind,rid:str,payload:TaskAuthorization,request:Request,response:Response,session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db)):
        from app.agent import authorization
        protect(request,session);response.headers['Cache-Control']='no-store'
        return envelope(authorization.issue_token(db,session,task_id,kind,rid,payload.action_id))

    @app.post('/api/tasks/{task_id}/authorizations/{kind}/{rid}/challenge')
    def task_challenge(task_id:str,kind:Kind,rid:str,payload:TaskAuthorization,request:Request,response:Response,session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db)):
        from app.agent import authorization
        from app.agent.service import _task
        from app.data.atomic import atomic_write
        protect(request,session);response.headers['Cache-Control']='no-store'
        with atomic_write(db):
            authorization.check(db,session,_task(db,session,task_id),kind,rid,payload.action_id)
            return envelope(security.start_challenge(db,session.id,kind,rid))

    @app.post('/api/tasks/{task_id}/authorizations/{kind}/{rid}/verify')
    def task_verify(task_id:str,kind:Kind,rid:str,payload:TaskVerification,request:Request,session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db)):
        from app.agent import authorization
        from app.agent.service import _task
        protect(request,session)
        authorization.check(db,session,_task(db,session,task_id),kind,rid,payload.action_id)
        return envelope(security.verify_challenge(db,session.id,kind,rid,payload.challenge_id,payload.code))

    @app.post('/api/tasks/{task_id}/authorizations/{kind}/{rid}/confirm')
    def task_confirm(task_id:str,kind:Kind,rid:str,payload:TaskConfirmation,request:Request,response:Response,background_tasks:BackgroundTasks,session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db)):
        from app.agent import authorization
        protect(request,session);response.headers['Cache-Control']='no-store'
        result=authorization.confirm(db,session,task_id,kind,rid,payload.action_id,payload.confirmation_token)
        from app.agent.dynamic_runtime import queue_authorization
        return envelope(queue_authorization(background_tasks,db,session,task_id,kind,rid,payload.action_id,result))

    @app.post('/api/tasks/{task_id}/authorizations/{kind}/{rid}/cancel')
    def task_cancel(task_id:str,kind:Kind,rid:str,payload:TaskAuthorization,request:Request,background_tasks:BackgroundTasks,session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db)):
        from app.agent import authorization
        protect(request,session)
        result=authorization.cancel(db,session,task_id,kind,rid,payload.action_id)
        from app.agent.dynamic_runtime import queue_authorization
        return envelope(queue_authorization(background_tasks,db,session,task_id,kind,rid,payload.action_id,result,cancelled=True))

    @app.post('/api/tasks/{task_id}/risk-assessment')
    def task_assessment(task_id:str,payload:TaskAssessment,request:Request,session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db)):
        from app.agent import authorization
        protect(request,session)
        result=authorization.submit_assessment(db,session,task_id,payload.pending_id,payload.answers)
        from app.agent.dynamic_runtime import after_input
        return envelope(after_input(db,session,task_id,result))

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

    @app.post('/api/security/operations/{kind}/{rid}/simulated-sms')
    def simulated_sms(kind: Kind, rid: str, payload: SimulatedSMS, request: Request, response: Response,
                      session: DemoSession = Depends(require_demo_session), db: Session = Depends(get_db)):
        protect(request, session)
        if not request.client or not is_demo_admin_request(request.client.host):
            raise DomainError('LOCAL_SIMULATOR_ONLY', '模拟短信小窗仅供本机测试使用', 403)
        response.headers['Cache-Control'] = 'no-store'
        return envelope(security.read_simulated_sms(db, session.id, kind, rid, payload.challenge_id))

    @app.post('/api/tasks/{task_id}/authorizations/{kind}/{rid}/simulated-sms')
    def task_simulated_sms(task_id: str, kind: Kind, rid: str, payload: TaskSimulatedSMS,
                           request: Request, response: Response,
                           session: DemoSession = Depends(require_demo_session), db: Session = Depends(get_db)):
        from app.agent import authorization
        from app.agent.service import _task
        from app.data.atomic import atomic_write
        protect(request, session)
        if not request.client or not is_demo_admin_request(request.client.host):
            raise DomainError('LOCAL_SIMULATOR_ONLY', '模拟短信小窗仅供本机测试使用', 403)
        response.headers['Cache-Control'] = 'no-store'
        with atomic_write(db):
            authorization.check(db, session, _task(db, session, task_id), kind, rid, payload.action_id)
            return envelope(security.read_simulated_sms(db, session.id, kind, rid, payload.challenge_id))

    @app.post('/api/subscriptions/query-confirmation')
    def subscription_confirmation(request: Request, session: DemoSession = Depends(require_demo_session), db: Session = Depends(get_db)):
        protect(request, session)
        return envelope(security.confirm_subscription_access(db, session.id))

    @app.post('/api/tasks/{task_id}/subscription-query-confirmation')
    def confirm_chat_query(task_id:str,payload:QueryContinuation,request:Request,session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db)):
        protect(request,session)
        from app.agent.conversation_reads import confirm_subscription_query
        result=confirm_subscription_query(db,session,task_id,payload.pending_id)
        from app.agent.dynamic_runtime import after_input
        return envelope(after_input(db,session,task_id,result))

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
