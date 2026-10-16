"""Symbolic schedule lifecycle: draft -> UI authorization -> due -> one transfer.

The interpreter can prepare a draft. Only the protected UI confirmation route
can persist a bounded authorization. The worker uses that authorization in one
atomic transaction and reuses the ordinary transfer confirmation and ledger.
"""
from __future__ import annotations

import hmac
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from threading import Event, Thread
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.atomic import atomic_write
from app.data.collection_models import CollectionRequest
from app.data.models import Account, AgentTask, DemoSession, DemoSetting, Recipient, Task
from app.data.scheduled_models import ScheduledTransfer
from app.domain.errors import DomainError
from app.domain.ledger import owned_account
from app.domain.money import require_cny, validate_transfer_amount
from app.domain.operations import (_audit, _canonical, _digest, _now, _session, _stamp,
                                   confirm_proposal, issue_confirmation_token, prepare_transfer)

LOGGER = logging.getLogger(__name__)
PENDING = "pending_confirmation"
TIMEZONE = "Asia/Shanghai"
DRAFT_SECONDS = 300
MAX_FUTURE_DAYS = 366


def _parse_execution(value: str, timezone_name: str, instant: datetime) -> datetime:
    if timezone_name != TIMEZONE:
        raise DomainError("INVALID_SCHEDULE_TIME", "预约时区必须明确为 Asia/Shanghai")
    try:
        if not isinstance(value, str):
            raise ValueError()
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError()
        result = result.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError) as exc:
        raise DomainError("INVALID_SCHEDULE_TIME", "预约时间必须包含明确日期和时区") from exc
    if result <= instant:
        raise DomainError("SCHEDULE_TIME_PASSED", "预约时间已到或已过，请重新选择未来时间", 409)
    if result > instant + timedelta(days=MAX_FUTURE_DAYS):
        raise DomainError("INVALID_SCHEDULE_TIME", "预约时间不能超过未来366天")
    return result


def _binding(db: Session, owner_id: str, params: dict) -> dict:
    source = owned_account(db, owner_id, params["source_account_id"])
    target = params["target"]
    source_binding = {"id": source.id, "alias": source.alias, "account_last4": source.account_last4,
                      "currency": source.currency}
    if target["type"] == "own_account":
        destination = owned_account(db, owner_id, target["account_id"])
        if source.id == destination.id:
            raise DomainError("INVALID_INPUT", "转出和转入账户不能相同")
        target_binding = {"id": destination.id, "name": destination.alias,
                          "account_last4": destination.account_last4, "currency": destination.currency}
    else:
        destination = db.scalar(select(Recipient).where(Recipient.id == target["recipient_id"],
                                                        Recipient.owner_id == owner_id))
        if destination is None:
            raise DomainError("RESOURCE_NOT_FOUND", "资源不存在", 404)
        if destination.status != "active":
            raise DomainError("INVALID_RECIPIENT_STATE", "收款人不可用")
        target_binding = {"id": destination.id, "name": destination.name,
                          "account_last4": destination.account_last4, "version": destination.version}
    return {"source": source_binding, "target": target_binding}


def _terms(row: ScheduledTransfer) -> dict:
    return {"params": json.loads(row.params_json), "target_binding": json.loads(row.target_binding_json),
            "execute_at_utc": row.execute_at_utc, "timezone": row.timezone,
            "owner_id": row.owner_id, "dataset_version": row.dataset_version}


def _authorization_digest(row: ScheduledTransfer) -> str:
    return _digest(_canonical({"schedule_id": row.id, "terms": _terms(row),
                               "confirmed_at_utc": row.confirmed_at_utc}))


def _validate_digest(row: ScheduledTransfer) -> None:
    try:
        valid = hmac.compare_digest(row.params_digest, _digest(_canonical(_terms(row))))
    except (ValueError, TypeError, KeyError) as exc:
        raise DomainError("SCHEDULE_PARAMETERS_CHANGED", "预约参数已变化，需要重新授权", 409) from exc
    if not valid:
        raise DomainError("SCHEDULE_PARAMETERS_CHANGED", "预约参数已变化，需要重新授权", 409)


def _owned(db: Session, session: DemoSession, schedule_id: str) -> ScheduledTransfer:
    row = db.scalar(select(ScheduledTransfer).where(ScheduledTransfer.id == schedule_id,
                   ScheduledTransfer.owner_id == session.owner_id,
                   ScheduledTransfer.dataset_version == session.dataset_version))
    if row is None:
        raise DomainError("RESOURCE_NOT_FOUND", "资源不存在", 404)
    return row


def _draft_session(row: ScheduledTransfer, session: DemoSession) -> None:
    if row.session_id != session.id:
        raise DomainError("SCHEDULE_CONFIRMATION_SESSION_CHANGED", "请在原会话确认预约，或取消后重新准备", 409)


def _sync_agent(db: Session, row: ScheduledTransfer) -> None:
    if not row.agent_task_id:
        return
    agent = db.get(AgentTask, row.agent_task_id)
    if agent and agent.owner_id == row.owner_id and agent.dataset_version == row.dataset_version:
        agent.status = {PENDING: "awaiting_confirmation", "executed": "completed", "expired": "failed"}.get(
            row.status, row.status)
        agent.updated_at_utc = row.updated_at_utc


def schedule_view(row: ScheduledTransfer, now: datetime | None = None) -> dict:
    instant = _now(now)
    params = json.loads(row.params_json)
    binding = json.loads(row.target_binding_json)
    status = "expired" if row.status == PENDING and datetime.fromisoformat(row.expires_at_utc) <= instant else row.status
    return {"id": row.id, "kind": "scheduled_transfer", "status": status,
            "agent_task_id": row.agent_task_id, "execute_at_utc": row.execute_at_utc,
            "execute_at_local": datetime.fromisoformat(row.execute_at_utc).astimezone(
                ZoneInfo(row.timezone)).isoformat(timespec="seconds"), "timezone": row.timezone,
            "created_at_utc": row.created_at_utc, "updated_at_utc": row.updated_at_utc,
            "expires_at_utc": row.expires_at_utc, "confirmed_at_utc": row.confirmed_at_utc,
            "summary": {"action": "scheduled_transfer", "source": {
                "id": binding["source"]["id"], "alias": binding["source"]["alias"],
                "mask": "**" + binding["source"]["account_last4"]}, "target": {
                "id": binding["target"]["id"], "alias": binding["target"]["name"],
                "mask": "**" + binding["target"]["account_last4"]},
                "target_type": params["target"]["type"], "amount_minor": params["amount_minor"],
                "currency": params["currency"], "note": params["note"],
                "impact": "确认仅授权预约，到期重新校验后执行模拟转账；未到时间不扣款"},
            "execution_proposal_id": row.execution_proposal_id,
            "receipt": json.loads(row.receipt_json) if row.receipt_json else None,
            "error_code": row.error_code}


from app.domain.security import preparing

@preparing
def prepare_schedule(db: Session, *, session_id: str, source_account_id: str, target: dict,
                     amount_minor: int, execute_at_utc: str, timezone_name: str = TIMEZONE,
                     currency: str = "CNY", note: str | None = None,
                     agent_task_id: str | None = None, now: datetime | None = None) -> dict:
    instant = _now(now)
    execution = _parse_execution(execute_at_utc, timezone_name, instant)
    require_cny(currency)
    validate_transfer_amount(amount_minor)
    if not isinstance(source_account_id, str) or not source_account_id:
        raise DomainError("INVALID_INPUT", "请选择转出账户")
    if note is not None and (not isinstance(note, str) or len(note) > 120):
        raise DomainError("INVALID_INPUT", "备注过长或格式无效")
    if not isinstance(target, dict) or target.get("type") not in {"own_account", "saved_recipient"}:
        raise DomainError("INVALID_INPUT", "无效转账目标")
    key = "account_id" if target["type"] == "own_account" else "recipient_id"
    if set(target) != {"type", key} or not isinstance(target.get(key), str) or not target[key]:
        raise DomainError("INVALID_INPUT", "请选择唯一转账目标")
    params = {"action": "transfer", "source_account_id": source_account_id, "target": target,
              "amount_minor": amount_minor, "currency": currency, "note": (note or "").strip()}
    with atomic_write(db):
        session, _ = _session(db, session_id, instant)
        from app.domain.wealth import ensure_no_pending_wealth
        ensure_no_pending_wealth(db, session.id, instant)
        if agent_task_id:
            agent = db.get(AgentTask, agent_task_id)
            if not agent or agent.owner_id != session.owner_id or agent.session_id != session.id or agent.dataset_version != session.dataset_version:
                raise DomainError("RESOURCE_NOT_FOUND", "资源不存在", 404)
        source = owned_account(db, session.owner_id, source_account_id)
        from app.domain.ledger import available_balance
        if available_balance(db, source) < amount_minor:
            raise DomainError("INSUFFICIENT_FUNDS", "账户一般可用余额不足")
        binding = _binding(db, session.owner_id, params)
        row = ScheduledTransfer(id=uuid4().hex, owner_id=session.owner_id, session_id=session.id,
            agent_task_id=agent_task_id, dataset_version=session.dataset_version, params_json=_canonical(params),
            target_binding_json=_canonical(binding), params_digest="", execute_at_utc=_stamp(execution),
            timezone=timezone_name, status=PENDING, expires_at_utc=_stamp(min(
                instant + timedelta(seconds=DRAFT_SECONDS), execution)),
            created_at_utc=_stamp(instant), updated_at_utc=_stamp(instant))
        row.params_digest = _digest(_canonical(_terms(row)))
        existing = db.scalar(select(ScheduledTransfer).where(ScheduledTransfer.owner_id == session.owner_id,
            ScheduledTransfer.session_id == session.id, ScheduledTransfer.dataset_version == session.dataset_version,
            ScheduledTransfer.params_digest == row.params_digest, ScheduledTransfer.status.in_([PENDING, "scheduled"])))
        if existing and (existing.status != PENDING or datetime.fromisoformat(existing.expires_at_utc) > instant):
            return {"schedule": schedule_view(existing, instant), "reused": True}
        active = db.scalar(select(Task).where(Task.active_session_id == session.id))
        if active:
            raise DomainError("ACTIVE_WRITE_TASK_EXISTS", "请先确认或取消当前操作单", 409)
        collection = db.scalar(select(CollectionRequest).where(CollectionRequest.session_id == session.id,
            CollectionRequest.status == PENDING, CollectionRequest.expires_at_utc > _stamp(instant)))
        if collection:
            raise DomainError("ACTIVE_WRITE_TASK_EXISTS", "请先确认或取消当前AA收款草稿", 409)
        other = db.scalar(select(ScheduledTransfer).where(ScheduledTransfer.session_id == session.id,
            ScheduledTransfer.status == PENDING, ScheduledTransfer.expires_at_utc > _stamp(instant)))
        if other:
            if not agent_task_id or other.agent_task_id != agent_task_id:
                raise DomainError("ACTIVE_WRITE_TASK_EXISTS", "请先确认或取消当前预约", 409)
            other.status = "cancelled"
            other.token_hash = None
            other.error_code = "PARAMETERS_CHANGED"
            other.updated_at_utc = _stamp(instant)
        db.add(row)
        db.flush()
        _audit(db, session.owner_id, None, "schedule_prepared", instant)
        _sync_agent(db, row)
        return {"schedule": schedule_view(row, instant), "reused": False}


def get_schedule(db: Session, *, session_id: str, schedule_id: str, now: datetime | None = None) -> dict:
    instant = _now(now)
    session, _ = _session(db, session_id, instant)
    return schedule_view(_owned(db, session, schedule_id), instant)


def list_schedules(db: Session, *, session_id: str, now: datetime | None = None) -> list[dict]:
    instant = _now(now)
    session, _ = _session(db, session_id, instant)
    rows = db.scalars(select(ScheduledTransfer).where(ScheduledTransfer.owner_id == session.owner_id,
        ScheduledTransfer.dataset_version == session.dataset_version).order_by(ScheduledTransfer.created_at_utc.desc(),
        ScheduledTransfer.id).limit(100)).all()
    return [schedule_view(row, instant) for row in rows]


def _expire(db: Session, row: ScheduledTransfer, instant: datetime) -> bool:
    if row.status == PENDING and datetime.fromisoformat(row.expires_at_utc) <= instant:
        row.status = "expired"
        row.token_hash = None
        row.updated_at_utc = _stamp(instant)
        row.error_code = "SCHEDULE_CONFIRMATION_EXPIRED"
        _sync_agent(db, row)
        return True
    return False


def issue_schedule_confirmation_token(db: Session, *, session_id: str, schedule_id: str,
                                      now: datetime | None = None) -> dict:
    instant = _now(now)
    result = None
    error = None
    with atomic_write(db):
        session, _ = _session(db, session_id, instant)
        row = _owned(db, session, schedule_id)
        _draft_session(row, session)
        if _expire(db, row, instant):
            error = DomainError("SCHEDULE_CONFIRMATION_EXPIRED", "预约确认已过期，请重新准备", 409)
        elif row.status != PENDING:
            error = DomainError("INVALID_SCHEDULE_STATE", "预约已不再等待确认", 409)
        else:
            _validate_digest(row)
            token = secrets.token_urlsafe(32)
            row.token_hash = _digest(token)
            row.updated_at_utc = _stamp(instant)
            _audit(db, row.owner_id, None, "schedule_token_issued", instant)
            result = {"confirmation_token": token, "expires_at_utc": row.expires_at_utc}
    if error:
        raise error
    return result


from app.domain.security import secured

@secured('schedule', 'schedule_id')
def confirm_schedule(db: Session, *, session_id: str, schedule_id: str, confirmation_token: str,
                     now: datetime | None = None) -> dict:
    instant = _now(now)
    error = None
    with atomic_write(db):
        session, _ = _session(db, session_id, instant)
        row = _owned(db, session, schedule_id)
        _draft_session(row, session)
        if row.status in {"scheduled", "executed"}:
            return schedule_view(row, instant)
        if _expire(db, row, instant):
            error = DomainError("SCHEDULE_CONFIRMATION_EXPIRED", "预约确认已过期，请重新准备", 409)
        elif row.status != PENDING:
            error = DomainError("INVALID_SCHEDULE_STATE", "预约已不再等待确认", 409)
        elif not isinstance(confirmation_token, str) or not row.token_hash or not hmac.compare_digest(
                row.token_hash, _digest(confirmation_token)):
            _audit(db, row.owner_id, None, "schedule_confirmation_rejected", instant, "CONFIRMATION_INVALID")
            error = DomainError("CONFIRMATION_INVALID", "确认凭据无效", 403)
        else:
            _validate_digest(row)
            params = json.loads(row.params_json)
            if _binding(db, row.owner_id, params) != json.loads(row.target_binding_json):
                raise DomainError("STALE_RESOURCE", "收款目标已变化，请重新准备预约", 409)
            row.status = "scheduled"
            row.confirmed_at_utc = _stamp(instant)
            row.authorization_digest = _authorization_digest(row)
            row.token_hash = None
            row.updated_at_utc = _stamp(instant)
            _audit(db, row.owner_id, None, "schedule_authorized", instant)
            _sync_agent(db, row)
        result = schedule_view(row, instant)
    if error:
        raise error
    return result


def cancel_schedule(db: Session, *, session_id: str, schedule_id: str, now: datetime | None = None) -> dict:
    instant = _now(now)
    with atomic_write(db):
        session, _ = _session(db, session_id, instant)
        row = _owned(db, session, schedule_id)
        if row.status in {PENDING, "scheduled"}:
            row.status = "cancelled"
            row.token_hash = None
            row.authorization_digest = None
            row.updated_at_utc = _stamp(instant)
            _audit(db, row.owner_id, None, "schedule_cancelled", instant)
            _sync_agent(db, row)
        return schedule_view(row, instant)


def _execute_due(db: Session, schedule_id: str, instant: datetime) -> dict:
    with atomic_write(db):
        row = db.get(ScheduledTransfer, schedule_id)
        if row is None:
            return {}
        if row.status != "scheduled" or datetime.fromisoformat(row.execute_at_utc) > instant:
            return schedule_view(row, instant)
        try:
            # A savepoint rolls back private sessions/proposals/ledger together
            # before persisting a terminal validation failure on this schedule.
            with db.begin_nested():
                _validate_digest(row)
                if not row.confirmed_at_utc or not row.authorization_digest or not hmac.compare_digest(
                        row.authorization_digest, _authorization_digest(row)):
                    raise DomainError("SCHEDULE_NOT_AUTHORIZED", "预约缺少有效的用户授权", 409)
                setting = db.get(DemoSetting, 1)
                if not setting or setting.dataset_version != row.dataset_version:
                    raise DomainError("SCHEDULE_DATASET_CHANGED", "演示数据已变化，预约失效", 409)
                params = json.loads(row.params_json)
                from app.domain.security import check_grant, schedule_terms, policy
                check_grant(db, row.owner_id, 'schedule:' + row.id, schedule_terms(row),
                            policy(db, row.owner_id, 'transfer', params['amount_minor'])['level'])
                db.info['scheduled_security_grant'] = {'id': row.id, 'terms': schedule_terms(row), 'params': params}
                if _binding(db, row.owner_id, params) != json.loads(row.target_binding_json):
                    raise DomainError("STALE_RESOURCE", "收款目标已变化，预约未执行", 409)
                private_session = DemoSession(id=uuid4().hex, owner_id=row.owner_id,
                    dataset_version=row.dataset_version, expires_at_utc=_stamp(instant + timedelta(minutes=5)),
                    csrf_token="")
                db.add(private_session)
                db.flush()
                prepared = prepare_transfer(db, session_id=private_session.id,
                    source_account_id=params["source_account_id"], target=params["target"],
                    amount_minor=params["amount_minor"], currency=params["currency"], note=params["note"],
                    now=instant, business_at=instant)
                proposal_id = prepared["proposal"]["id"]
                token = issue_confirmation_token(db, session_id=private_session.id,
                    proposal_id=proposal_id, now=instant)["confirmation_token"]
                receipt = confirm_proposal(db, session_id=private_session.id, proposal_id=proposal_id,
                    confirmation_token=token, now=instant, business_at=instant)
                db.delete(private_session)
                row.execution_proposal_id = proposal_id
                row.receipt_json = _canonical({**receipt, "schedule_id": row.id,
                    "scheduled_for_utc": row.execute_at_utc, "executed_at_utc": _stamp(instant)})
                row.status = "executed"
                row.updated_at_utc = _stamp(instant)
                _audit(db, row.owner_id, proposal_id, "schedule_executed", instant)
        except DomainError as error:
            row = db.get(ScheduledTransfer, schedule_id)
            row.status = "failed"
            row.error_code = error.code
            row.updated_at_utc = _stamp(instant)
            _audit(db, row.owner_id, None, "schedule_failed", instant, error.code)
        finally:
            db.info.pop('scheduled_security_grant', None)
        _sync_agent(db, row)
        return schedule_view(row, instant)


def run_due_schedules(session_factory, *, now: datetime | None = None, limit: int = 20) -> list[dict]:
    """Polling is safe across workers; authorization and state are rechecked under the write lock."""
    instant = _now(now)
    with session_factory() as db:
        ids = list(db.scalars(select(ScheduledTransfer.id).where(ScheduledTransfer.status == "scheduled",
            ScheduledTransfer.execute_at_utc <= _stamp(instant)).order_by(ScheduledTransfer.execute_at_utc,
            ScheduledTransfer.id).limit(limit)).all())
    result = []
    for schedule_id in ids:
        with session_factory() as db:
            result.append(_execute_due(db, schedule_id, instant))
    return result


class ScheduleWorker:
    def __init__(self, session_factory, interval_seconds: float = 1):
        self.session_factory = session_factory
        self.interval_seconds = max(0.05, interval_seconds)
        self._stop = Event()
        self._thread = Thread(target=self._loop, name="finagent-schedules", daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)

    def _loop(self):
        while not self._stop.is_set():
            try:
                run_due_schedules(self.session_factory)
            except Exception:
                LOGGER.exception("Scheduled transfer poll failed; durable schedules remain recoverable")
            self._stop.wait(self.interval_seconds)


def start_schedule_worker(session_factory, *, interval_seconds: float = 1) -> ScheduleWorker:
    return ScheduleWorker(session_factory, interval_seconds).start()
