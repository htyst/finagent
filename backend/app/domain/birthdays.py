"""Authorized birthday procurement; amounts and supplier results are deterministic.

Local simulated suppliers are independent from language-model interpretation.
Business-clock overrides are restricted to an explicitly bound TEST-ONLY database.
Session expiry, confirmation expiry and recorded wall time always use actual UTC.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4
from sqlalchemy import select

from app.data.atomic import atomic_write
from app.data.models import Account, AgentStep, AgentTask, AuditEvent, DemoSetting, Task, Transaction
from app.data.birthday_models import (BirthdayPlan, BirthdayRequest, BirthdayReservation,
    BirthdayOrder, BirthdayFundEvent, BirthdayNotification)
from app.domain.clock import SHANGHAI, parse_day
from app.domain.errors import DomainError
from app.domain.ledger import owned_account, available_balance, reserved_balance, post_entry, assert_debit_allowed
from app.domain.money import validate_minor
from app.domain.operations import _canonical, _digest, _now, _session, _stamp

LOGGER = logging.getLogger(__name__)
ITEMS = {"flowers": {"supplier": "模拟花店", "amount_minor": 30000},
         "cake": {"supplier": "模拟蛋糕店", "amount_minor": 20000}}
LABELS = {"create": "生日安排", "change": "修改生日安排", "cancel": "取消生日安排",
          "cancel_item": "取消蛋糕部分", "retry": "重试蛋糕订单"}
TERMINAL = {"completed", "completed_with_changes", "cancelled"}
RULES = ("生日资料由用户主动提供；仅支持当前月的一次安排。北京时间生日前两天09:00自动下单，"
    "支持用户明确选定的新店铺及固定商品报价，无运费；默认模拟花店鲜花300元、模拟蛋糕店蛋糕200元。报价、供应商、地址、日期和预算在专用页面固定授权。"
    "预留只减少一般可用资金，不扣账面；付款从专款扣除且只入账一次。"
    "后台每秒检查，离开聊天页或重新登录不撤销已授权安排；停机后恢复会补办。"
    "修改或取消草稿在确认前暂停该计划的下单，草稿取消或过期后恢复原授权。"
    "到生日日期不会自动认定送达，须模拟供应商持久送达回执。全部有效商品送达后释放专款余款。"
    "累计释放金额包括此前调低预算已释放的金额；预算未使用额与当前专款、累计释放分别展示。"
    "失败商品未付款，不自动重试；可页面授权重试蛋糕或取消蛋糕部分，已付鲜花不会重复购买。"
    "已付订单不可通过取消整个计划撤销，本地不模拟退款。")


def _test_paths(db):
    value = db.get_bind().url.database
    expected = os.getenv("FINAGENT_BIRTHDAYS_TEST_DB", "")
    if not value or value == ":memory:" or not expected:
        raise DomainError("TEST_CLOCK_GUARD", "生日测试须显式绑定独立数据库", 500)
    actual = Path(value).resolve()
    if actual != Path(expected).resolve() or not any(p.upper() == "TEST-ONLY" for p in actual.parts):
        raise DomainError("TEST_CLOCK_GUARD", "生日测试仅允许精确绑定TEST-ONLY数据库", 500)
    return actual


def _test_json(db, variable):
    actual = _test_paths(db)
    filename = os.getenv(variable, "")
    if not filename or Path(filename).resolve().parent != actual.parent:
        raise DomainError("TEST_CLOCK_GUARD", "生日测试文件须与隔离数据库同目录", 500)
    path = Path(filename).resolve()
    try:
        if path.stat().st_size > 4096:
            raise ValueError("oversized test file")
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("object required")
        return value
    except (OSError, ValueError, TypeError) as error:
        raise DomainError("TEST_CLOCK_GUARD", "生日测试文件无效", 500) from error


def birthday_now(db=None):
    if os.getenv("FINAGENT_BIRTHDAYS_TEST_CLOCK_ENABLED") != "1":
        return _now()
    if db is None:
        raise DomainError("TEST_CLOCK_GUARD", "生日测试时钟缺少数据库绑定", 500)
    value = _test_json(db, "FINAGENT_BIRTHDAYS_TEST_CLOCK_FILE")
    try:
        if set(value) != {"now_utc"} or not isinstance(value["now_utc"], str):
            raise ValueError("invalid clock contract")
        return _now(datetime.fromisoformat(value["now_utc"]))
    except (ValueError, TypeError) as error:
        raise DomainError("TEST_CLOCK_GUARD", "生日测试时钟无效", 500) from error


def guarded_birthday_clock(session_factory):
    def current():
        with session_factory() as db:
            return birthday_now(db)
    current()
    return current


def _suppliers(db, contracts):
    defaults = {key: {"price_minor": item["amount_minor"], "reject_order": False} for key, item in ITEMS.items()}
    # All vendors remain local simulations. A new vendor accepts its explicit,
    # confirmed fixed quote; the guarded test adapter can independently change it.
    for item in contracts:
        if item["supplier"] != ITEMS[item["item_key"]]["supplier"]:
            defaults[item["item_key"]]["price_minor"] = item["amount_minor"]
    if not os.getenv("FINAGENT_BIRTHDAYS_TEST_SUPPLIER_FILE"):
        return defaults
    if os.getenv("FINAGENT_BIRTHDAYS_TEST_CLOCK_ENABLED") != "1":
        raise DomainError("TEST_CLOCK_GUARD", "供应商覆盖仅允许隔离生日测试", 500)
    value = _test_json(db, "FINAGENT_BIRTHDAYS_TEST_SUPPLIER_FILE")
    if set(value) != set(ITEMS) or any(not isinstance(r, dict) or set(r) != {"price_minor", "reject_order"}
        or type(r["price_minor"]) is not int or r["price_minor"] <= 0 or type(r["reject_order"]) is not bool
        for r in value.values()):
        raise DomainError("TEST_CLOCK_GUARD", "供应商测试合同无效", 500)
    return value


def _reservation(db, plan):
    row = db.scalar(select(BirthdayReservation).where(BirthdayReservation.plan_id == plan.id))
    if not row or row.owner_id != plan.owner_id or row.account_id != plan.source_account_id or row.dataset_version != plan.dataset_version:
        raise DomainError("RESERVATION_CHANGED", "生日专款与计划不一致", 409)
    return row


def _owned_plan(db, session, plan_id):
    row = db.scalar(select(BirthdayPlan).where(BirthdayPlan.id == plan_id,
        BirthdayPlan.owner_id == session.owner_id, BirthdayPlan.dataset_version == session.dataset_version))
    if row is None:
        raise DomainError("RESOURCE_NOT_FOUND", "生日计划不存在", 404)
    return row


def _owned_request(db, session, request_id):
    row = db.scalar(select(BirthdayRequest).where(BirthdayRequest.id == request_id,
        BirthdayRequest.owner_id == session.owner_id, BirthdayRequest.dataset_version == session.dataset_version))
    if row is None:
        raise DomainError("RESOURCE_NOT_FOUND", "生日办理单不存在", 404)
    if row.session_id != session.id:
        raise DomainError("INVALID_ORDER_SESSION", "请在准备生日办理单的原会话确认", 409)
    return row


def _config(plan):
    keys = json.loads(plan.item_keys_json)
    authorized = json.loads(plan.authorized_terms_json)
    stored = authorized.get("items", [dict(item_key=key, **ITEMS[key]) for key in keys])
    if not isinstance(stored, list):
        raise ValueError("invalid supplier contracts")
    contracts = {}
    for item in stored:
        if (not isinstance(item, dict) or set(item) != {"item_key", "supplier", "amount_minor"}
            or item["item_key"] not in ITEMS or item["item_key"] in contracts
            or not isinstance(item["supplier"], str) or not 1 <= len(item["supplier"].strip()) <= 80
            or any(ord(char) < 32 for char in item["supplier"])
            or type(item["amount_minor"]) is not int or item["amount_minor"] <= 0):
            raise ValueError("invalid supplier contract")
        contracts[item["item_key"]] = item
    if any(key not in contracts for key in keys):
        raise ValueError("missing supplier contract")
    return dict(plan_id=plan.id, birthday_date=plan.birthday_date, birthday_source=plan.birthday_source,
        source_account_id=plan.source_account_id, order_at_utc=plan.order_at_utc,
        delivery_date=plan.delivery_date, recipient=plan.recipient, address=plan.address,
        budget_minor=plan.budget_minor, item_keys=keys,
        items=[contracts[key] for key in keys], timezone="Asia/Shanghai", release_remainder=True)


def validate_authorized_plan(db, plan):
    try:
        if not hmac.compare_digest(plan.terms_digest, _digest(plan.authorized_terms_json)) or json.loads(plan.authorized_terms_json) != _config(plan):
            raise ValueError("authorization terms changed")
        request = db.get(BirthdayRequest, plan.authorization_request_id)
        if not request or request.status != "executed" or request.plan_id != plan.id or request.owner_id != plan.owner_id:
            raise ValueError("missing authority")
        fixed = _fixed(request)
        if any(fixed.get(key) != value for key, value in _config(plan).items() if key != "plan_id"):
            raise ValueError("plan no longer matches its confirmed request")
        setting = db.get(DemoSetting, 1)
        if not setting or plan.dataset_version != setting.dataset_version:
            raise DomainError("DATASET_CHANGED", "生日计划的数据版本已变化", 409)
        owned_account(db, plan.owner_id, plan.source_account_id)
        _reservation(db, plan)
    except (ValueError, KeyError, TypeError) as error:
        raise DomainError("REQUEST_CHANGED", "生日固定授权条款已变化，不能执行", 409) from error


def _pending_for_plan(db, plan_id, now=None):
    return db.scalar(select(BirthdayRequest).where(BirthdayRequest.plan_id == plan_id,
        BirthdayRequest.status == "pending_confirmation", BirthdayRequest.expires_at_utc > _stamp(_now(now))))


def order_view(row):
    return {**{key: getattr(row, key) for key in ("id", "plan_id", "item_key", "supplier", "amount_minor",
        "recipient", "address", "delivery_date", "status", "payment_status", "transaction_id", "attempt_count",
        "error_code", "created_at_utc", "updated_at_utc")},
        "delivery_receipt": json.loads(row.delivery_receipt_json) if row.delivery_receipt_json else None}


def request_view(row, now=None):
    status = "expired" if row.status == "pending_confirmation" and datetime.fromisoformat(row.expires_at_utc) <= _now(now) else row.status
    return dict(id=row.id, kind=row.kind, label=LABELS[row.kind], status=status, plan_id=row.plan_id,
        summary=json.loads(row.terms_json), created_at_utc=row.created_at_utc,
        expires_at_utc=row.expires_at_utc, completed_at_utc=row.completed_at_utc,
        receipt=json.loads(row.receipt_json) if row.receipt_json else None)


def plan_view(db, row):
    hold = _reservation(db, row)
    pending = _pending_for_plan(db, row.id)
    events = db.scalars(select(BirthdayFundEvent).where(BirthdayFundEvent.plan_id == row.id)
        .order_by(BirthdayFundEvent.created_at_utc, BirthdayFundEvent.id)).all()
    return {**_config(row), "id": row.id, "status": row.status, "version": row.version,
        "spent_minor": row.spent_minor, "released_minor": row.released_minor,
        "reserved_minor": hold.remaining_minor, "error_code": row.error_code,
        "paused": pending is not None, "pending_request_id": pending.id if pending else None,
        "created_at_utc": row.created_at_utc, "updated_at_utc": row.updated_at_utc,
        "fund_events": [{key: getattr(event, key) for key in ("id", "kind", "delta_minor", "amount_minor",
            "remaining_minor", "request_id", "order_id", "transaction_id", "created_at_utc")} for event in events]}


def get_portfolio(db, session_id):
    session, _ = _session(db, session_id, _now())
    plans = db.scalars(select(BirthdayPlan).where(BirthdayPlan.owner_id == session.owner_id,
        BirthdayPlan.dataset_version == session.dataset_version).order_by(BirthdayPlan.created_at_utc, BirthdayPlan.id)).all()
    plan_ids = [p.id for p in plans]
    accounts = db.scalars(select(Account).where(Account.owner_id == session.owner_id).order_by(Account.id)).all()
    return dict(rules=RULES, business_now_utc=_stamp(birthday_now(db)),
        accounts=[dict(account_id=a.id, alias=a.alias, balance_minor=a.balance_minor,
            reserved_minor=reserved_balance(db, a.id), available_minor=available_balance(db, a)) for a in accounts],
        plans=[plan_view(db, p) for p in plans],
        requests=[request_view(r) for r in db.scalars(select(BirthdayRequest).where(
            BirthdayRequest.owner_id == session.owner_id, BirthdayRequest.dataset_version == session.dataset_version)
            .order_by(BirthdayRequest.created_at_utc, BirthdayRequest.id))],
        orders=[order_view(r) for r in db.scalars(select(BirthdayOrder).where(BirthdayOrder.plan_id.in_(plan_ids))
            .order_by(BirthdayOrder.created_at_utc, BirthdayOrder.item_key))],
        notifications=[{key: getattr(r, key) for key in ("id", "plan_id", "title", "body", "created_at_utc")}
            for r in db.scalars(select(BirthdayNotification).where(BirthdayNotification.plan_id.in_(plan_ids))
                .order_by(BirthdayNotification.created_at_utc, BirthdayNotification.id))])


def ensure_no_pending(db, session_id, instant=None):
    if db.scalar(select(BirthdayRequest.id).where(BirthdayRequest.session_id == session_id,
        BirthdayRequest.status == "pending_confirmation", BirthdayRequest.expires_at_utc > _stamp(_now(instant)))):
        raise DomainError("ACTIVE_WRITE_TASK_EXISTS", "请先确认或取消当前生日办理单", 409)


def _fixed(row):
    if not hmac.compare_digest(row.terms_digest, _digest(row.terms_json)):
        raise DomainError("REQUEST_CHANGED", "生日固定办理条款已变化", 409)
    terms = json.loads(row.terms_json)
    if terms.get("kind") != row.kind or terms.get("plan_id") != (None if row.kind == "create" else row.plan_id):
        raise DomainError("REQUEST_CHANGED", "生日办理单与固定条款不一致", 409)
    return terms


def _draft(db, session, row, instant):
    if row.status != "pending_confirmation" or datetime.fromisoformat(row.expires_at_utc) <= instant:
        raise DomainError("INVALID_ORDER_STATE", "生日办理单已失效或过期", 409)
    if row.agent_task_id:
        task = db.get(AgentTask, row.agent_task_id)
        if not task or task.stop_requested or task.session_id != session.id or task.owner_id != session.owner_id:
            raise DomainError("INVALID_TASK_STATE", "生日对话已停止", 409)
    return _fixed(row)


def _item_keys(value):
    if not isinstance(value, list) or not value or any(not isinstance(key, str) or key not in ITEMS for key in value) or len(value) != len(set(value)) or "flowers" not in value:
        raise DomainError("INVALID_INPUT", "生日商品只支持鲜花，或鲜花和蛋糕", 422)
    return [key for key in ITEMS if key in value]


def _supplier_contracts(values, keys, previous=()):
    names, amounts = values.get("supplier_names", {}), values.get("quoted_amounts", {})
    if not isinstance(names, dict) or (names and set(names) != set(keys)):
        raise DomainError("INVALID_INPUT", "请逐项明确所选店铺名称", 422)
    if not isinstance(amounts, dict) or (amounts and set(amounts) != set(keys)):
        raise DomainError("INVALID_INPUT", "请逐项明确固定商品报价", 422)
    prior = {item["item_key"]: item for item in previous}
    contracts = []
    for key in keys:
        old = prior.get(key, ITEMS[key])
        name = names.get(key, old["supplier"])
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or any(ord(char) < 32 for char in name):
            raise DomainError("INVALID_INPUT", "店铺名称无效，请核对所选供应商", 422)
        name = name.strip()
        if name == ("花店" if key == "flowers" else "蛋糕店"):
            name = ITEMS[key]["supplier"]
        known = name == ITEMS[key]["supplier"]
        if not known and key not in amounts and name != prior.get(key, {}).get("supplier"):
            raise DomainError("MISSING_SUPPLIER_QUOTE", "新店铺无需预先收录，请补充其固定商品报价", 422)
        amount = validate_minor(amounts.get(key, old["amount_minor"]))
        if known and amount != ITEMS[key]["amount_minor"]:
            raise DomainError("PRICE_CHANGED", "默认模拟店铺的报价已变化，请核对供应商固定报价", 409)
        contracts.append(dict(item_key=key, supplier=name, amount_minor=amount))
    return contracts


def _prepare_terms(db, session, kind, values, plan, business):
    if not isinstance(values, dict):
        raise DomainError("INVALID_INPUT", "生日条款须为对象", 422)
    common = {"budget_minor", "item_keys", "supplier_names", "quoted_amounts"}
    allowed = common | {"birthday_date", "source_account_id", "recipient", "address", "order_hour", "order_minute",
        "timezone", "release_remainder", "birthday_source"} if kind == "create" else common if kind == "change" else {"item_key"} if kind in {"cancel_item", "retry"} else set()
    if set(values) - allowed:
        raise DomainError("INVALID_INPUT", "生日条款包含不支持的要求", 422)
    if kind == "create":
        required = {"birthday_date", "source_account_id", "budget_minor", "recipient", "address", "item_keys"}
        if not required <= set(values):
            raise DomainError("INVALID_INPUT", "请补齐生日、付款账户、预算、收件人、地址和商品", 422)
        day = parse_day(values["birthday_date"])
        if (day.year, day.month) != (business.astimezone(SHANGHAI).year, business.astimezone(SHANGHAI).month):
            raise DomainError("BIRTHDAY_OUTSIDE_CURRENT_MONTH", "仅支持当前月生日安排，请核对用户提供的日期", 409)
        if values.get("birthday_source", "user_provided") != "user_provided" or values.get("release_remainder", True) is not True or values.get("timezone", "Asia/Shanghai") != "Asia/Shanghai":
            raise DomainError("INVALID_INPUT", "须用户提供生日、使用北京时间并同意任务完成释放余款", 422)
        hour, minute = values.get("order_hour", 9), values.get("order_minute", 0)
        if type(hour) is not int or type(minute) is not int or hour != 9 or minute != 0:
            raise DomainError("INVALID_INPUT", "当前目录下单时间固定为生日前两天北京时间09:00", 422)
        for key, maximum in (("recipient", 80), ("address", 200)):
            if not isinstance(values[key], str) or not 1 <= len(values[key].strip()) <= maximum:
                raise DomainError("INVALID_INPUT", "收件人或地址无效", 422)
        order_at = datetime.combine(day - timedelta(days=2), time(hour, minute), SHANGHAI).astimezone(timezone.utc)
        if order_at <= business:
            raise DomainError("ORDER_TIME_PASSED", "约定下单时间已过，不能新建该自动安排", 409)
        account = owned_account(db, session.owner_id, values["source_account_id"])
        keys = _item_keys(values["item_keys"])
        budget = validate_minor(values["budget_minor"])
        data = dict(plan_id=None, birthday_date=day.isoformat(), birthday_source="user_provided",
            source_account_id=account.id, order_at_utc=_stamp(order_at), delivery_date=day.isoformat(),
            recipient=values["recipient"].strip(), address=values["address"].strip(), budget_minor=budget,
            item_keys=keys, timezone="Asia/Shanghai", release_remainder=True)
        previous_budget, previous_hold, new_hold = 0, 0, budget
    else:
        validate_authorized_plan(db, plan)
        if plan.status in TERMINAL:
            raise DomainError("INVALID_PLAN_STATE", "生日任务已结束，不能变更", 409)
        data = _config(plan)
        account = owned_account(db, session.owner_id, plan.source_account_id)
        hold = _reservation(db, plan)
        previous_budget, previous_hold, new_hold = plan.budget_minor, hold.remaining_minor, hold.remaining_minor
        orders = db.scalars(select(BirthdayOrder).where(BirthdayOrder.plan_id == plan.id)).all()
        if kind == "change":
            if orders or datetime.fromisoformat(plan.order_at_utc) <= business:
                raise DomainError("ORDER_ALREADY_STARTED", "仅支持下单前修改预算和商品", 409)
            if not {"budget_minor", "item_keys"} <= set(values):
                raise DomainError("INVALID_INPUT", "请同时明确新预算和全部商品", 422)
            data.update(budget_minor=validate_minor(values["budget_minor"]), item_keys=_item_keys(values["item_keys"]))
            new_hold = data["budget_minor"]
        elif kind == "cancel":
            if any(o.payment_status == "paid" for o in orders):
                raise DomainError("IRREVERSIBLE_ORDER", "已有已付受理订单，不能笼统撤销整个安排；订单和专款保留，请核对真实进度", 409)
            new_hold = 0
        else:
            if values.get("item_key") != "cake":
                raise DomainError("INVALID_INPUT", "当前只支持处理失败且未付款的蛋糕部分", 422)
            order = next((o for o in orders if o.item_key == "cake"), None)
            if not order or order.status != "rejected" or order.payment_status != "unpaid" or "cake" not in data["item_keys"]:
                raise DomainError("INVALID_ORDER_STATE", "仅能处理已拒单且未付款的蛋糕，鲜花订单保留", 409)
            if kind == "retry" and business.astimezone(SHANGHAI).date() > parse_day(plan.delivery_date):
                raise DomainError("ORDER_TIME_PASSED", "生日送达日期已过，不能按原承诺重试", 409)
            if kind == "cancel_item":
                data["item_keys"] = [key for key in data["item_keys"] if key != "cake"]
            data["item_key"] = "cake"
        data["expected_plan_version"] = plan.version
    contracts = _supplier_contracts(values, data["item_keys"], data.get("items", ()))
    if data["budget_minor"] < sum(item["amount_minor"] for item in contracts):
        raise DomainError("BUDGET_EXCEEDED", "预算不足以支付全部固定报价商品", 409)
    increase = new_hold - previous_hold
    if kind in {"create", "retry"} or increase > 0:
        assert_debit_allowed(db, account)
    if increase > available_balance(db, account):
        raise DomainError("INSUFFICIENT_FUNDS", "一般可用余额不足以新增生日专款", 409)
    return {**data, "kind": kind, "account_id": account.id, "account_alias": account.alias,
        "items": contracts,
        "previous_budget_minor": previous_budget, "previous_reserved_minor": previous_hold,
        "new_reserved_minor": new_hold, "reservation_delta_minor": increase,
        "release_amount_minor": max(0, -increase)}


def _other_pending(db, session_id, instant):
    from app.data.card_models import CardRequest
    from app.data.wealth_models import WealthOrder
    from app.data.scheduled_models import ScheduledTransfer
    from app.data.collection_models import CollectionRequest
    from app.data.subscription_models import SubscriptionRequest
    if db.scalar(select(Task.id).where(Task.active_session_id == session_id)) or any(db.scalar(select(model.id).where(
        model.session_id == session_id, model.status == "pending_confirmation", model.expires_at_utc > _stamp(instant)))
        for model in (CardRequest, WealthOrder, ScheduledTransfer, CollectionRequest, SubscriptionRequest)):
        raise DomainError("ACTIVE_WRITE_TASK_EXISTS", "请先确认或取消其他待确认业务", 409)


from app.domain.security import preparing

@preparing
def prepare(db, *, session_id, kind, terms, plan_id=None, agent_task_id=None, now=None, business_now=None):
    if kind not in LABELS or (kind == "create") != (plan_id is None):
        raise DomainError("INVALID_INPUT", "生日办理类型或计划引用无效", 422)
    instant = _now(now)
    with atomic_write(db):
        session, _ = _session(db, session_id, instant)
        if agent_task_id:
            task = db.get(AgentTask, agent_task_id)
            if not task or task.owner_id != session.owner_id or task.session_id != session.id or task.dataset_version != session.dataset_version or task.stop_requested:
                raise DomainError("INVALID_TASK_STATE", "生日对话已停止", 409)
        plan = _owned_plan(db, session, plan_id) if plan_id else None
        business = _now(business_now) if business_now else birthday_now(db)
        fixed = _prepare_terms(db, session, kind, terms, plan, business)
        canonical = _canonical(fixed)
        digest = _digest(canonical)
        for old in db.scalars(select(BirthdayRequest).where(BirthdayRequest.status == "pending_confirmation",
            (BirthdayRequest.session_id == session.id) | (BirthdayRequest.plan_id == plan_id if plan_id else BirthdayRequest.id == ""))):
            if datetime.fromisoformat(old.expires_at_utc) <= instant:
                old.status, old.token_hash = "expired", None
                sync(db, old)
            elif old.session_id == session.id and old.agent_task_id == agent_task_id and old.terms_digest == digest:
                return request_view(old, instant)
            elif old.session_id == session.id and agent_task_id and old.agent_task_id == agent_task_id:
                old.status, old.token_hash = "invalidated", None
                sync(db, old)
            else:
                raise DomainError("ACTIVE_WRITE_TASK_EXISTS", "请先确认或取消已有生日办理单", 409)
        _other_pending(db, session.id, instant)
        row = BirthdayRequest(id=uuid4().hex, owner_id=session.owner_id, session_id=session.id,
            dataset_version=session.dataset_version, agent_task_id=agent_task_id, plan_id=plan_id,
            kind=kind, status="pending_confirmation", terms_json=canonical, terms_digest=digest,
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
        return dict(confirmation_token=token, expires_at_utc=row.expires_at_utc)


def _fund(db, plan, kind, amount, event_key, *, request_id=None, order_id=None, transaction_id=None):
    hold = _reservation(db, plan)
    db.add(BirthdayFundEvent(id=uuid4().hex, plan_id=plan.id, owner_id=plan.owner_id,
        event_key=event_key, kind=kind, amount_minor=amount, delta_minor=amount if kind == "reserve" else -amount,
        remaining_minor=hold.remaining_minor, request_id=request_id, order_id=order_id,
        transaction_id=transaction_id, created_at_utc=_stamp(_now())))


def _adjust_reservation(db, plan, new_amount, key, request_id=None):
    hold = _reservation(db, plan)
    difference = new_amount - hold.remaining_minor
    if difference == 0:
        return
    account = owned_account(db, plan.owner_id, plan.source_account_id)
    if difference > 0:
        assert_debit_allowed(db, account)
    if difference > available_balance(db, account):
        raise DomainError("INSUFFICIENT_FUNDS", "一般可用余额不足以增加生日专款", 409)
    hold.remaining_minor = new_amount
    hold.status = "active" if new_amount else "released"
    if difference < 0:
        plan.released_minor += -difference
    account.version += 1
    _fund(db, plan, "reserve" if difference > 0 else "release", abs(difference), key, request_id=request_id)


def _authorize(plan, request_id):
    plan.authorized_terms_json = _canonical(_config(plan))
    plan.terms_digest = _digest(plan.authorized_terms_json)
    plan.authorization_request_id = request_id
    plan.updated_at_utc = _stamp(_now())


def sync(db, row):
    task = db.get(AgentTask, row.agent_task_id) if row.agent_task_id else None
    if not task or task.owner_id != row.owner_id:
        return
    context = json.loads(task.context_json)
    if row.status == "executed" and row.plan_id:
        context["birthday_plan_id"] = row.plan_id
        task.context_json = _canonical(context)
    if context.get("birthday_request_id") == row.id:
        if (task.intent or '').startswith('birthday_'):
            if row.status == "executed":
                plan = db.get(BirthdayPlan, row.plan_id)
                status = "cancelled" if plan and plan.status == "cancelled" else "completed" if plan and plan.status in TERMINAL else "gathering_info"
            else:
                status = {"pending_confirmation": "awaiting_confirmation", "invalidated": "gathering_info"}.get(row.status, row.status)
            if task.status != status:
                task.status = status
                task.updated_at_utc = _stamp(_now())
        for step in db.scalars(select(AgentStep).where(AgentStep.task_id == task.id, AgentStep.result_ref == row.id)):
            step.status = "succeeded" if row.status == "executed" else "awaiting_confirmation" if row.status == "pending_confirmation" else row.status
            step.result_json = _canonical({"birthday_request": request_view(row)})


from app.domain.security import secured

@secured('birthday', 'request_id')
def confirm(db, *, session_id, request_id, confirmation_token, now=None, business_now=None):
    instant = _now(now)
    with atomic_write(db):
        session, _ = _session(db, session_id, instant)
        row = _owned_request(db, session, request_id)
        if row.status == "executed":
            return request_view(row, instant)
        terms = _draft(db, session, row, instant)
        if not isinstance(confirmation_token, str) or not row.token_hash or not hmac.compare_digest(row.token_hash, _digest(confirmation_token)):
            raise DomainError("CONFIRMATION_INVALID", "生日确认凭据无效", 403)
        business = _now(business_now) if business_now else birthday_now(db)
        plan = _owned_plan(db, session, row.plan_id) if row.plan_id else None
        if plan:
            validate_authorized_plan(db, plan)
            if plan.version != terms["expected_plan_version"] or plan.status in TERMINAL:
                raise DomainError("STALE_RESOURCE", "生日计划已变化，请重新准备", 409)
        # Revalidate fixed requirements under the same write lock as settlement.
        if row.kind == "create":
            if datetime.fromisoformat(terms["order_at_utc"]) <= business:
                raise DomainError("ORDER_TIME_PASSED", "预约下单时间已过，请重新规划", 409)
            account = owned_account(db, session.owner_id, terms["source_account_id"])
            assert_debit_allowed(db, account)
            if available_balance(db, account) < terms["budget_minor"]:
                raise DomainError("INSUFFICIENT_FUNDS", "一般可用余额不足以预留生日预算", 409)
            plan = BirthdayPlan(id=uuid4().hex, owner_id=session.owner_id, dataset_version=session.dataset_version,
                agent_task_id=row.agent_task_id, source_account_id=account.id,
                birthday_date=terms["birthday_date"], birthday_source=terms["birthday_source"],
                order_at_utc=terms["order_at_utc"], delivery_date=terms["delivery_date"], recipient=terms["recipient"],
                address=terms["address"], budget_minor=terms["budget_minor"], item_keys_json=_canonical(terms["item_keys"]),
                spent_minor=0, released_minor=0, status="scheduled", version=1, items_cancelled=0,
                authorized_terms_json="{}", terms_digest="", authorization_request_id=row.id,
                created_at_utc=_stamp(instant), updated_at_utc=_stamp(instant))
            db.add(plan)
            db.flush()
            db.add(BirthdayReservation(id=uuid4().hex, plan_id=plan.id, owner_id=session.owner_id,
                account_id=account.id, dataset_version=session.dataset_version, remaining_minor=0, status="active"))
            db.flush()
            row.plan_id = plan.id
            _adjust_reservation(db, plan, plan.budget_minor, row.id + ":reserve", row.id)
        elif row.kind == "change":
            if db.scalar(select(BirthdayOrder.id).where(BirthdayOrder.plan_id == plan.id)) or datetime.fromisoformat(plan.order_at_utc) <= business:
                raise DomainError("ORDER_ALREADY_STARTED", "下单时点已过或订单已产生，不能修改", 409)
            plan.budget_minor = terms["budget_minor"]
            plan.item_keys_json = _canonical(terms["item_keys"])
            _adjust_reservation(db, plan, terms["new_reserved_minor"], row.id + ":adjust", row.id)
            plan.version += 1
        elif row.kind == "cancel":
            orders = db.scalars(select(BirthdayOrder).where(BirthdayOrder.plan_id == plan.id)).all()
            if any(o.payment_status == "paid" for o in orders):
                raise DomainError("IRREVERSIBLE_ORDER", "已有已付订单，不能撤销整个安排", 409)
            for order in orders:
                order.status = "cancelled"
            _adjust_reservation(db, plan, 0, row.id + ":release", row.id)
            plan.status, plan.error_code = "cancelled", None
            plan.version += 1
        else:
            order = db.scalar(select(BirthdayOrder).where(BirthdayOrder.plan_id == plan.id, BirthdayOrder.item_key == "cake"))
            if not order or order.status != "rejected" or order.payment_status != "unpaid":
                raise DomainError("STALE_RESOURCE", "蛋糕订单状态已变化，请重新准备", 409)
            if row.kind == "cancel_item":
                order.status, order.error_code = "cancelled", None
                plan.item_keys_json, plan.items_cancelled = _canonical(terms["item_keys"]), 1
                plan.status, plan.error_code = "awaiting_delivery", None
            else:
                if business.astimezone(SHANGHAI).date() > parse_day(plan.delivery_date):
                    raise DomainError("ORDER_TIME_PASSED", "原送达日期已过，不能重试", 409)
                assert_debit_allowed(db, owned_account(db, plan.owner_id, plan.source_account_id))
                order.status, order.error_code = "pending", None
                plan.status, plan.error_code = "scheduled", None
            plan.version += 1
        row.status, row.token_hash, row.completed_at_utc = "executed", None, _stamp(instant)
        if row.kind in {"create", "change"}:
            plan.authorized_terms_json = _canonical({"items": terms["items"]})
        _authorize(plan, row.id)
        if row.kind == "cancel_item":
            _complete_if_delivered(db, plan)
        message = {"create": "生日计划已授权，已预留预算；后台到生日前两天下单，送达回执齐备后释放余款",
            "change": "生日计划已修改授权，预留差额已调整；原待执行配置不再使用",
            "cancel": "生日安排已取消，未执行订单停止，未使用专款已释放",
            "cancel_item": "未付款蛋糕部分已取消；已成功鲜花保留，送达后释放剩余专款",
            "retry": "仅蛋糕重试已授权；已付鲜花不会重复购买"}[row.kind]
        row.receipt_json = _canonical(dict(request_id=row.id, plan_id=plan.id, kind=row.kind, message=message,
            completed_at_utc=_stamp(instant), plan_status=plan.status, spent_minor=plan.spent_minor,
            released_minor=plan.released_minor, reserved_minor=_reservation(db, plan).remaining_minor))
        db.add(AuditEvent(id=uuid4().hex, owner_id=session.owner_id,
            event="birthday_" + row.kind + "_confirmed", created_at_utc=_stamp(instant)))
        db.flush()
        sync(db, row)
        return request_view(row, instant)


def cancel(db, *, session_id, request_id, now=None):
    instant = _now(now)
    with atomic_write(db):
        session, _ = _session(db, session_id, instant)
        row = _owned_request(db, session, request_id)
        if row.status == "pending_confirmation":
            row.status, row.token_hash = "cancelled", None
            sync(db, row)
        return request_view(row, instant)


def invalidate_task(db, task):
    rid = json.loads(task.context_json).get("birthday_request_id")
    row = db.get(BirthdayRequest, rid) if rid else None
    if row and row.owner_id == task.owner_id and row.agent_task_id == task.id and row.status == "pending_confirmation":
        row.status, row.token_hash = "invalidated", None
        sync(db, row)


def invalidate_session(db, session_id):
    for row in db.scalars(select(BirthdayRequest).where(BirthdayRequest.session_id == session_id,
        BirthdayRequest.status == "pending_confirmation")):
        row.status, row.token_hash = "invalidated", None
        sync(db, row)


def task_state(db, task):
    context = json.loads(task.context_json)
    request = db.get(BirthdayRequest, context.get("birthday_request_id")) if context.get("birthday_request_id") else None
    if request and (request.owner_id != task.owner_id or request.dataset_version != task.dataset_version):
        request = None
    pid = context.get("birthday_plan_id") or context.get("last_birthday_plan_id") or (request.plan_id if request else None)
    plan = db.get(BirthdayPlan, pid) if pid else None
    if plan and (plan.owner_id != task.owner_id or plan.dataset_version != task.dataset_version):
        plan = None
    return dict(birthday_request=request_view(request) if request else None,
                birthday_plan=plan_view(db, plan) if plan else None)


def _notify(db, plan, key, title, body):
    if not db.scalar(select(BirthdayNotification.id).where(BirthdayNotification.event_key == key)):
        db.add(BirthdayNotification(id=uuid4().hex, plan_id=plan.id, owner_id=plan.owner_id,
            event_key=key, title=title, body=body, created_at_utc=_stamp(_now())))


def _complete_if_delivered(db, plan):
    if plan.status in TERMINAL:
        return
    keys = json.loads(plan.item_keys_json)
    orders = {r.item_key: r for r in db.scalars(select(BirthdayOrder).where(BirthdayOrder.plan_id == plan.id))}
    if keys and all(key in orders and orders[key].payment_status == "paid" and orders[key].status == "delivered" and orders[key].delivery_receipt_json for key in keys):
        _adjust_reservation(db, plan, 0, plan.id + ":completion-release")
        plan.status = "completed_with_changes" if plan.items_cancelled else "completed"
        plan.version += 1
        plan.updated_at_utc = _stamp(_now())
        _notify(db, plan, plan.id + ":complete", "生日安排已完成",
            f"有效订单均已有模拟供应商送达回执，共支付{plan.spent_minor/100:.2f}元，累计释放{plan.released_minor/100:.2f}元，当前专款0元。")


def _execute_due(db, plan_id, business):
    with atomic_write(db):
        plan = db.get(BirthdayPlan, plan_id)
        if not plan or plan.status != "scheduled" or datetime.fromisoformat(plan.order_at_utc) > business or _pending_for_plan(db, plan.id):
            return None
        try:
            from app.domain.security import check_grant, birthday_terms
            check_grant(db, plan.owner_id, 'birthday:' + plan.id, birthday_terms(plan))
            validate_authorized_plan(db, plan)
            assert_debit_allowed(db, owned_account(db, plan.owner_id, plan.source_account_id))
        except DomainError as error:
            plan.status, plan.error_code, plan.updated_at_utc = "blocked", error.code, _stamp(_now())
            _notify(db, plan, plan.id + ":blocked", "生日安排被阻止", error.message + "；未新增订单或付款，专款保留。")
            return plan_view(db, plan)
        contracts = {item["item_key"]: item for item in _config(plan)["items"]}
        suppliers = _suppliers(db, list(contracts.values()))
        plan.status = "ordering"
        for key in json.loads(plan.item_keys_json):
            order = db.scalar(select(BirthdayOrder).where(BirthdayOrder.plan_id == plan.id, BirthdayOrder.item_key == key))
            if order and (order.payment_status == "paid" or order.status != "pending"):
                continue
            actual = _now()
            if not order:
                order = BirthdayOrder(id=uuid4().hex, plan_id=plan.id, owner_id=plan.owner_id, item_key=key,
                    supplier=contracts[key]["supplier"], amount_minor=contracts[key]["amount_minor"], recipient=plan.recipient,
                    address=plan.address, delivery_date=plan.delivery_date, status="pending", payment_status="unpaid",
                    attempt_count=0, created_at_utc=_stamp(actual), updated_at_utc=_stamp(actual))
                db.add(order)
                db.flush()
            order.attempt_count += 1
            error_code = "PRICE_CHANGED" if suppliers[key]["price_minor"] != order.amount_minor else "SUPPLIER_REJECTED" if suppliers[key]["reject_order"] else None
            if not error_code:
                try:
                    with db.begin_nested():
                        from app.domain.security import check_grant, birthday_terms
                        check_grant(db, plan.owner_id, 'birthday:' + plan.id, birthday_terms(plan), consume=True)
                        tx = post_entry(db, owner_id=plan.owner_id, account_id=plan.source_account_id,
                            kind="purchase", direction="debit", amount_minor=order.amount_minor,
                            business_at=business, merchant=order.supplier, category="生日采购", business_id=order.id,
                            system_now=actual, reservation_plan_id=plan.id)
                        order.transaction_id, order.payment_status, order.status = tx.id, "paid", "accepted"
                except DomainError as error:
                    error_code = error.code
            if error_code:
                order.status, order.error_code = "rejected", error_code
            order.updated_at_utc = _stamp(actual)
            _notify(db, plan, order.id + ":attempt:" + str(order.attempt_count),
                ("鲜花" if key == "flowers" else "蛋糕") + ("订单已受理" if order.payment_status == "paid" else "下单失败"),
                f"{order.supplier}：" + (f"已支付{order.amount_minor/100:.2f}元，已受理，尚无送达回执。" if order.payment_status == "paid" else f"{error_code}，未支付；已成功的其他商品保留。"))
        orders = db.scalars(select(BirthdayOrder).where(BirthdayOrder.plan_id == plan.id,
            BirthdayOrder.item_key.in_(json.loads(plan.item_keys_json)))).all()
        plan.status = "partial_failure" if any(o.status == "rejected" for o in orders) else "awaiting_delivery"
        plan.error_code = "PARTIAL_ORDER_FAILURE" if plan.status == "partial_failure" else None
        plan.version += 1
        plan.updated_at_utc = _stamp(_now())
        db.flush()
        return plan_view(db, plan)


def run_due_birthdays(session_factory, *, now=None, limit=20):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise DomainError("INVALID_INPUT", "每轮生日任务数量须为1至100", 422)
    with session_factory() as db:
        business = _now(now) if now else birthday_now(db)
        identifiers = list(db.scalars(select(BirthdayPlan.id).where(BirthdayPlan.status == "scheduled",
            BirthdayPlan.order_at_utc <= _stamp(business)).order_by(BirthdayPlan.order_at_utc, BirthdayPlan.id).limit(limit)))
    results = []
    for identifier in identifiers:
        with session_factory() as db:
            result = _execute_due(db, identifier, business)
        if result:
            results.append(result)
    return results


def simulate_delivery(db, *, session_id, order_id, now=None, business_now=None):
    instant = _now(now)
    with atomic_write(db):
        session, _ = _session(db, session_id, instant)
        order = db.scalar(select(BirthdayOrder).where(BirthdayOrder.id == order_id, BirthdayOrder.owner_id == session.owner_id))
        if not order:
            raise DomainError("RESOURCE_NOT_FOUND", "生日订单不存在", 404)
        plan = _owned_plan(db, session, order.plan_id)
        validate_authorized_plan(db, plan)
        if order.status == "delivered" and order.delivery_receipt_json:
            return order_view(order)
        business = _now(business_now) if business_now else birthday_now(db)
        if business.astimezone(SHANGHAI).date() < parse_day(order.delivery_date):
            raise DomainError("DELIVERY_TIME_NOT_REACHED", "未到约定生日送达日期，不能模拟已送达", 409)
        tx = db.get(Transaction, order.transaction_id) if order.transaction_id else None
        if order.payment_status != "paid" or order.status != "accepted" or not tx or tx.status != "posted" or tx.business_id != order.id or tx.amount_minor != order.amount_minor:
            raise DomainError("INVALID_ORDER_STATE", "仅已付款且受理的订单可记录送达", 409)
        receipt = dict(id=uuid4().hex, order_id=order.id, supplier=order.supplier, simulated=True,
            status="delivered", delivery_date=order.delivery_date, delivered_at_utc=_stamp(business), recorded_at_utc=_stamp(instant))
        order.status, order.delivery_receipt_json, order.updated_at_utc = "delivered", _canonical(receipt), _stamp(instant)
        plan.version += 1
        plan.updated_at_utc = _stamp(instant)
        _notify(db, plan, order.id + ":delivered", ("鲜花" if order.item_key == "flowers" else "蛋糕") + "已模拟送达", "独立模拟供应商已送达回执已记录，回执编号" + receipt["id"] + "。")
        _complete_if_delivered(db, plan)
        db.flush()
        return order_view(order)


class BirthdayWorker:
    def __init__(self, session_factory, *, interval_seconds=1, clock=None):
        self.session_factory, self.interval_seconds = session_factory, max(.05, interval_seconds)
        self.clock = clock or guarded_birthday_clock(session_factory)
        self._stop = Event()
        self._thread = Thread(target=self._loop, name="finagent-birthdays", daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)

    def _loop(self):
        while not self._stop.is_set():
            try:
                run_due_birthdays(self.session_factory, now=self.clock())
            except Exception:
                LOGGER.exception("Birthday poll failed; persisted authorized work remains recoverable")
            self._stop.wait(self.interval_seconds)


def start_birthday_worker(session_factory, *, interval_seconds=1, clock=None):
    return BirthdayWorker(session_factory, interval_seconds=interval_seconds, clock=clock).start()
