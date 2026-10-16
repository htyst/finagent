"""Conservative recurring-payment evidence and explicitly confirmed bank mandates.

The subscription business clock is separate from session/token time and from the
ordinary scheduled-transfer clock. Production always uses actual UTC. Test-only
clock files require an explicit switch and an exact isolated-database binding.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone as utc_timezone
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

from sqlalchemy import select

from app.data.atomic import atomic_write
from app.data.models import AgentStep, AgentTask, AuditEvent, DemoSetting, Task, Transaction
from app.data.subscription_models import (SubscriptionAuthorization, SubscriptionCharge,
    SubscriptionNotification, SubscriptionReminder, SubscriptionRequest)
from app.domain.clock import SHANGHAI, shifted_month
from app.domain.errors import DomainError
from app.domain.ledger import owned_account, post_entry
from app.domain.money import MAX_MINOR
from app.domain.operations import _canonical, _digest, _now, _session, _stamp

LOGGER = logging.getLogger(__name__)
LABELS = {"reminder": "设置续费提醒", "revoke": "解除银行代扣授权"}
RULES = ("疑似订阅仅由本人同账户、同商户、同金额的至少三次连续月份同日已入账消费识别，"
         "流水规律不代表已确认商户订阅合同。银行代扣为本地模拟授权，每月约定日北京时间09:00扣费；"
         "月末不足约定日时取当月末日。仅提供站内持久通知，后台每秒检查一次；停机后恢复时补发，"
         "保留原计划时间和实际触达时间，每轮最多处理20个到期事项。解除银行代扣不取消商户会员或合同，"
         "商户侧自动续费及会员状态需另向商户处理。取消提醒仅停止通知，不解除代扣。")


def _bound_path(db) -> Path:
    value = db.get_bind().url.database
    if not value or value == ":memory:":
        raise DomainError("TEST_CLOCK_GUARD", "测试时钟必须绑定独立数据库", 500)
    return Path(value).resolve()


def _is_test_path(path: Path) -> bool:
    return any(part.upper() == "TEST-ONLY" for part in path.parts)


def subscription_now(db=None) -> datetime:
    """Use actual UTC unless an explicitly guarded subscription test clock is enabled."""
    if os.getenv("FINAGENT_SUBSCRIPTIONS_TEST_CLOCK_ENABLED") != "1":
        return _now()
    if db is None:
        raise DomainError("TEST_CLOCK_GUARD", "测试时钟缺少数据库绑定", 500)
    actual = _bound_path(db)
    requested = os.getenv("FINAGENT_SUBSCRIPTIONS_TEST_DB", "")
    filename = os.getenv("FINAGENT_SUBSCRIPTIONS_TEST_CLOCK_FILE", "")
    if not requested or not filename:
        raise DomainError("TEST_CLOCK_GUARD", "测试时钟缺少显式路径", 500)
    expected, clock_path = Path(requested).resolve(), Path(filename).resolve()
    if actual != expected or not _is_test_path(actual) or clock_path.parent != actual.parent:
        raise DomainError("TEST_CLOCK_GUARD", "测试时钟仅允许同目录TEST-ONLY隔离数据库", 500)
    try:
        if clock_path.stat().st_size > 4096:
            raise ValueError("clock file too large")
        payload = json.loads(clock_path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict) or set(payload) != {"now_utc"} or not isinstance(payload["now_utc"], str):
            raise ValueError("invalid clock contract")
        return _now(datetime.fromisoformat(payload["now_utc"]))
    except (OSError, ValueError, TypeError) as exc:
        raise DomainError("TEST_CLOCK_GUARD", "测试时钟文件无效", 500) from exc


def guarded_subscription_clock(session_factory=None):
    """Worker clock factory; every read revalidates file and database ownership."""
    if session_factory is None:
        if os.getenv("FINAGENT_SUBSCRIPTIONS_TEST_CLOCK_ENABLED") == "1":
            raise DomainError("TEST_CLOCK_GUARD", "测试时钟须指定独立会话工厂", 500)
        return _now
    def current():
        with session_factory() as db:
            return subscription_now(db)
    current()  # Fail closed at startup, before any background processing.
    return current


def _instant(value: str) -> datetime:
    try:
        return _now(datetime.fromisoformat(value))
    except (ValueError, TypeError) as exc:
        raise DomainError("INVALID_INPUT", "订阅时间必须包含时区") from exc


def authorization_view(row):
    return {key: getattr(row, key) for key in ("id", "account_id", "merchant", "amount_minor", "period",
        "charge_day", "next_charge_at_utc", "timezone", "status", "version", "created_at_utc", "revoked_at_utc")}


def identify(db, session_id):
    """Derive candidates directly from ledger rows, never from the mandate directory."""
    from app.domain.security import assert_subscription_access
    assert_subscription_access(db, session_id)
    session, _ = _session(db, session_id, _now())
    groups = defaultdict(list)
    for row in db.scalars(select(Transaction).where(Transaction.owner_id == session.owner_id,
        Transaction.type == "purchase", Transaction.direction == "debit", Transaction.status == "posted",
        Transaction.currency == "CNY").order_by(Transaction.posted_at_utc, Transaction.id)):
        merchant = (row.merchant or "").strip()
        if merchant:
            groups[(row.account_id, merchant, row.amount_minor)].append(row)
    result = []
    for (account_id, merchant, amount), rows in groups.items():
        days = [_instant(row.posted_at_utc).astimezone(SHANGHAI).date() for row in rows]
        months = [day.year * 12 + day.month for day in days]
        if (len(rows) < 3 or len({day.day for day in days}) != 1 or
                any(right != left + 1 for left, right in zip(months, months[1:]))):
            continue
        next_day = shifted_month(days[-1], 1)
        predicted = datetime.combine(next_day, time(9), SHANGHAI).astimezone(utc_timezone.utc)
        authorization = db.scalar(select(SubscriptionAuthorization).where(
            SubscriptionAuthorization.owner_id == session.owner_id,
            SubscriptionAuthorization.dataset_version == session.dataset_version,
            SubscriptionAuthorization.account_id == account_id,
            SubscriptionAuthorization.merchant == merchant,
            SubscriptionAuthorization.amount_minor == amount))
        result.append(dict(id=_digest(_canonical([account_id, merchant, amount])), account_id=account_id,
            merchant=merchant, amount_minor=amount, period="monthly", period_label="每月",
            next_charge_at_utc=_stamp(predicted), timezone="Asia/Shanghai", suspected=True,
            authorization_id=authorization.id if authorization else None,
            evidence=[dict(id=r.id, posted_at_utc=r.posted_at_utc, amount_minor=r.amount_minor,
                merchant=r.merchant) for r in rows]))
    return sorted(result, key=lambda item: (item["merchant"], item["account_id"], item["amount_minor"]))


def request_view(row, now=None):
    status = ("expired" if row.status == "pending_confirmation" and _instant(row.expires_at_utc) <= _now(now)
              else row.status)
    return dict(id=row.id, authorization_id=row.authorization_id, kind=row.kind, label=LABELS[row.kind],
        status=status, summary=json.loads(row.terms_json), created_at_utc=row.created_at_utc,
        expires_at_utc=row.expires_at_utc, completed_at_utc=row.completed_at_utc,
        receipt=json.loads(row.receipt_json) if row.receipt_json else None)


def reminder_view(db, row):
    auth = db.get(SubscriptionAuthorization, row.authorization_id)
    return dict(id=row.id, authorization_id=row.authorization_id, request_id=row.request_id,
        merchant=auth.merchant, amount_minor=row.amount_minor, charge_at_utc=row.charge_at_utc,
        fire_at_utc=row.fire_at_utc, days_before=row.days_before, hour=row.hour, minute=row.minute,
        timezone=row.timezone, status=row.status, created_at_utc=row.created_at_utc,
        delivered_at_utc=row.delivered_at_utc)


def notification_view(row):
    return {key: getattr(row, key) for key in ("id", "reminder_id", "title", "body", "scheduled_at_utc",
        "delivered_at_utc", "recorded_at_utc")}


def charge_view(db, row):
    messages = {"INSUFFICIENT_FUNDS": "余额不足，本次代扣失败", "INVALID_ACCOUNT_STATE": "账户状态不可用，本次代扣失败",
        "DATASET_CHANGED": "演示数据已变化，本次代扣未执行", "INVALID_INPUT": "代扣条款无效，本次未扣款"}
    auth = db.get(SubscriptionAuthorization, row.authorization_id)
    return dict(id=row.id, authorization_id=row.authorization_id, merchant=auth.merchant,
        amount_minor=row.amount_minor, due_at_utc=row.due_at_utc, status=row.status,
        error_code=row.error_code, transaction_id=row.transaction_id, balance_minor=row.balance_minor,
        created_at_utc=row.created_at_utc, message=("模拟代扣已入账" if row.status == "posted" else
            "银行代扣已解除，本次未扣款" if row.status == "blocked" else messages.get(row.error_code, "本次代扣失败")))


def get_portfolio(db, session_id):
    from app.domain.security import assert_subscription_access
    assert_subscription_access(db, session_id)
    session, _ = _session(db, session_id, _now())
    auths = list(db.scalars(select(SubscriptionAuthorization).where(
        SubscriptionAuthorization.owner_id == session.owner_id,
        SubscriptionAuthorization.dataset_version == session.dataset_version).order_by(SubscriptionAuthorization.merchant)))
    auth_ids = [auth.id for auth in auths]
    def related(model):
        return list(db.scalars(select(model).where(model.owner_id == session.owner_id,
            model.authorization_id.in_(auth_ids))))
    reminders = related(SubscriptionReminder)
    reminder_ids = [reminder.id for reminder in reminders]
    notifications = list(db.scalars(select(SubscriptionNotification).where(
        SubscriptionNotification.owner_id == session.owner_id,
        SubscriptionNotification.reminder_id.in_(reminder_ids)).order_by(SubscriptionNotification.recorded_at_utc.desc())))
    requests = list(db.scalars(select(SubscriptionRequest).where(SubscriptionRequest.owner_id == session.owner_id,
        SubscriptionRequest.dataset_version == session.dataset_version).order_by(SubscriptionRequest.created_at_utc.desc())))
    # Opening a panel reads the latest explicit identification step; it does not
    # run the Agent's identification task or pre-supply the answer before 29.
    previous = db.scalar(select(AgentStep.result_json).join(AgentTask, AgentStep.task_id == AgentTask.id).where(
        AgentTask.owner_id == session.owner_id, AgentTask.dataset_version == session.dataset_version,
        AgentStep.tool_name == "identify_subscriptions").order_by(AgentStep.created_at_utc.desc(), AgentStep.id.desc()))
    candidates = json.loads(previous).get("candidates", []) if previous else []
    return dict(rules=RULES, business_now_utc=_stamp(subscription_now(db)), candidates=candidates,
        authorizations=[authorization_view(row) for row in auths], requests=[request_view(row) for row in requests],
        reminders=[reminder_view(db, row) for row in reminders],
        notifications=[notification_view(row) for row in notifications],
        charges=[charge_view(db, row) for row in related(SubscriptionCharge)])


def _owned_authorization(db, session, authorization_id):
    row = db.scalar(select(SubscriptionAuthorization).where(SubscriptionAuthorization.id == authorization_id,
        SubscriptionAuthorization.owner_id == session.owner_id,
        SubscriptionAuthorization.dataset_version == session.dataset_version))
    if row is None:
        raise DomainError("RESOURCE_NOT_FOUND", "资源不存在", 404)
    return row


def _owned_request(db, session, request_id):
    row = db.scalar(select(SubscriptionRequest).where(SubscriptionRequest.id == request_id,
        SubscriptionRequest.owner_id == session.owner_id, SubscriptionRequest.dataset_version == session.dataset_version))
    if row is None:
        raise DomainError("RESOURCE_NOT_FOUND", "资源不存在", 404)
    if row.session_id != session.id:
        raise DomainError("INVALID_ORDER_SESSION", "请在原会话办理", 409)
    return row


def sync(db, row):
    task = db.get(AgentTask, row.agent_task_id) if row.agent_task_id else None
    if task and json.loads(task.context_json).get("subscription_request_id") == row.id:
        if (task.intent or '').startswith('subscription_'):
            status = {"executed": "completed", "pending_confirmation": "awaiting_confirmation",
                "invalidated": "gathering_info"}.get(row.status, row.status)
            if task.status != status:
                task.status = status
                task.updated_at_utc = _stamp(_now())
        for step in db.scalars(select(AgentStep).where(AgentStep.task_id == task.id, AgentStep.result_ref == row.id)):
            step.status = {"executed": "succeeded", "pending_confirmation": "awaiting_confirmation"}.get(row.status, row.status)
            step.result_json = _canonical({"subscription_request": request_view(row)})


def ensure_no_pending(db, session_id, instant=None):
    from app.domain.birthdays import ensure_no_pending as ensure_no_pending_birthday
    ensure_no_pending_birthday(db, session_id, instant)
    instant = _now(instant)
    if db.scalar(select(SubscriptionRequest.id).where(SubscriptionRequest.session_id == session_id,
        SubscriptionRequest.status == "pending_confirmation", SubscriptionRequest.expires_at_utc > _stamp(instant))):
        raise DomainError("ACTIVE_WRITE_TASK_EXISTS", "请先确认或取消当前订阅办理单", 409)


def invalidate_task(db, task):
    rid = json.loads(task.context_json).get("subscription_request_id")
    row = db.get(SubscriptionRequest, rid) if rid else None
    if row and row.agent_task_id == task.id and row.owner_id == task.owner_id and row.status == "pending_confirmation":
        row.status = "invalidated"
        row.token_hash = None
        sync(db, row)


def invalidate_session(db, session_id):
    for row in db.scalars(select(SubscriptionRequest).where(SubscriptionRequest.session_id == session_id,
        SubscriptionRequest.status == "pending_confirmation")):
        row.status = "invalidated"
        row.token_hash = None
        sync(db, row)
        task = db.get(AgentTask, row.agent_task_id) if row.agent_task_id else None
        if task:
            task.stop_requested = True


def _fixed(row):
    if not hmac.compare_digest(row.terms_digest, _digest(row.terms_json)):
        raise DomainError("REQUEST_CHANGED", "固定办理条款已变化", 409)
    try:
        terms = json.loads(row.terms_json)
        if terms["kind"] != row.kind or terms["authorization_id"] != row.authorization_id or terms["scope"] != "bank_debit_authorization":
            raise ValueError("request binding changed")
        return terms
    except (ValueError, KeyError, TypeError) as exc:
        raise DomainError("REQUEST_CHANGED", "办理字段与固定条款不一致", 409) from exc


def _draft(db, session, row, instant):
    if row.status != "pending_confirmation" or _instant(row.expires_at_utc) <= instant:
        raise DomainError("INVALID_ORDER_STATE", "办理单已失效或过期", 409)
    if row.agent_task_id:
        task = db.get(AgentTask, row.agent_task_id)
        if not task or task.stop_requested or task.session_id != session.id or task.owner_id != session.owner_id:
            raise DomainError("INVALID_TASK_STATE", "对话任务已停止", 409)
    _fixed(row)


from app.domain.security import preparing

@preparing
def prepare(db, *, session_id, kind, authorization_id, days_before=2, hour=9, minute=0,
            timezone="Asia/Shanghai", agent_task_id=None, now=None, business_now=None):
    if kind not in LABELS or timezone != "Asia/Shanghai":
        raise DomainError("INVALID_INPUT", "未知订阅业务或时区")
    if kind == "reminder" and (type(days_before) is not int or not 1 <= days_before <= 31 or
            type(hour) is not int or not 0 <= hour <= 23 or type(minute) is not int or not 0 <= minute <= 59):
        raise DomainError("INVALID_INPUT", "提醒提前天数或时间无效")
    instant = _now(now)
    with atomic_write(db):
        session, _ = _session(db, session_id, instant)
        if agent_task_id:
            task = db.get(AgentTask, agent_task_id)
            if not task or task.session_id != session.id or task.owner_id != session.owner_id or task.stop_requested:
                raise DomainError("INVALID_TASK_STATE", "对话任务已停止", 409)
        auth = _owned_authorization(db, session, authorization_id)
        if auth.status != "active":
            raise DomainError("AUTHORIZATION_REVOKED", "银行代扣已解除，不能继续设置续费提醒或重复解除", 409)
        account = owned_account(db, session.owner_id, auth.account_id)
        if type(auth.amount_minor) is not int or not 0 < auth.amount_minor <= MAX_MINOR:
            raise DomainError("INVALID_INPUT", "代扣金额无效")
        terms = dict(kind=kind, authorization_id=auth.id, authorization_version=auth.version,
            merchant=auth.merchant, account_id=auth.account_id, account_alias=account.alias,
            amount_minor=auth.amount_minor, next_charge_at_utc=_stamp(_instant(auth.next_charge_at_utc)),
            timezone=auth.timezone, period=auth.period, scope="bank_debit_authorization")
        if kind == "reminder":
            charge = _instant(auth.next_charge_at_utc).astimezone(SHANGHAI)
            day = charge.date() - timedelta(days=days_before)
            fire = datetime.combine(day, time(hour, minute), SHANGHAI).astimezone(utc_timezone.utc)
            business = _now(business_now) if business_now else subscription_now(db)
            if fire <= business:
                raise DomainError("REMINDER_TIME_PASSED", "该次提醒时间已过，请明确下一次续费安排", 409)
            terms.update(days_before=days_before, hour=hour, minute=minute, fire_at_utc=_stamp(fire))
        digest = _digest(_canonical(terms))
        for old in db.scalars(select(SubscriptionRequest).where(SubscriptionRequest.session_id == session.id,
            SubscriptionRequest.status == "pending_confirmation")):
            if _instant(old.expires_at_utc) <= instant:
                old.status = "expired"
                old.token_hash = None
                sync(db, old)
            elif old.agent_task_id == agent_task_id and old.terms_digest == digest:
                return request_view(old, instant)
            elif agent_task_id and old.agent_task_id == agent_task_id:
                old.status = "invalidated"
                old.token_hash = None
                sync(db, old)
            else:
                raise DomainError("ACTIVE_WRITE_TASK_EXISTS", "请先处理已有订阅办理单", 409)
        from app.data.card_models import CardRequest
        from app.data.collection_models import CollectionRequest
        from app.data.scheduled_models import ScheduledTransfer
        from app.data.wealth_models import WealthOrder
        if db.scalar(select(Task.id).where(Task.active_session_id == session.id)) or any(
                db.scalar(select(model.id).where(model.session_id == session.id,
                    model.status == "pending_confirmation", model.expires_at_utc > _stamp(instant)))
                for model in (CardRequest, CollectionRequest, ScheduledTransfer, WealthOrder)):
            raise DomainError("ACTIVE_WRITE_TASK_EXISTS", "请先确认或取消其他待确认业务", 409)
        row = SubscriptionRequest(id=uuid4().hex, owner_id=session.owner_id, session_id=session.id,
            dataset_version=session.dataset_version, agent_task_id=agent_task_id, authorization_id=auth.id,
            kind=kind, status="pending_confirmation", terms_json=_canonical(terms), terms_digest=digest,
            created_at_utc=_stamp(instant), expires_at_utc=_stamp(instant + timedelta(minutes=5)))
        db.add(row)
        db.flush()
        return request_view(row, instant)


def issue_token(db, *, session_id, request_id, now=None):
    instant = _now(now)
    with atomic_write(db):
        session, _ = _session(db, session_id, instant)
        row = _owned_request(db, session, request_id)
        _draft(db, session, row, instant)
        token = secrets.token_urlsafe(32)
        row.token_hash = _digest(token)
        return {"confirmation_token": token}


from app.domain.security import secured

@secured('subscription', 'request_id')
def confirm(db, *, session_id, request_id, confirmation_token, now=None, business_now=None):
    instant = _now(now)
    with atomic_write(db):
        session, _ = _session(db, session_id, instant)
        row = _owned_request(db, session, request_id)
        if row.status == "executed":
            return request_view(row, instant)
        _draft(db, session, row, instant)
        if not isinstance(confirmation_token, str) or not row.token_hash or not hmac.compare_digest(
                row.token_hash, _digest(confirmation_token)):
            raise DomainError("CONFIRMATION_INVALID", "确认凭据无效", 403)
        terms = _fixed(row)
        auth = _owned_authorization(db, session, row.authorization_id)
        current = dict(authorization_version=auth.version, merchant=auth.merchant, account_id=auth.account_id,
            amount_minor=auth.amount_minor, next_charge_at_utc=_stamp(_instant(auth.next_charge_at_utc)),
            timezone=auth.timezone, period=auth.period)
        if auth.status != "active" or any(terms.get(key) != value for key, value in current.items()):
            raise DomainError("STALE_RESOURCE", "代扣授权或预计周期已变化，请重新准备", 409)
        owned_account(db, session.owner_id, auth.account_id)
        receipt = dict(request_id=row.id, authorization_id=auth.id, merchant=auth.merchant,
            scope="bank_debit_authorization", amount_minor=auth.amount_minor,
            next_charge_at_utc=terms["next_charge_at_utc"], completed_at_utc=_stamp(instant))
        if row.kind == "reminder":
            business = _now(business_now) if business_now else subscription_now(db)
            if _instant(terms["fire_at_utc"]) <= business:
                raise DomainError("REMINDER_TIME_PASSED", "提醒时间已过，请重新准备下一次安排", 409)
            # A new explicit setting replaces any older untriggered reminder for this cycle.
            for old in db.scalars(select(SubscriptionReminder).where(
                    SubscriptionReminder.authorization_id == auth.id,
                    SubscriptionReminder.status == "scheduled")):
                old.status = "cancelled"
            reminder = SubscriptionReminder(id=uuid4().hex, owner_id=auth.owner_id,
                authorization_id=auth.id, request_id=row.id, charge_at_utc=terms["next_charge_at_utc"],
                fire_at_utc=terms["fire_at_utc"], days_before=terms["days_before"], hour=terms["hour"],
                minute=terms["minute"], timezone=terms["timezone"], amount_minor=terms["amount_minor"],
                status="scheduled", created_at_utc=_stamp(instant))
            db.add(reminder)
            receipt.update(reminder_id=reminder.id, fire_at_utc=reminder.fire_at_utc,
                message="续费提醒已预约；到时发送站内通知，离开当前对话不影响提醒")
        else:
            auth.status = "revoked"
            auth.version += 1
            auth.revoked_at_utc = _stamp(instant)
            for reminder in db.scalars(select(SubscriptionReminder).where(
                    SubscriptionReminder.authorization_id == auth.id, SubscriptionReminder.status == "scheduled")):
                reminder.status = "cancelled"
            receipt.update(authorization_status="revoked",
                message="银行代扣授权已解除，该授权下后续扣款将被阻止；商户会员或合同未取消，须另向商户处理")
        row.status = "executed"
        row.completed_at_utc = _stamp(instant)
        row.token_hash = None
        row.receipt_json = _canonical(receipt)
        db.add(AuditEvent(id=uuid4().hex, owner_id=session.owner_id,
            event="subscription_" + row.kind + "_confirmed", created_at_utc=_stamp(instant)))
        db.flush()
        sync(db, row)
        return request_view(row, instant)


def cancel(db, *, session_id, request_id, now=None):
    instant = _now(now)
    with atomic_write(db):
        session, _ = _session(db, session_id, instant)
        row = _owned_request(db, session, request_id)
        if row.status == "pending_confirmation":
            row.status = "cancelled"
            row.token_hash = None
            sync(db, row)
            task = db.get(AgentTask, row.agent_task_id) if row.agent_task_id else None
            if task:
                task.stop_requested = True
        return request_view(row, instant)


def cancel_reminder(db, *, session_id, reminder_id, now=None):
    instant = _now(now)
    with atomic_write(db):
        session, _ = _session(db, session_id, instant)
        row = db.scalar(select(SubscriptionReminder).where(SubscriptionReminder.id == reminder_id,
            SubscriptionReminder.owner_id == session.owner_id))
        if row is None:
            raise DomainError("RESOURCE_NOT_FOUND", "资源不存在", 404)
        _owned_authorization(db, session, row.authorization_id)
        if row.status == "scheduled":
            row.status = "cancelled"
            db.add(AuditEvent(id=uuid4().hex, owner_id=session.owner_id,
                event="subscription_reminder_cancelled", created_at_utc=_stamp(instant)))
        return reminder_view(db, row)


def task_state(db, task):
    rid = json.loads(task.context_json).get("subscription_request_id")
    row = db.get(SubscriptionRequest, rid) if rid else None
    if row and row.owner_id == task.owner_id and row.session_id == task.session_id:
        if row.status == "pending_confirmation" and _instant(row.expires_at_utc) <= _now():
            row.status = "expired"
            row.token_hash = None
        sync(db, row)
        return {"subscription_request": request_view(row)}
    return {"subscription_request": None}


def _notify_due(db, reminder_id, business):
    with atomic_write(db):
        row = db.get(SubscriptionReminder, reminder_id)
        if row is None or row.status != "scheduled" or _instant(row.fire_at_utc) > business:
            return None
        auth = db.get(SubscriptionAuthorization, row.authorization_id)
        setting = db.get(DemoSetting, 1)
        if (not auth or auth.status != "active" or setting is None or
                auth.dataset_version != setting.dataset_version):
            row.status = "cancelled"
            return None
        charge_date = _instant(row.charge_at_utc).astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M")
        notification = SubscriptionNotification(id=uuid4().hex, owner_id=row.owner_id, reminder_id=row.id,
            title=auth.merchant + "续费提醒",
            body=f"{auth.merchant}预计于{charge_date}（北京时间）扣费{row.amount_minor / 100:.2f}元。"
                "可在订阅/代扣管理解除银行代扣授权；商户会员或合同、商户续费需另向商户处理。",
            scheduled_at_utc=row.fire_at_utc, delivered_at_utc=_stamp(business), recorded_at_utc=_stamp(_now()))
        db.add(notification)
        row.status = "delivered"
        row.delivered_at_utc = _stamp(business)
        db.flush()
        return notification_view(notification)


def _next_charge(auth, due):
    local = due.astimezone(SHANGHAI)
    day = shifted_month(local.date(), 1, auth.charge_day)
    return datetime.combine(day, time(9), SHANGHAI).astimezone(utc_timezone.utc)


def _charge_due(db, authorization_id, business):
    with atomic_write(db):
        auth = db.get(SubscriptionAuthorization, authorization_id)
        if auth is None:
            return None
        due = _instant(auth.next_charge_at_utc)
        if due > business:
            return None
        normalized_due = _stamp(due)
        old = db.scalar(select(SubscriptionCharge).where(SubscriptionCharge.authorization_id == auth.id,
            SubscriptionCharge.due_at_utc == normalized_due))
        if old:
            return charge_view(db, old)
        account = owned_account(db, auth.owner_id, auth.account_id, active=False)
        actual = _now()
        row = SubscriptionCharge(id=uuid4().hex, owner_id=auth.owner_id, authorization_id=auth.id,
            due_at_utc=normalized_due, amount_minor=auth.amount_minor, status="posted",
            balance_minor=account.balance_minor, created_at_utc=_stamp(actual))
        setting = db.get(DemoSetting, 1)
        error = None
        if auth.status == "revoked":
            row.status = "blocked"
            error = "AUTHORIZATION_REVOKED"
        elif not setting or auth.dataset_version != setting.dataset_version:
            row.status = "failed"
            error = "DATASET_CHANGED"
        elif auth.timezone != "Asia/Shanghai" or auth.period != "monthly" or not 1 <= auth.charge_day <= 31:
            row.status = "failed"
            error = "INVALID_INPUT"
        else:
            try:
                # Savepoint prevents a partial ledger mutation if deterministic validation fails.
                with db.begin_nested():
                    from app.domain.security import check_grant, subscription_terms
                    import calendar
                    due_local = due.astimezone(SHANGHAI)
                    if (due_local.day != min(auth.charge_day, calendar.monthrange(due_local.year, due_local.month)[1])
                            or (due_local.hour, due_local.minute, due_local.second, due_local.microsecond) != (9, 0, 0, 0)):
                        raise DomainError('AUTHORIZATION_SCOPE_CHANGED', '扣费时点超出固定每月授权范围', 409)
                    check_grant(db, auth.owner_id, 'subscription:' + auth.id, subscription_terms(auth), consume=True)
                    tx = post_entry(db, owner_id=auth.owner_id, account_id=auth.account_id, kind="purchase",
                        direction="debit", amount_minor=auth.amount_minor, business_at=due,
                        merchant=auth.merchant, category="订阅服务", business_id=row.id, system_now=actual)
                    row.transaction_id = tx.id
                    row.balance_minor = account.balance_minor
            except DomainError as exc:
                row.status = "failed"
                error = exc.code
        row.error_code = error
        db.add(row)
        auth.next_charge_at_utc = _stamp(_next_charge(auth, due))
        auth.version += 1
        db.add(AuditEvent(id=uuid4().hex, owner_id=auth.owner_id,
            event="subscription_charge_" + row.status, created_at_utc=_stamp(actual)))
        db.flush()
        return charge_view(db, row)


def run_due_subscriptions(session_factory, *, now=None, limit=20):
    """Process bounded durable work. Polling and restarts never authorize new mandates."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise DomainError("INVALID_INPUT", "每轮处理数量须为1至100")
    with session_factory() as db:
        business = _now(now) if now else subscription_now(db)
        reminder_ids = list(db.scalars(select(SubscriptionReminder.id).where(
            SubscriptionReminder.status == "scheduled", SubscriptionReminder.fire_at_utc <= _stamp(business))
            .order_by(SubscriptionReminder.fire_at_utc, SubscriptionReminder.id).limit(limit)))
        authorization_ids = list(db.scalars(select(SubscriptionAuthorization.id).where(
            SubscriptionAuthorization.next_charge_at_utc <= _stamp(business))
            .order_by(SubscriptionAuthorization.next_charge_at_utc, SubscriptionAuthorization.id).limit(limit)))
    results = []
    processed = 0
    for identifier in reminder_ids:
        if processed >= limit:
            break
        with session_factory() as db:
            result = _notify_due(db, identifier, business)
        processed += 1
        if result:
            results.append(result)
    for identifier in authorization_ids:
        if processed >= limit:
            break
        with session_factory() as db:
            result = _charge_due(db, identifier, business)
        processed += 1
        if result:
            results.append(result)
    return results


class SubscriptionWorker:
    def __init__(self, session_factory, *, interval_seconds=1, clock=None):
        self.session_factory = session_factory
        self.interval_seconds = max(0.05, interval_seconds)
        self.clock = clock or guarded_subscription_clock(session_factory)
        self._stop = Event()
        self._thread = Thread(target=self._loop, name="finagent-subscriptions", daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)

    def _loop(self):
        while not self._stop.is_set():
            try:
                run_due_subscriptions(self.session_factory, now=self.clock())
            except Exception:
                LOGGER.exception("Subscription poll failed; durable work remains recoverable")
            self._stop.wait(self.interval_seconds)


def start_subscription_worker(session_factory, *, interval_seconds=1, clock=None):
    return SubscriptionWorker(session_factory, interval_seconds=interval_seconds, clock=clock).start()


def seed_test_authorizations(db, *, owner_id, account_id):
    """Explicit TEST-ONLY fixture helper. Never called by production migration or reset."""
    if not _is_test_path(_bound_path(db)):
        raise DomainError("TEST_FIXTURE_GUARD", "测试授权只能写入TEST-ONLY隔离数据库", 403)
    setting = db.get(DemoSetting, 1)
    owned_account(db, owner_id, account_id)
    if not setting:
        raise DomainError("INVALID_INPUT", "测试数据未初始化")
    for merchant, amount, day in (("云音乐", 3000, 15), ("云存储", 1000, 20)):
        old = db.scalar(select(SubscriptionAuthorization).where(SubscriptionAuthorization.owner_id == owner_id,
            SubscriptionAuthorization.dataset_version == setting.dataset_version,
            SubscriptionAuthorization.account_id == account_id, SubscriptionAuthorization.merchant == merchant))
        if not old:
            db.add(SubscriptionAuthorization(id=uuid4().hex, owner_id=owner_id,
                dataset_version=setting.dataset_version, account_id=account_id, merchant=merchant,
                amount_minor=amount, period="monthly", charge_day=day,
                next_charge_at_utc=_stamp(datetime(2026, 9, day, 9, tzinfo=SHANGHAI).astimezone(utc_timezone.utc)),
                timezone="Asia/Shanghai", status="active", version=1, created_at_utc=_stamp(_now())))
    db.flush()
