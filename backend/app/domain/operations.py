from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from app.data.atomic import atomic_write

from app.data.models import (Account, AgentTask, AgentStep, AuditEvent, BusinessResult, Card, CardActionOrder,
                             DemoSession, DemoSetting, Proposal, Recipient, RecipientCredit, Task, TransferOrder)
from app.domain.clock import Clock, parse_day, SHANGHAI
from app.domain.conditions import evaluate_condition, normalize_condition
from app.domain.errors import DomainError
from app.domain.recipient_directory import mask_phone
from app.domain.ledger import daily_transfer_outflow, owned_account, available_balance, post_external_transfer, post_internal_transfer
from app.domain.money import require_cny, validate_transfer_amount
from app.domain.security import secured, preparing

PENDING = "pending_confirmation"
TERMINAL = {"executed", "cancelled", "expired", "invalidated", "failed"}
TOKEN_SECONDS = 300
logger = logging.getLogger(__name__)


def _now(value: datetime | None = None) -> datetime:
    instant = value or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("system clock must be timezone-aware")
    return instant.astimezone(timezone.utc)


def _stamp(instant: datetime) -> str:
    return instant.isoformat(timespec="microseconds")


def _canonical(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write(db: Session, work: Callable[[], object]):
    """Serialize SQLite writers before reading snapshots; caller owns no pending changes."""
    with atomic_write(db):
        return work()


def _session(db: Session, session_id: str, instant: datetime) -> tuple[DemoSession, DemoSetting]:
    setting = db.get(DemoSetting, 1)
    session = db.get(DemoSession, session_id)
    if (setting is None or session is None or session.dataset_version != setting.dataset_version
            or datetime.fromisoformat(session.expires_at_utc) <= instant):
        raise DomainError("SESSION_EXPIRED", "演示会话已失效，请重新选择用户", 401)
    return session, setting


def _proposal(db: Session, session: DemoSession, proposal_id: str) -> Proposal:
    row = db.scalar(select(Proposal).where(Proposal.id == proposal_id,
                                           Proposal.owner_id == session.owner_id,
                                           Proposal.session_id == session.id,
                                           Proposal.dataset_version == session.dataset_version))
    if row is None:
        raise DomainError("RESOURCE_NOT_FOUND", "资源不存在", 404)
    return row


def _task(db: Session, session: DemoSession, task_id: str) -> Task:
    row = db.scalar(select(Task).where(Task.id == task_id, Task.owner_id == session.owner_id,
                                       Task.session_id == session.id,
                                       Task.dataset_version == session.dataset_version))
    if row is None:
        raise DomainError("RESOURCE_NOT_FOUND", "资源不存在", 404)
    return row


def _audit(db: Session, owner_id: str, proposal_id: str | None, event: str,
           instant: datetime, detail_code: str | None = None) -> None:
    db.add(AuditEvent(id=uuid4().hex, owner_id=owner_id, proposal_id=proposal_id,
                      event=event, detail_code=detail_code, created_at_utc=_stamp(instant)))


def _update_agent_step(db: Session, step: AgentStep, proposal: Proposal, detail_code: str | None = None):
    step.status = {'pending_confirmation': 'awaiting_confirmation', 'executed': 'succeeded'}.get(
        proposal.status, proposal.status)
    step.result_ref = proposal.id
    step.result_json = _canonical({'proposal_id': proposal.id, 'status': proposal.status,
        'receipt': _receipt(db, proposal), 'error_code': detail_code,
        'validation': json.loads(proposal.validation_json) if proposal.validation_json else None})


def _end(db: Session, proposal: Proposal, status: str, instant: datetime,
         detail_code: str | None = None) -> None:
    proposal.status = status
    proposal.token_hash = None
    proposal.updated_at_utc = _stamp(instant)
    task = db.get(Task, proposal.task_id)
    task.status = status
    task.active_session_id = None
    task.updated_at_utc = _stamp(instant)
    _audit(db, proposal.owner_id, proposal.id, status, instant, detail_code)
    # Each proposal owns its actual step, including superseded single-scenario steps.
    for step in db.scalars(select(AgentStep).join(AgentTask, AgentStep.task_id == AgentTask.id).where(
            AgentStep.result_ref == proposal.id, AgentTask.owner_id == proposal.owner_id,
            AgentTask.session_id == proposal.session_id)).all():
        _update_agent_step(db, step, proposal, detail_code)
    for agent in db.scalars(select(AgentTask).where(AgentTask.proposal_id == proposal.id)).all():
        if agent.intent not in {'conditional_transfer', 'investigate_card'}:
            agent.status = {'executed': 'completed', 'invalidated': 'gathering_info'}.get(status, status)


def _current_active(db: Session, session: DemoSession, instant: datetime) -> tuple[Task, Proposal] | None:
    task = db.scalar(select(Task).where(Task.active_session_id == session.id))
    if task is None:
        return None
    proposal = db.scalar(select(Proposal).where(Proposal.task_id == task.id))
    if proposal is None:
        raise DomainError("DATA_UNAVAILABLE", "活动任务缺少操作单", 503)
    if proposal.status == PENDING and datetime.fromisoformat(proposal.expires_at_utc) <= instant:
        _end(db, proposal, "expired", instant)
        return None
    return task, proposal


def _preparing(db: Session, session: DemoSession, request_digest: str,
               task_id: str | None, instant: datetime) -> Proposal | None:
    from app.data.scheduled_models import ScheduledTransfer
    from app.data.collection_models import CollectionRequest
    from app.domain.wealth import ensure_no_pending_wealth
    ensure_no_pending_wealth(db, session.id, instant)
    for model in (ScheduledTransfer, CollectionRequest):
        pending = db.scalar(select(model.id).where(model.session_id == session.id,
            model.owner_id == session.owner_id, model.dataset_version == session.dataset_version,
            model.status == 'pending_confirmation', model.expires_at_utc > _stamp(instant)))
        if pending:
            raise DomainError('ACTIVE_WRITE_TASK_EXISTS', '请先确认或取消当前预约或AA收款草稿', 409)
    active = _current_active(db, session, instant)
    if active:
        task, proposal = active
        if task.request_digest == request_digest and (task_id is None or task_id == task.id):
            return proposal
        if task_id == task.id:
            _end(db, proposal, "invalidated", instant, "PARAMETERS_CHANGED")
        else:
            raise DomainError("ACTIVE_WRITE_TASK_EXISTS", "请先确认或取消当前操作单", 409)
    if task_id:
        old_task = _task(db, session, task_id)
        old_proposal = db.scalar(select(Proposal).where(Proposal.task_id == old_task.id))
        if old_task.request_digest == request_digest and old_proposal is not None:
            return old_proposal
    return None


def _create(db: Session, session: DemoSession, kind: str, params: dict,
            snapshot: dict, instant: datetime) -> Proposal:
    params_json = _canonical(params)
    snapshot_json = _canonical(snapshot)
    request_digest = _digest(params_json)
    task = Task(id=uuid4().hex, owner_id=session.owner_id, session_id=session.id,
                active_session_id=session.id, dataset_version=session.dataset_version,
                kind=kind, status="awaiting_confirmation", request_digest=request_digest,
                created_at_utc=_stamp(instant), updated_at_utc=_stamp(instant))
    proposal = Proposal(id=uuid4().hex, task_id=task.id, owner_id=session.owner_id,
                        session_id=session.id, dataset_version=session.dataset_version,
                        kind=kind, status=PENDING, params_json=params_json,
                        snapshot_json=snapshot_json,
                        params_digest=_digest(_canonical({"params": params, "snapshot": snapshot,
                                                         "dataset_version": session.dataset_version})),
                        token_hash=None, created_at_utc=_stamp(instant),
                        expires_at_utc=_stamp(instant + timedelta(seconds=TOKEN_SECONDS)),
                        updated_at_utc=_stamp(instant))
    db.add(task)
    db.flush()
    db.add(proposal)
    db.flush()
    _audit(db, session.owner_id, proposal.id, "prepared", instant)
    return proposal


def _receipt(db: Session, proposal: Proposal) -> dict | None:
    row = db.scalar(select(BusinessResult).where(BusinessResult.proposal_id == proposal.id))
    return json.loads(row.receipt_json) if row else None


def proposal_view(db: Session, proposal: Proposal, instant: datetime | None = None) -> dict:
    params = json.loads(proposal.params_json)
    result = _receipt(db, proposal)
    status = "expired" if (proposal.status == PENDING and
                           datetime.fromisoformat(proposal.expires_at_utc) <= _now(instant)) else proposal.status
    summary: dict = {"action": params["action"]}
    if proposal.kind == "transfer":
        source = db.get(Account, params["source_account_id"])
        target_id = params["target"].get("account_id")
        direct=params["target"]["type"]=="external_account"
        target = None if direct else db.get(Account, target_id) if target_id else db.get(Recipient, params["target"]["recipient_id"])
        target_summary=({"alias":params["target"]["name"],"bank_name":params["target"]["bank_name"],
                         "mask":"**"+params["target"]["account_number"][-4:],"account_number":params["target"]["account_number"]}
                        if direct else {"id":target.id,"alias":getattr(target,"alias",None) or target.name,"mask":"**"+target.account_last4})
        summary.update({"source": {"id": source.id, "alias": source.alias, "mask": "**" + source.account_last4},
                        "target": target_summary,
                        "target_type": params["target"]["type"], "amount_minor": params["amount_minor"],
                        "currency": "CNY", "note": params.get("note") or "",
                        "impact": "本人账户间资金移动" if target_id else "模拟来源账户出账及收款人入账；不接入真实银行"})
        if not target_id and not direct:
            summary["target"]["phone_mask"] = mask_phone(target.phone)
        if not target_id and status == "executed" and not (result or {}).get("recipient_credit_id"):
            summary["impact"] = "历史模拟出账；该回执未保存收款侧入账记录"
    else:
        card = db.get(Card, params["card_id"])
        summary.update({"card": {"id": card.id, "alias": card.alias, "mask": "**" + card.card_last4},
                        "impact": "只改变卡状态，不改变账户余额"})
    return {"id": proposal.id, "task_id": proposal.task_id, "kind": proposal.kind,
            "status": status, "created_at_utc": proposal.created_at_utc,
            "expires_at_utc": proposal.expires_at_utc,
            "summary": {**summary, "condition": params.get("condition")}, "receipt": result,
            "validation": json.loads(proposal.validation_json) if proposal.validation_json else None}


def task_view(db: Session, task: Task, instant: datetime | None = None) -> dict:
    proposal = db.scalar(select(Proposal).where(Proposal.task_id == task.id))
    events = db.scalars(select(AuditEvent).where(AuditEvent.proposal_id == proposal.id)
                        .order_by(AuditEvent.created_at_utc, AuditEvent.id)).all() if proposal else []
    return {"id": task.id, "kind": task.kind, "status": task.status,
            "proposal": proposal_view(db, proposal, instant) if proposal else None,
            "events": [{"event": row.event, "detail_code": row.detail_code,
                        "created_at_utc": row.created_at_utc} for row in events]}


@preparing
def prepare_transfer(db: Session, *, session_id: str, source_account_id: str,
                     target: dict, amount_minor: int, currency: str = "CNY",
                     note: str | None = None, task_id: str | None = None,
                     now: datetime | None = None, condition: dict | None = None,
                     agent_task_id: str | None = None, business_at: datetime | None = None) -> dict:
    instant = _now(now)
    if business_at is not None and business_at.tzinfo is None:
        raise DomainError("INVALID_INPUT", "业务时间必须包含时区")
    condition = normalize_condition(condition, "transfer")
    require_cny(currency)
    validate_transfer_amount(amount_minor)
    if note is not None and len(note) > 120:
        raise DomainError("INVALID_INPUT", "备注过长")
    if target.get("type")=="external_account":
        from app.domain.direct_recipient import validated_target
        target=validated_target(target)
    if target.get("type") not in {"own_account", "saved_recipient", "external_account"}:
        raise DomainError("INVALID_INPUT", "无效转账目标")
    target_id = target.get("account_id") if target["type"] == "own_account" else target.get("recipient_id")
    if target["type"]!="external_account" and (not isinstance(target_id, str) or not target_id):
        raise DomainError("INVALID_INPUT", "请选择转账目标")
    params = {"action": "transfer", "source_account_id": source_account_id,
              "target": target, "amount_minor": amount_minor, "currency": currency,
              "note": (note or "").strip()}
    if condition is not None:
        params["condition"] = condition
    request_digest = _digest(_canonical(params))

    def work():
        session, setting = _session(db, session_id, instant)
        existing = _preparing(db, session, request_digest, task_id, instant)
        if existing:
            _bind_agent(db, session, existing, agent_task_id)
            return {"proposal": proposal_view(db, existing, instant), "reused": True}
        if condition:
            evaluation = evaluate_condition(db, session.owner_id, condition)
            if not evaluation["met"]:
                return {"condition_met": False, "validation": evaluation}
        source = owned_account(db, session.owner_id, source_account_id)
        daily = daily_transfer_outflow(db, source.id, business_at.astimezone(SHANGHAI).date() if business_at is not None else parse_day(setting.demo_date))
        validate_transfer_amount(amount_minor, daily)
        if available_balance(db, source) < amount_minor:
            raise DomainError("INSUFFICIENT_FUNDS", "账户一般可用余额不足")
        snapshot = {"source_version": source.version, "source_balance_minor": source.balance_minor}
        if target["type"] == "own_account":
            if source.id == target_id:
                raise DomainError("INVALID_INPUT", "转出和转入账户不能相同")
            destination = owned_account(db, session.owner_id, target_id)
            snapshot["target_version"] = destination.version
        elif target["type"]=="external_account":
            snapshot["external_target_digest"]=_digest(_canonical(target))
        else:
            recipient = db.scalar(select(Recipient).where(Recipient.id == target_id,
                                                           Recipient.owner_id == session.owner_id))
            if recipient is None:
                raise DomainError("RESOURCE_NOT_FOUND", "资源不存在", 404)
            if recipient.status != "active":
                raise DomainError("INVALID_RECIPIENT_STATE", "收款人不可用")
            snapshot["recipient_version"] = recipient.version
        proposal = _create(db, session, "transfer", params, snapshot, instant)
        if condition:
            proposal.validation_json = _canonical(evaluation)
        _bind_agent(db, session, proposal, agent_task_id)
        return {"proposal": proposal_view(db, proposal, instant), "reused": False}

    return _write(db, work)


@preparing
def prepare_card_action(db: Session, *, session_id: str, card_id: str, action: str,
                        task_id: str | None = None, now: datetime | None = None,
                        condition: dict | None = None, agent_task_id: str | None = None) -> dict:
    instant = _now(now)
    condition = normalize_condition(condition, "card_action")
    if condition and (condition["card_id"] != card_id or action != "freeze"):
        raise DomainError("INVALID_CONDITION", "交易条件与冻结目标不一致")
    if action not in {"freeze", "unfreeze"}:
        raise DomainError("INVALID_INPUT", "无效卡操作")
    desired = "frozen" if action == "freeze" else "active"
    params = {"action": action, "card_id": card_id}
    if condition is not None:
        params["condition"] = condition
    request_digest = _digest(_canonical(params))

    def work():
        session, _setting = _session(db, session_id, instant)
        existing = _preparing(db, session, request_digest, task_id, instant)
        if existing:
            _bind_agent(db, session, existing, agent_task_id)
            return {"proposal": proposal_view(db, existing, instant), "reused": True}
        if condition:
            evaluation = evaluate_condition(db, session.owner_id, condition)
            if not evaluation["met"]:
                raise DomainError("TRANSACTION_CHANGED", "所选交易与卡片关联已变化", 409)
        card = db.scalar(select(Card).where(Card.id == card_id, Card.owner_id == session.owner_id))
        if card is None:
            raise DomainError("RESOURCE_NOT_FOUND", "资源不存在", 404)
        account = owned_account(db, session.owner_id, card.account_id)
        from app.domain.card_management import controls
        if action == 'unfreeze' and controls(db,card)['lost']:
            raise DomainError('CARD_LOST','卡片已挂失，解冻不能解除挂失',409)
        if card.status == desired:
            return {"already_in_state": True, "card": {"id": card.id, "status": card.status}}
        snapshot = {"card_version": card.version, "card_status": card.status,
                    "account_version": account.version, "account_status": account.status}
        proposal = _create(db, session, "card_action", params, snapshot, instant)
        if condition:
            proposal.validation_json = _canonical(evaluation)
        _bind_agent(db, session, proposal, agent_task_id)
        return {"proposal": proposal_view(db, proposal, instant), "reused": False}

    return _write(db, work)


def issue_confirmation_token(db: Session, *, session_id: str, proposal_id: str,
                             now: datetime | None = None) -> dict:
    instant = _now(now)

    def work():
        session, _setting = _session(db, session_id, instant)
        proposal = _proposal(db, session, proposal_id)
        if proposal.status != PENDING:
            return DomainError("INVALID_PROPOSAL_STATE", "操作单已不再等待确认", 409)
        if datetime.fromisoformat(proposal.expires_at_utc) <= instant:
            _end(db, proposal, "expired", instant)
            return DomainError("PROPOSAL_EXPIRED", "确认已过期，请重新准备", 409)
        token = secrets.token_urlsafe(32)
        proposal.token_hash = _digest(token)
        proposal.updated_at_utc = _stamp(instant)
        _audit(db, session.owner_id, proposal.id, "token_issued", instant)
        return {"confirmation_token": token, "expires_at_utc": proposal.expires_at_utc}

    result = _write(db, work)
    if isinstance(result, DomainError):
        raise result
    return result


def _check_snapshot(db: Session, owner_id: str, proposal: Proposal, params: dict, snapshot: dict) -> bool:
    if proposal.kind == "transfer":
        source = db.scalar(select(Account).where(Account.id == params["source_account_id"],
                                                 Account.owner_id == owner_id))
        if source is None or source.status != "active" or source.version != snapshot["source_version"]:
            return False
        target = params["target"]
        if target["type"] == "own_account":
            dest = db.scalar(select(Account).where(Account.id == target["account_id"],
                                                   Account.owner_id == owner_id))
            return dest is not None and dest.status == "active" and dest.version == snapshot["target_version"]
        if target["type"]=="external_account":
            from app.domain.direct_recipient import validated_target
            try:fixed=validated_target(target)
            except DomainError:return False
            return _digest(_canonical(fixed))==snapshot.get("external_target_digest")
        recipient = db.scalar(select(Recipient).where(Recipient.id == target["recipient_id"],
                                                       Recipient.owner_id == owner_id))
        return recipient is not None and recipient.status == "active" and recipient.version == snapshot["recipient_version"]
    card = db.scalar(select(Card).where(Card.id == params["card_id"], Card.owner_id == owner_id))
    if card is None or card.version != snapshot["card_version"] or card.status != snapshot["card_status"]:
        return False
    account = db.scalar(select(Account).where(Account.id == card.account_id, Account.owner_id == owner_id))
    return (account is not None and account.status == snapshot["account_status"]
            and account.version == snapshot["account_version"])


@secured('proposal', 'proposal_id')
def confirm_proposal(db: Session, *, session_id: str, proposal_id: str,
                     confirmation_token: str, now: datetime | None = None,
                     before_commit: Callable[[], None] | None = None,
                     business_at: datetime | None = None) -> dict:
    instant = _now(now)
    if business_at is not None and business_at.tzinfo is None:
        raise DomainError("INVALID_INPUT", "业务时间必须包含时区")

    def work():
        session, setting = _session(db, session_id, instant)
        proposal = _proposal(db, session, proposal_id)
        if proposal.status == "executed":
            result = _receipt(db, proposal)
            if result is None:
                raise DomainError("DATA_UNAVAILABLE", "已执行操作缺少回执", 503)
            return result
        if proposal.status != PENDING:
            return DomainError("INVALID_PROPOSAL_STATE", "操作单已不再等待确认", 409)
        if datetime.fromisoformat(proposal.expires_at_utc) <= instant:
            _end(db, proposal, "expired", instant)
            return DomainError("PROPOSAL_EXPIRED", "确认已过期，请重新准备", 409)
        if not proposal.token_hash or not hmac.compare_digest(proposal.token_hash, _digest(confirmation_token)):
            _audit(db, session.owner_id, proposal.id, "confirmation_rejected", instant, "CONFIRMATION_INVALID")
            return DomainError("CONFIRMATION_INVALID", "确认凭据无效", 403)
        params = json.loads(proposal.params_json)
        snapshot = json.loads(proposal.snapshot_json)
        expected = _digest(_canonical({"params": params, "snapshot": snapshot,
                                       "dataset_version": proposal.dataset_version}))
        if not hmac.compare_digest(expected, proposal.params_digest):
            _end(db, proposal, "invalidated", instant, "PARAMETERS_CHANGED")
            return DomainError("STALE_RESOURCE", "操作单参数已变化，请重新准备", 409)
        if params.get("condition"):
            evaluation = evaluate_condition(db, session.owner_id, params["condition"])
            proposal.validation_json = _canonical(evaluation)
            if not evaluation["met"]:
                _end(db, proposal, "invalidated", instant, evaluation["error_code"])
                message = "条件不满足，无需执行" if evaluation["complete"] else "统计数据不完整，不能执行"
                return DomainError(evaluation["error_code"], message, 409)
        if not _check_snapshot(db, session.owner_id, proposal, params, snapshot):
            _end(db, proposal, "invalidated", instant, "STALE_RESOURCE")
            return DomainError("STALE_RESOURCE", "账户或卡片状态已变化，请重新准备", 409)
        posting_at = business_at if business_at is not None else Clock(parse_day(setting.demo_date)).business_now()
        created = _stamp(instant)
        if proposal.kind == "transfer":
            source = db.get(Account, params["source_account_id"])
            try:
                validate_transfer_amount(params["amount_minor"], daily_transfer_outflow(
                    db, source.id, posting_at.astimezone(SHANGHAI).date()))
                if available_balance(db, source) < params["amount_minor"]:
                    raise DomainError("INSUFFICIENT_FUNDS", "账户一般可用余额不足")
            except DomainError as error:
                _end(db, proposal, "failed", instant, error.code)
                return error
            target = params["target"]
            if target["type"] == "own_account":
                debit, credit = post_internal_transfer(db, owner_id=session.owner_id,
                                                       source_id=source.id, target_id=target["account_id"],
                                                       amount_minor=params["amount_minor"],
                                                       business_at=posting_at, business_id=proposal.id,
                                                       system_now=instant)
                legs = [debit.id, credit.id]
                target_account = db.get(Account, target["account_id"])
                target_balance = target_account.balance_minor
                recipient_id = None
                target_account_id = target_account.id
            else:
                if target["type"]=="external_account":
                    # A receipt-side identity is created only inside the confirmed
                    # settlement transaction; it is not a saved-directory entry.
                    one_time=Recipient(id=uuid4().hex,owner_id=session.owner_id,name=target["name"],
                        account_last4=target["account_number"][-4:],status="one_time",version=1)
                    db.add(one_time);db.flush();recipient_id=one_time.id
                else:recipient_id=target["recipient_id"]
                debit = post_external_transfer(db, owner_id=session.owner_id, source_id=source.id,
                                               recipient_id=recipient_id,
                                               amount_minor=params["amount_minor"], business_at=posting_at,
                                               business_id=proposal.id, system_now=instant)
                legs = [debit.id]
                target_balance = None
                target_account_id = None
            order = TransferOrder(id=uuid4().hex, proposal_id=proposal.id, owner_id=session.owner_id,
                                  business_id=proposal.id, source_account_id=source.id,
                                  target_account_id=target_account_id, recipient_id=recipient_id,
                                  amount_minor=params["amount_minor"], currency="CNY", created_at_utc=created)
            db.add(order)
            receipt = {"proposal_id": proposal.id, "task_id": proposal.task_id, "order_id": order.id,
                       "kind": "transfer", "status": "executed", "message": "模拟转账成功",
                       "business_id": proposal.id, "transaction_ids": legs,
                       "amount_minor": params["amount_minor"], "currency": "CNY",
                       "source_account_id": source.id, "source_balance_minor": source.balance_minor,
                       "target_account_id": target_account_id, "target_balance_minor": target_balance,
                       "recipient_id": recipient_id, "committed_at_utc": created}
            if recipient_id:
                credit = db.scalar(select(RecipientCredit).where(RecipientCredit.business_id == proposal.id))
                receipt.update(recipient_credit_id=credit.id, recipient_credit_minor=credit.amount_minor,
                               recipient_credit_status="posted")
        else:
            card = db.get(Card, params["card_id"])
            from app.domain.card_management import controls
            if params['action']=='unfreeze' and controls(db,card)['lost']:
                raise DomainError('CARD_LOST','解冻不能解除挂失',409)
            before = card.status
            card.status = "frozen" if params["action"] == "freeze" else "active"
            card.version += 1
            order = CardActionOrder(id=uuid4().hex, proposal_id=proposal.id, owner_id=session.owner_id,
                                    card_id=card.id, action=params["action"], before_status=before,
                                    after_status=card.status, created_at_utc=created)
            db.add(order)
            receipt = {"proposal_id": proposal.id, "task_id": proposal.task_id, "order_id": order.id,
                       "kind": "card_action", "status": "executed",
                       "message": "卡片已冻结" if params["action"] == "freeze" else "卡片已解冻",
                       "card_id": card.id, "card_status": card.status,
                       "card_version": card.version, "committed_at_utc": created}
        db.add(BusinessResult(id=uuid4().hex, proposal_id=proposal.id, owner_id=session.owner_id,
                              kind=proposal.kind, receipt_json=_canonical(receipt), created_at_utc=created))
        _end(db, proposal, "executed", instant)
        if before_commit:
            before_commit()
        return receipt

    try:
        result = _write(db, work)
    except OperationalError as error:
        raise DomainError("RETRYABLE_CONFLICT", "数据库正忙，请先查询原操作单状态后重试", 409) from error
    except Exception:
        # The business transaction rolled back. A separate audit record may describe
        # the failed attempt, while the still-pending proposal remains retryable.
        try:
            def failure_audit():
                session, _setting = _session(db, session_id, instant)
                proposal = _proposal(db, session, proposal_id)
                if proposal.status == PENDING:
                    _audit(db, session.owner_id, proposal.id, "execution_error", instant, "ROLLED_BACK")
            _write(db, failure_audit)
        except Exception:
            logger.exception("Could not persist execution failure audit for proposal %s", proposal_id)
        raise
    if isinstance(result, DomainError):
        # The security wrapper commits the deterministic failure state, then
        # raises this outcome after leaving the financial transaction.
        return result
    return result


def cancel_proposal(db: Session, *, session_id: str, proposal_id: str,
                    now: datetime | None = None) -> dict:
    instant = _now(now)

    def work():
        session, _setting = _session(db, session_id, instant)
        proposal = _proposal(db, session, proposal_id)
        if proposal.status == PENDING:
            if datetime.fromisoformat(proposal.expires_at_utc) <= instant:
                _end(db, proposal, "expired", instant)
            else:
                _end(db, proposal, "cancelled", instant)
        return proposal_view(db, proposal, instant)

    return _write(db, work)


def invalidate_session_proposals(db: Session, session_id: str, instant: datetime) -> None:
    from app.domain.birthdays import invalidate_session as invalidate_birthday_session
    invalidate_birthday_session(db, session_id)
    from app.domain.subscriptions import invalidate_session as invalidate_subscription_session
    invalidate_subscription_session(db,session_id)
    from app.domain.wealth import invalidate_session_orders
    invalidate_session_orders(db, session_id)
    from app.domain.card_management import invalidate_session
    invalidate_session(db,session_id)
    rows = db.scalars(select(Proposal).where(Proposal.session_id == session_id,
                                             Proposal.status == PENDING)).all()
    for proposal in rows:
        _end(db, proposal, "invalidated", instant, "SESSION_SWITCHED")


def get_proposal(db: Session, *, session_id: str, proposal_id: str,
                 now: datetime | None = None) -> dict:
    instant = _now(now)
    session, _setting = _session(db, session_id, instant)
    return proposal_view(db, _proposal(db, session, proposal_id), instant)


def get_task(db: Session, *, session_id: str, task_id: str,
             now: datetime | None = None) -> dict:
    instant = _now(now)
    session, _setting = _session(db, session_id, instant)
    return task_view(db, _task(db, session, task_id), instant)


def get_current_task(db: Session, *, session_id: str, now: datetime | None = None) -> dict | None:
    instant = _now(now)
    session, _setting = _session(db, session_id, instant)
    task = db.scalar(select(Task).where(Task.owner_id == session.owner_id,
                                        Task.session_id == session.id,
                                        Task.dataset_version == session.dataset_version)
                     .order_by(Task.created_at_utc.desc(), Task.id.desc()))
    return task_view(db, task, instant) if task else None


def invalidate_agent_proposal(db: Session, *, session_id: str, proposal_id: str,
                              now: datetime | None = None) -> dict:
    instant = _now(now)

    def work():
        session, _setting = _session(db, session_id, instant)
        proposal = _proposal(db, session, proposal_id)
        if proposal.status == PENDING:
            _end(db, proposal, "invalidated", instant, "AGENT_REVISION")
        return proposal_view(db, proposal, instant)

    return _write(db, work)


def _bind_agent(db: Session, session: DemoSession, proposal: Proposal, agent_task_id: str | None):
    if not agent_task_id:
        return
    agent = db.get(AgentTask, agent_task_id)
    if (not agent or agent.owner_id != session.owner_id or agent.session_id != session.id
            or agent.dataset_version != session.dataset_version or agent.stop_requested
            or agent.status in {"cancelled", "completed", "expired", "failed", "partially_completed"}):
        raise DomainError("INVALID_TASK_STATE", "任务已失效或取消", 409)
    other = db.scalar(select(AgentTask.id).where(AgentTask.proposal_id == proposal.id, AgentTask.id != agent.id))
    if other:
        raise DomainError("ACTIVE_WRITE_TASK_EXISTS", "操作单已关联其他任务", 409)
    agent.proposal_id = proposal.id
    agent.status = "awaiting_confirmation"
    # Cross-scenario plans preinstall a running write step. Single-scenario
    # steps are recorded after execute returns; never rebind their historical first step.
    if agent.intent in {'conditional_transfer', 'investigate_card'}:
        tool = 'prepare_transfer' if proposal.kind == 'transfer' else 'prepare_card_action'
        step = db.scalar(select(AgentStep).where(AgentStep.task_id == agent.id,
                         AgentStep.plan_version == agent.plan_version,
                         AgentStep.tool_name == tool, AgentStep.status == 'running')
                         .order_by(AgentStep.ordinal.desc()))
        if step:
            _update_agent_step(db, step, proposal)


def sync_single_agent_steps(db: Session, agent_task_id: str):
    """Recover legacy preparation history from durable proposals, never replay a write."""
    def work():
        agent = db.get(AgentTask, agent_task_id)
        if not agent or agent.intent in {'conditional_transfer', 'investigate_card'}:
            return
        for step in db.scalars(select(AgentStep).where(AgentStep.task_id == agent.id,
                AgentStep.tool_name.in_(['prepare_transfer', 'prepare_card_action']))).all():
            result = json.loads(step.result_json)
            pid = (result.get('proposal') or {}).get('id') or step.result_ref or result.get('proposal_id')
            if not pid:
                continue  # e.g. card already in the requested state, no business proposal
            proposal = db.get(Proposal, pid)
            if not proposal or (proposal.owner_id, proposal.session_id, proposal.dataset_version) != (
                    agent.owner_id, agent.session_id, agent.dataset_version):
                continue
            args, params = json.loads(step.args_json), json.loads(proposal.params_json)
            keys = ('source_account_id', 'target', 'amount_minor', 'condition') if step.tool_name == 'prepare_transfer' else ('card_id', 'action', 'condition')
            if any(args.get(key) != params.get(key) for key in keys):
                # Old code overwrote the first step's link when parameters changed.
                # Its original proposal is ambiguous; do not attach the new receipt.
                step.status = 'invalidated'
                step.result_ref = None
                step.result_json = _canonical({'status': 'invalidated',
                    'reason': '原操作参数已修改，此准备步骤已失效，未按该步骤执行。'})
            else:
                _update_agent_step(db, step, proposal, result.get('error_code'))
    _write(db, work)
