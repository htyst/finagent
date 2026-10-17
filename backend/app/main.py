from __future__ import annotations

import os
import hmac
import secrets
from contextlib import asynccontextmanager
from sqlalchemy import inspect
from typing import Annotated, Literal
from urllib.parse import urlsplit
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.data.db import get_db
from app.data.models import Account, AgentTask, Card, DataCoverage, DemoSession, DemoSetting, Recipient, RecipientCredit, Transaction, User
from app.domain.clock import Clock, local_midnight_utc, parse_day
from app.domain.errors import DomainError
from app.domain.recipient_directory import normalize_phone, mask_phone
from app.domain.spending import summarize_spending
from app.agent.service import (cancel as cancel_agent_task, create as create_agent_task,
                               current_agent_task, get_agent_task, send as send_agent_message)
from app.domain.operations import (cancel_proposal, confirm_proposal, get_current_task, get_proposal,
                                   get_task, invalidate_session_proposals, issue_confirmation_token,
                                   prepare_card_action, prepare_transfer)

@asynccontextmanager
async def lifespan(application):
    from app.data.db import engine, SessionLocal
    from app.domain.scheduled_transfers import start_schedule_worker
    worker = None
    subscription_worker = None
    birthday_worker = None
    try:
        # Test fixtures override the database dependency and drive their own isolated clocks/workers.
        if os.getenv('FINAGENT_SCHEDULER_ENABLED', '1') != '0' and get_db not in application.dependency_overrides:
            if inspect(engine).has_table('scheduled_transfers'):
                worker = start_schedule_worker(SessionLocal, interval_seconds=1)
        if os.getenv("FINAGENT_SUBSCRIPTIONS_ENABLED","1") != "0" and get_db not in application.dependency_overrides:
            if inspect(engine).has_table("subscription_authorizations"):
                from app.domain.subscriptions import start_subscription_worker, guarded_subscription_clock
                subscription_worker=start_subscription_worker(SessionLocal,interval_seconds=1,clock=guarded_subscription_clock(SessionLocal))
        if os.getenv("FINAGENT_BIRTHDAYS_ENABLED", "1") != "0" and get_db not in application.dependency_overrides:
            if inspect(engine).has_table("birthday_plans"):
                from app.domain.birthdays import start_birthday_worker, guarded_birthday_clock
                birthday_worker = start_birthday_worker(SessionLocal, interval_seconds=1, clock=guarded_birthday_clock(SessionLocal))
        yield
    finally:
        if worker is not None:
            worker.stop()
        if subscription_worker is not None:
            subscription_worker.stop()
        if birthday_worker is not None:
            birthday_worker.stop()


app = FastAPI(title="FinAgent Demo API", version="0.2.0", lifespan=lifespan)
SHANGHAI = ZoneInfo("Asia/Shanghai")
COOKIE_NAME = "finagent_demo_session"


def envelope(data=None, *, status="ok", error_code=None, message="", request_id=None):
    return {"status": status, "data": data, "error_code": error_code, "message": message, "request_id": request_id or uuid4().hex}


def fail(code: str, message: str, http_status: int = 400):
    raise HTTPException(status_code=http_status, detail={"error_code": code, "message": message})


@app.exception_handler(HTTPException)
async def handle_http_error(request: Request, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, dict) else {"error_code": "INVALID_INPUT", "message": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content=envelope(status="error", error_code=detail["error_code"], message=detail["message"]))


@app.exception_handler(DomainError)
async def handle_domain_error(request: Request, exc: DomainError):
    return JSONResponse(status_code=exc.http_status, content=envelope(status="error", error_code=exc.code, message=exc.message))

@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError):
    # Do not echo invalid input (which may include confirmation credentials).
    errors = exc.errors()
    location = errors[0].get("loc", ()) if errors else ()
    field = {"limit": "每页条数", "offset": "分页位置", "evidence_limit": "证据条数",
             "evidence_offset": "证据分页位置", "message": "对话内容", "amount_minor": "金额",
             "confirmation_token": "确认凭据", "user_id": "演示用户"}.get(
                 location[-1] if location else None, "请求参数")
    return JSONResponse(status_code=422, content=envelope(status="error", error_code="INVALID_INPUT",
                        message=f"{field}格式或取值无效，请检查后重试。"))


class SelectUser(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str


def get_setting(db: Session) -> DemoSetting:
    setting = db.get(DemoSetting, 1)
    if setting is None:
        fail("DATA_UNAVAILABLE", "演示数据尚未初始化", 503)
    return setting


def require_demo_session(request: Request, db: Session = Depends(get_db)) -> DemoSession:
    session_id = request.cookies.get(COOKIE_NAME)
    if not session_id:
        fail("SESSION_EXPIRED", "请先选择演示用户", 401)
    demo_session = db.get(DemoSession, session_id)
    setting = get_setting(db)
    now = Clock(parse_day(setting.demo_date)).system_now()
    if (demo_session is None or demo_session.dataset_version != setting.dataset_version
            or datetime.fromisoformat(demo_session.expires_at_utc) <= now):
        fail("SESSION_EXPIRED", "演示会话已失效，请重新选择用户", 401)
    expected_owner = request.headers.get("x-finagent-expected-user")
    if expected_owner and expected_owner != demo_session.owner_id:
        fail("SESSION_CHANGED", "其他标签页已切换用户，请重新选择演示用户", 409)
    return demo_session


def require_owner(request: Request, db: Session = Depends(get_db)) -> str:
    return require_demo_session(request, db).owner_id


def require_write_protection(request: Request, session: DemoSession) -> None:
    origin = request.headers.get("origin")
    if not origin:
        referer = request.headers.get("referer")
        if referer:
            parsed = urlsplit(referer)
            origin = f"{parsed.scheme}://{parsed.netloc}"
    expected = f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"
    allowed = {expected}
    configured_origin = os.getenv("FINAGENT_ALLOWED_ORIGIN")
    if configured_origin:
        allowed.add(configured_origin.rstrip("/"))
    if origin not in allowed:
        fail("CSRF_REJECTED", "请从当前页面发起操作", 403)
    token = request.headers.get("x-finagent-csrf")
    if not token or not session.csrf_token or not hmac.compare_digest(token, session.csrf_token):
        fail("CSRF_REJECTED", "会话校验失败，请刷新页面", 403)


def date_utc(value: str, end: bool = False) -> str:
    day = parse_day(value)
    if end:
        try:
            day += timedelta(days=1)
        except OverflowError as exc:
            raise DomainError("INVALID_INPUT", "结束日期超出支持范围") from exc
    return local_midnight_utc(day)


@app.get("/api/health")
def health():
    from app.config import database_path
    return envelope({"service": "finagent", "phase": "P0", 'test_only': 'TEST-ONLY' in database_path().parts})


@app.get("/api/demo/info")
def demo_info(db: Session = Depends(get_db)):
    from app.agent.model_client import public_status
    setting = get_setting(db)
    return envelope({"demo_date": setting.demo_date, "dataset_version": setting.dataset_version,
                     "seed_version": setting.seed_version, "mode": os.getenv("FINAGENT_MODE", "mock"),
                     "model": public_status()})


@app.post('/api/model/probe')
def model_probe(request: Request, session: DemoSession = Depends(require_demo_session)):
    require_write_protection(request, session)
    from app.agent.model_client import probe
    return envelope(probe())


@app.get('/api/tasks/{task_id}/audit')
def task_audit_route(task_id: str, session: DemoSession = Depends(require_demo_session), db: Session = Depends(get_db)):
    from app.agent.audit import task_audit
    return envelope(task_audit(db, session, task_id))


@app.get("/api/demo/users")
def demo_users(db: Session = Depends(get_db)):
    users = db.scalars(select(User).order_by(User.id)).all()
    return envelope([{"id": user.id, "display_name": user.display_name, "profile": user.profile} for user in users])


@app.post("/api/demo/session")
def select_demo_user(payload: SelectUser, response: Response, request: Request, db: Session = Depends(get_db)):
    db.rollback()
    try:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        user = db.get(User, payload.user_id)
        if user is None:
            fail("RESOURCE_NOT_FOUND", "演示用户不存在", 404)
        setting = get_setting(db)
        old_id = request.cookies.get(COOKIE_NAME)
        instant = datetime.now(timezone.utc)
        if old_id:
            old = db.get(DemoSession, old_id)
            if old:
                invalidate_session_proposals(db, old_id, instant)
                db.delete(old)
        session_id = uuid4().hex
        expires_at = (instant + timedelta(hours=8)).isoformat(timespec="seconds")
        db.add(DemoSession(id=session_id, owner_id=user.id, dataset_version=setting.dataset_version,
                           expires_at_utc=expires_at, csrf_token=secrets.token_urlsafe(32)))
        db.commit()
    except Exception:
        db.rollback()
        raise
    response.set_cookie(COOKIE_NAME, session_id, httponly=True, samesite="lax",
                        secure=os.getenv('FINAGENT_SECURE_COOKIE', '0') == '1', path="/")
    return envelope({"user": {"id": user.id, "display_name": user.display_name}, "expires_at": expires_at})


@app.get("/api/demo/session")
def current_demo_user(owner_id: str = Depends(require_owner), db: Session = Depends(get_db)):
    user = db.get(User, owner_id)
    return envelope({"user": {"id": user.id, "display_name": user.display_name}})

@app.get("/api/accounts")
def accounts(owner_id: str = Depends(require_owner), db: Session = Depends(get_db)):
    from app.domain.ledger import available_balance, reserved_balance, account_restrictions
    rows = db.scalars(select(Account).where(Account.owner_id == owner_id).order_by(Account.id)).all()
    data = []
    for row in rows:
        restriction = account_restrictions(db, row)
        data.append({"id": row.id, "alias": row.alias, "account_mask": "**" + row.account_last4,
                      "currency": row.currency, "balance_minor": row.balance_minor,
                      "available_balance_minor": 0 if restriction['debit_blocked'] else available_balance(db, row),
                      "reserved_minor": reserved_balance(db, row.id), **restriction, "version": row.version})
    return envelope(data)


@app.get("/api/cards")
def cards(owner_id: str = Depends(require_owner), db: Session = Depends(get_db)):
    from app.domain.financial_queries import card_directory
    return envelope(card_directory(db,owner_id))


@app.get("/api/recipients")
def recipients(q: str = "", remark: str | None = None, owner_id: str = Depends(require_owner), db: Session = Depends(get_db)):
    stmt = select(Recipient).where(Recipient.owner_id == owner_id, Recipient.status != "one_time")
    if q:
        phone = normalize_phone(q)
        stmt = stmt.where(Recipient.phone == phone) if phone else stmt.where(Recipient.name.contains(q))
    if remark is not None:
        stmt = stmt.where(Recipient.remark == remark.strip())
    rows = db.scalars(stmt.order_by(Recipient.id)).all()
    from app.domain.directory import recipient_view
    return envelope([recipient_view(row) for row in rows])


@app.get("/api/recipients/{recipient_id}/credits")
def recipient_credits(recipient_id: str, owner_id: str = Depends(require_owner), db: Session = Depends(get_db),
                      limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    recipient = db.scalar(select(Recipient).where(Recipient.id == recipient_id, Recipient.owner_id == owner_id))
    if recipient is None:
        fail("RESOURCE_NOT_FOUND", "资源不存在", 404)
    filters = (RecipientCredit.owner_id == owner_id, RecipientCredit.recipient_id == recipient_id)
    total = db.scalar(select(func.count()).select_from(RecipientCredit).where(*filters)) or 0
    received = db.scalar(select(func.coalesce(func.sum(RecipientCredit.amount_minor), 0)).where(*filters)) or 0
    rows = db.scalars(select(RecipientCredit).where(*filters).order_by(RecipientCredit.created_at_utc.desc(), RecipientCredit.id).limit(limit).offset(offset)).all()
    return envelope({"simulated": True, "recipient": {"id": recipient.id, "name": recipient.name, "account_mask": "**"+recipient.account_last4},
                     "total_received_minor": received, "currency": "CNY", "total": total, "limit": limit, "offset": offset,
                     "items": [{"id": row.id, "business_id": row.business_id, "source_transaction_id": row.source_transaction_id,
                                "amount_minor": row.amount_minor, "currency": row.currency, "status": "posted",
                                "posted_at_utc": row.posted_at_utc} for row in rows]})


def check_read_query(request: Request):
    common={'start','end','account_id','card_id','category','merchant'}
    allowed=common|({'limit','offset','type','direction','status','sort'} if request.url.path=='/api/transactions' else {'comparison_mode','compare_start','compare_end','evidence_limit','evidence_offset'})
    if set(request.query_params)-allowed:
        raise DomainError('INVALID_INPUT','请求包含不支持的查询参数；筛选未执行',422)


@app.get("/api/transactions")
def transactions(start: str | None = None, end: str | None = None,
                 account_id: str | None = None, card_id: str | None = None, category: str | None = None,
                 limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
                 owner_id: str = Depends(require_owner), db: Session = Depends(get_db),
                 merchant: str | None = None,type: str | None = None,direction: str | None = None,status: str | None = None,
                 sort: str = 'date_desc',_query_guard=Depends(check_read_query)):
    from app.domain.financial_queries import list_facts
    return envelope(list_facts(db,owner_id,start=start,end=end,account_id=account_id,card_id=card_id,category=category,
        limit=limit,offset=offset,merchant=merchant,type=type,direction=direction,status=status,sort=sort))

@app.get('/api/transactions/{transaction_id}')
def transaction_detail(transaction_id:str,owner_id:str=Depends(require_owner),db:Session=Depends(get_db)):
    from app.domain.financial_queries import fact_detail
    return envelope(fact_detail(db,owner_id,transaction_id))


@app.get("/api/spending")
def spending(start: str | None = None, end: str | None = None, account_id: str | None = None,
             category: str | None = None, comparison_mode: str | None = None,
             compare_start: str | None = None, compare_end: str | None = None,
             evidence_limit: int = Query(20, ge=1, le=100), evidence_offset: int = Query(0, ge=0),
             owner_id: str = Depends(require_owner), db: Session = Depends(get_db),
             card_id: str | None = None,merchant: str | None = None,_query_guard=Depends(check_read_query)):
    result = summarize_spending(db, owner_id=owner_id, account_id=account_id, category=category,
                                start=start, end=end, comparison_mode=comparison_mode,
                                compare_start=compare_start, compare_end=compare_end,
                                evidence_limit=evidence_limit, evidence_offset=evidence_offset,card_id=card_id,merchant=merchant)
    return envelope(result)


class OwnAccountTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["own_account"]
    account_id: str


class SavedRecipientTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["saved_recipient"]
    recipient_id: str


from app.domain.direct_recipient import ExternalTarget

class TransferPrepare(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_account_id: str
    target: Annotated[OwnAccountTarget | SavedRecipientTarget | ExternalTarget, Field(discriminator="type")]
    amount_minor: StrictInt
    currency: str = "CNY"
    note: str | None = None
    task_id: str | None = None


class CardPrepare(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["freeze", "unfreeze"]
    task_id: str | None = None


class ConfirmPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation_token: str


@app.get("/api/demo/csrf")
def csrf_token(response: Response, session: DemoSession = Depends(require_demo_session),
               db: Session = Depends(get_db)):
    token = session.csrf_token
    if not token:
        token = secrets.token_urlsafe(32)
        session.csrf_token = token
        db.commit()
    response.headers["Cache-Control"] = "no-store"
    return envelope({"csrf_token": token})


@app.post("/api/transfers/prepare")
def prepare_transfer_route(payload: TransferPrepare, request: Request,
                           session: DemoSession = Depends(require_demo_session),
                           db: Session = Depends(get_db)):
    require_write_protection(request, session)
    return envelope(prepare_transfer(db, session_id=session.id,
                                     source_account_id=payload.source_account_id,
                                     target=payload.target.model_dump(),
                                     amount_minor=payload.amount_minor, currency=payload.currency,
                                     note=payload.note, task_id=payload.task_id))


@app.post("/api/cards/{card_id}/actions/prepare")
def prepare_card_route(card_id: str, payload: CardPrepare, request: Request,
                       session: DemoSession = Depends(require_demo_session),
                       db: Session = Depends(get_db)):
    require_write_protection(request, session)
    return envelope(prepare_card_action(db, session_id=session.id, card_id=card_id,
                                        action=payload.action, task_id=payload.task_id))


@app.get("/api/tasks/current")
def current_task(session: DemoSession = Depends(require_demo_session), db: Session = Depends(get_db)):
    return envelope(get_current_task(db, session_id=session.id))


@app.get("/api/tasks/{task_id}")
def task_detail(task_id: str, session: DemoSession = Depends(require_demo_session),
                db: Session = Depends(get_db)):
    if db.get(AgentTask, task_id) is not None:
        return envelope(get_agent_task(db, session, task_id))
    return envelope(get_task(db, session_id=session.id, task_id=task_id))


@app.get("/api/proposals/{proposal_id}")
def proposal_detail(proposal_id: str, response: Response,
                    session: DemoSession = Depends(require_demo_session),
                    db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    return envelope(get_proposal(db, session_id=session.id, proposal_id=proposal_id))


@app.post("/api/proposals/{proposal_id}/confirmation-token")
def confirmation_token_route(proposal_id: str, request: Request, response: Response,
                             session: DemoSession = Depends(require_demo_session),
                             db: Session = Depends(get_db)):
    require_write_protection(request, session)
    response.headers["Cache-Control"] = "no-store"
    return envelope(issue_confirmation_token(db, session_id=session.id, proposal_id=proposal_id))


@app.post("/api/proposals/{proposal_id}/confirm")
def confirm_route(proposal_id: str, payload: ConfirmPayload, request: Request,
                  response: Response, session: DemoSession = Depends(require_demo_session),
                  db: Session = Depends(get_db)):
    require_write_protection(request, session)
    response.headers["Cache-Control"] = "no-store"
    return envelope(confirm_proposal(db, session_id=session.id, proposal_id=proposal_id,
                                     confirmation_token=payload.confirmation_token))


@app.post("/api/proposals/{proposal_id}/cancel")
def cancel_route(proposal_id: str, request: Request,
                 session: DemoSession = Depends(require_demo_session),
                 db: Session = Depends(get_db)):
    require_write_protection(request, session)
    return envelope(cancel_proposal(db, session_id=session.id, proposal_id=proposal_id))


class AgentMessagePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=1000)


@app.get("/api/agent/tasks/current")
def agent_current_task(session: DemoSession = Depends(require_demo_session),
                       db: Session = Depends(get_db)):
    return envelope(current_agent_task(db, session))


@app.post("/api/tasks")
def create_task_route(payload: AgentMessagePayload, request: Request,
                      session: DemoSession = Depends(require_demo_session),
                      db: Session = Depends(get_db)):
    require_write_protection(request, session)
    mode = os.getenv("FINAGENT_MODE", "mock")
    return envelope(create_agent_task(db, session, mode, payload.message))


@app.post("/api/tasks/{task_id}/messages")
def task_message_route(task_id: str, payload: AgentMessagePayload, request: Request,
                       session: DemoSession = Depends(require_demo_session),
                       db: Session = Depends(get_db)):
    require_write_protection(request, session)
    return envelope(send_agent_message(db, session, task_id, payload.message))


class BirthdayDetailsPayload(BaseModel):
    model_config=ConfigDict(extra="forbid",strict=True)
    source_reference: str = Field(min_length=1,max_length=80)
    recipient: str = Field(min_length=1,max_length=60)
    address: str = Field(min_length=1,max_length=200)

class TransferDetailsPayload(BaseModel):
    model_config=ConfigDict(extra="forbid",strict=True)
    source_reference:str=Field(default='',max_length=80)
    amount_yuan:str=Field(default='',max_length=13,pattern=r'^$|^[0-9]{1,10}(\.[0-9]{1,2})?$')
    name: str = Field(min_length=1,max_length=80)
    bank_name: str = Field(min_length=1,max_length=80)
    account_number: str = Field(min_length=6,max_length=50)

@app.post("/api/tasks/{task_id}/birthday-details")
def birthday_details_route(task_id: str,payload:BirthdayDetailsPayload,request:Request,
                           session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db)):
    require_write_protection(request,session)
    from app.agent.forms import supplement_birthday
    result=supplement_birthday(db,session,task_id,{key:value.strip() for key,value in payload.model_dump().items()})
    from app.agent.dynamic_runtime import after_input
    return envelope(after_input(db,session,task_id,result))

@app.post("/api/tasks/{task_id}/transfer-details")
def transfer_details_route(task_id: str,payload:TransferDetailsPayload,request:Request,
                           session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db)):
    require_write_protection(request,session)
    from app.agent.forms import supplement_transfer
    result=supplement_transfer(db,session,task_id,payload.model_dump())
    from app.agent.dynamic_runtime import after_input
    return envelope(after_input(db,session,task_id,result))

@app.post("/api/tasks/{task_id}/cancel")
def cancel_task_route(task_id: str, request: Request,
                      session: DemoSession = Depends(require_demo_session),
                      db: Session = Depends(get_db)):
    require_write_protection(request, session)
    return envelope(cancel_agent_task(db, session, task_id))


@app.post("/api/tasks/{task_id}/reject-operation")
def reject_task_operation(task_id: str, request: Request,
                          session: DemoSession = Depends(require_demo_session), db: Session = Depends(get_db)):
    require_write_protection(request, session)
    from app.agent.service import _task, view
    from app.agent.crossflow import INTENTS, cancel_task
    task = _task(db, session, task_id)
    if task.intent not in INTENTS:
        fail("INVALID_TASK_STATE", "当前任务不支持独立拒绝操作", 409)
    cancel_task(db, session, task, only_operation=True)
    return envelope(view(db, task))


@app.post("/api/tasks/{task_id}/resume")
def resume_task(task_id: str, request: Request,
                session: DemoSession = Depends(require_demo_session), db: Session = Depends(get_db)):
    require_write_protection(request, session)
    from app.agent.service import _task, view
    from app.agent.crossflow import recover
    task = _task(db, session, task_id)
    recover(db, task, retry_read=True)
    return envelope(view(db, task))


class RecipientRemarkPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    remark: str | None = Field(default=None, max_length=80)


@app.patch('/api/recipients/{recipient_id}/remark')
def update_recipient_remark(recipient_id: str, payload: RecipientRemarkPayload, request: Request,
                            session: DemoSession = Depends(require_demo_session), db: Session = Depends(get_db)):
    from app.data.atomic import atomic_write
    from app.domain.operations import _session, _now
    require_write_protection(request, session)
    session_id = session.id
    with atomic_write(db):
        session, _ = _session(db, session_id, _now())
        recipient = db.scalar(select(Recipient).where(Recipient.id == recipient_id, Recipient.owner_id == session.owner_id))
        if recipient is None:
            raise DomainError('RESOURCE_NOT_FOUND', '资源不存在', 404)
        remark = payload.remark.strip() if payload.remark else None
        if recipient.remark != remark:
            recipient.remark = remark
            recipient.version += 1
        return envelope({'id': recipient.id, 'name': recipient.name, 'remark': recipient.remark,
                         'account_mask': '**'+recipient.account_last4, 'version':recipient.version})


from app.api.schedules import register_routes as register_schedule_routes
from app.api.collections import register_routes as register_collection_routes
register_schedule_routes(app, get_db, require_demo_session, require_write_protection, envelope)
register_collection_routes(app, get_db, require_demo_session, require_write_protection, envelope)
from app.api.wealth import register_routes as register_wealth_routes
register_wealth_routes(app, get_db, require_demo_session, require_write_protection, envelope)

from app.api.cards import register_routes as register_card_routes
register_card_routes(app,get_db,require_demo_session,require_write_protection,envelope)

from app.api.subscriptions import register_routes as register_subscription_routes
register_subscription_routes(app,get_db,require_demo_session,require_write_protection,envelope)
from app.api.financial_profile import register_routes as register_financial_routes
register_financial_routes(app,get_db,require_demo_session,envelope)

from app.api.birthdays import register_routes as register_birthday_routes
register_birthday_routes(app, get_db, require_demo_session, require_write_protection, envelope)
from app.api.security import register_routes as register_security_routes
register_security_routes(app, get_db, require_demo_session, require_write_protection, envelope)
from app.api.servicing import register_routes as register_servicing_routes
register_servicing_routes(app,get_db,require_demo_session,require_write_protection,envelope)
from app.api.control import register_routes as register_control_routes
register_control_routes(app, get_db, require_demo_session, require_write_protection, envelope)

from app.api.directory import register_routes as register_directory_routes
register_directory_routes(app, get_db, require_demo_session, require_write_protection, envelope)
