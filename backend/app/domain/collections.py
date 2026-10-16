"""Symbolic AA split, lifecycle and posting rules; no model can mark a member paid."""
from __future__ import annotations
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.data.atomic import atomic_write
from app.data.collection_models import CollectionMember, CollectionPayment, CollectionRequest
from app.data.models import Account, AgentTask, AuditEvent, DemoSession, DemoSetting, Recipient, Proposal, Transaction, User
from app.domain.errors import DomainError
from app.domain.ledger import owned_account, post_entry
from app.domain.money import validate_minor

TOKEN_SECONDS = 300
MAX_PARTICIPANTS = 20
MAX_COLLECTION_TOTAL_MINOR = 5_000_000


def _now(value: datetime | None = None) -> datetime:
    instant = value or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise DomainError("INVALID_INPUT", "实际时间必须带时区")
    return instant.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _session(db: Session, session_id: str, now: datetime) -> DemoSession:
    row = db.get(DemoSession, session_id)
    setting = db.get(DemoSetting, 1)
    if (row is None or setting is None or row.dataset_version != setting.dataset_version
            or datetime.fromisoformat(row.expires_at_utc) <= now):
        raise DomainError("SESSION_EXPIRED", "演示会话已失效，请重新选择用户", 401)
    return row


def _request(db: Session, session: DemoSession, request_id: str) -> CollectionRequest:
    row = db.scalar(select(CollectionRequest).where(
        CollectionRequest.id == request_id, CollectionRequest.owner_id == session.owner_id,
        CollectionRequest.dataset_version == session.dataset_version))
    if row is None:
        raise DomainError("RESOURCE_NOT_FOUND", "资源不存在", 404)
    return row


def _members(db: Session, request_id: str) -> list[CollectionMember]:
    return list(db.scalars(select(CollectionMember).where(
        CollectionMember.request_id == request_id).order_by(CollectionMember.ordinal)))


def _member(db: Session, request: CollectionRequest, member_id: str) -> CollectionMember:
    row = db.scalar(select(CollectionMember).where(CollectionMember.id == member_id,
                                                 CollectionMember.request_id == request.id))
    if row is None:
        raise DomainError("RESOURCE_NOT_FOUND", "成员不存在", 404)
    return row


def _audit(db: Session, request: CollectionRequest, event: str, now: datetime, reference: str | None = None):
    db.add(AuditEvent(id=uuid4().hex, owner_id=request.owner_id, proposal_id=None,
                      event=event, detail_code=reference or request.id, created_at_utc=_stamp(now)))


def _expire(db: Session, request: CollectionRequest, now: datetime) -> None:
    if request.status == "pending_confirmation" and datetime.fromisoformat(request.expires_at_utc) <= now:
        request.status = "expired"
        request.token_hash = None
        request.updated_at_utc = _stamp(now)
        _audit(db, request, "collection_expired", now)
        db.flush()


def _receipt(payment: CollectionPayment) -> dict:
    return {"id": payment.id, "collection_id": payment.request_id, "member_id": payment.member_id,
            "transaction_id": payment.transaction_id, "business_id": payment.business_id,
            "amount_minor": payment.amount_minor, "currency": "CNY", "simulated": True,
            "channel": payment.channel, "created_at_utc": payment.created_at_utc}


def _view(db: Session, request: CollectionRequest) -> dict:
    members = _members(db, request.id)
    payments = {row.member_id: row for row in db.scalars(select(CollectionPayment).where(
        CollectionPayment.request_id == request.id))}
    account = db.get(Account, request.destination_account_id)
    received = sum(row.amount_minor for row in payments.values())
    return {"id": request.id, "agent_task_id": request.agent_task_id,
            "status": request.status, "title": request.title, "total_minor": request.total_minor,
            "self_share_minor": request.self_share_minor, "expected_minor": request.expected_minor,
            "received_minor": received, "remaining_minor": request.expected_minor - received,
            "currency": "CNY", "simulated": True,
            "payment_channel": "simulated_member_payment",
            "payment_channel_label": "模拟成员付款（演示通道，非真实银行支付）",
            "split_method": "equal",
            "remainder_rule": "按确认卡成员顺序（本人第一）依次多分1分，分摊总额等于垫付总额",
            "destination_account_id": request.destination_account_id,
            "destination_account": {"id": account.id, "alias": account.alias,
                                    "account_mask": "**" + account.account_last4},
            "members": [{"id": row.id, "recipient_id": row.recipient_id,
                         "ordinal": row.ordinal, "name": row.display_name,
                         "account_mask": "**" + row.account_last4 if row.account_last4 else None,
                         "is_self": row.recipient_id is None, "share_minor": row.share_minor,
                         "due_minor": 0 if row.recipient_id is None else row.share_minor,
                         "paid_minor": payments[row.id].amount_minor if row.id in payments else 0,
                         "status": row.status, "paid_at_utc": row.paid_at_utc,
                         "receipt": _receipt(payments[row.id]) if row.id in payments else None}
                        for row in members],
            "created_at_utc": request.created_at_utc, "expires_at_utc": request.expires_at_utc,
            "launched_at_utc": request.launched_at_utc, "updated_at_utc": request.updated_at_utc}


def equal_split(total_minor: int, participant_count: int) -> list[int]:
    total = validate_minor(total_minor)
    if (isinstance(participant_count, bool) or not isinstance(participant_count, int)
            or not 2 <= participant_count <= MAX_PARTICIPANTS):
        raise DomainError("INVALID_SPLIT", "AA分摊需包含本人及至少1名外部成员，最多20人")
    if total > MAX_COLLECTION_TOTAL_MINOR:
        raise DomainError("LIMIT_EXCEEDED", "AA垫付总额不能超过演示限额50000元")
    if total < participant_count:
        raise DomainError("INVALID_SPLIT", "总额不足以给每位参与人分摊至少1分")
    base, remainder = divmod(total, participant_count)
    return [base + (1 if ordinal < remainder else 0) for ordinal in range(participant_count)]


from app.domain.security import preparing

@preparing
def prepare_collection(db: Session, *, session_id: str, destination_account_id: str,
                       total_minor: int, recipient_ids: list[str], title: str = "AA收款",
                       request_key: str | None = None, agent_task_id: str | None = None,
                       split_method: str = "equal", now: datetime | None = None) -> dict:
    instant = _now(now)
    if split_method != "equal":
        raise DomainError("UNSUPPORTED_SPLIT", "当前AA仅支持平均分摊，请确认平均分摊规则")
    if (not isinstance(recipient_ids, list) or not all(isinstance(item, str) and item for item in recipient_ids)
            or len(set(recipient_ids)) != len(recipient_ids)):
        raise DomainError("INVALID_SPLIT", "成员标识必须明确且不能重复")
    shares = equal_split(total_minor, len(recipient_ids) + 1)
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 80:
        raise DomainError("INVALID_INPUT", "AA标题需为1至80个字符")
    if request_key is not None and (not isinstance(request_key, str) or not 1 <= len(request_key) <= 120):
        raise DomainError("INVALID_INPUT", "无效请求标识")
    title = title.strip()
    digest = _digest(json.dumps({"destination_account_id": destination_account_id,
        "total_minor": total_minor, "recipient_ids": recipient_ids, "title": title,
        "agent_task_id": agent_task_id, "split_method": split_method}, sort_keys=True, ensure_ascii=False))
    with atomic_write(db):
        session = _session(db, session_id, instant)
        from app.domain.wealth import ensure_no_pending_wealth
        ensure_no_pending_wealth(db, session.id, instant)
        if request_key:
            existing = db.scalar(select(CollectionRequest).where(
                CollectionRequest.owner_id == session.owner_id,
                CollectionRequest.dataset_version == session.dataset_version,
                CollectionRequest.request_key == request_key))
            if existing:
                if existing.request_digest != digest:
                    raise DomainError("IDEMPOTENCY_CONFLICT", "该请求标识已用于不同AA需求", 409)
                _expire(db, existing, instant)
                return _view(db, existing)
        # Authorized collecting requests coexist; pending write intentions do not.
        from app.data.scheduled_models import ScheduledTransfer
        pending_proposal = db.scalar(select(Proposal.id).where(
            Proposal.session_id == session.id, Proposal.owner_id == session.owner_id,
            Proposal.dataset_version == session.dataset_version,
            Proposal.status == "pending_confirmation", Proposal.expires_at_utc > _stamp(instant)))
        pending_collection = db.scalar(select(CollectionRequest.id).where(
            CollectionRequest.session_id == session.id, CollectionRequest.owner_id == session.owner_id,
            CollectionRequest.dataset_version == session.dataset_version,
            CollectionRequest.status == "pending_confirmation",
            CollectionRequest.expires_at_utc > _stamp(instant)))
        pending_schedule = db.scalar(select(ScheduledTransfer.id).where(
            ScheduledTransfer.session_id == session.id, ScheduledTransfer.owner_id == session.owner_id,
            ScheduledTransfer.dataset_version == session.dataset_version,
            ScheduledTransfer.status == "pending_confirmation",
            ScheduledTransfer.expires_at_utc > _stamp(instant)))
        if pending_proposal or pending_collection or pending_schedule:
            raise DomainError("ACTIVE_TASK_EXISTS", "请先确认或取消当前待确认操作", 409)
        account = owned_account(db, session.owner_id, destination_account_id)
        if agent_task_id:
            task = db.scalar(select(AgentTask).where(AgentTask.id == agent_task_id,
                AgentTask.owner_id == session.owner_id, AgentTask.session_id == session.id,
                AgentTask.dataset_version == session.dataset_version))
            if task is None:
                raise DomainError("RESOURCE_NOT_FOUND", "任务不存在", 404)
        recipients = []
        for recipient_id in recipient_ids:
            recipient = db.scalar(select(Recipient).where(Recipient.id == recipient_id,
                                                         Recipient.owner_id == session.owner_id))
            if recipient is None:
                raise DomainError("RESOURCE_NOT_FOUND", "成员不存在", 404)
            if recipient.status != "active":
                raise DomainError("INVALID_RECIPIENT_STATE", "成员当前不可用")
            recipients.append(recipient)
        request = CollectionRequest(id=uuid4().hex, owner_id=session.owner_id, session_id=session.id,
            dataset_version=session.dataset_version, agent_task_id=agent_task_id,
            destination_account_id=account.id, account_version=account.version, title=title,
            total_minor=total_minor, self_share_minor=shares[0], expected_minor=sum(shares[1:]),
            status="pending_confirmation", request_key=request_key, request_digest=digest,
            created_at_utc=_stamp(instant), updated_at_utc=_stamp(instant),
            expires_at_utc=_stamp(instant + timedelta(seconds=TOKEN_SECONDS)))
        db.add(request)
        db.flush()
        owner = db.get(User, session.owner_id)
        db.add(CollectionMember(id=uuid4().hex, request_id=request.id, ordinal=0,
            display_name=owner.display_name + "（本人）", share_minor=shares[0], status="self_share"))
        for ordinal, recipient in enumerate(recipients, start=1):
            db.add(CollectionMember(id=uuid4().hex, request_id=request.id, ordinal=ordinal,
                recipient_id=recipient.id, recipient_version=recipient.version,
                display_name=recipient.name, account_last4=recipient.account_last4,
                share_minor=shares[ordinal], status="unpaid"))
        db.flush()
        _audit(db, request, "collection_prepared", instant)
        return _view(db, request)


def get_collection(db: Session, *, session_id: str, collection_id: str, now: datetime | None = None) -> dict:
    instant = _now(now)
    with atomic_write(db):
        session = _session(db, session_id, instant)
        request = _request(db, session, collection_id)
        _expire(db, request, instant)
        return _view(db, request)


def list_collections(db: Session, *, session_id: str, now: datetime | None = None) -> list[dict]:
    instant = _now(now)
    with atomic_write(db):
        session = _session(db, session_id, instant)
        requests = list(db.scalars(select(CollectionRequest).where(
            CollectionRequest.owner_id == session.owner_id,
            CollectionRequest.dataset_version == session.dataset_version
        ).order_by(CollectionRequest.created_at_utc.desc()).limit(100)))
        for request in requests:
            _expire(db, request, instant)
        return [_view(db, request) for request in requests]


def _check_request_snapshot(db: Session, request: CollectionRequest) -> None:
    account = owned_account(db, request.owner_id, request.destination_account_id)
    if account.version != request.account_version:
        raise DomainError("STALE_COLLECTION", "到账账户状态已变化，请重新准备AA请求", 409)
    for member in _members(db, request.id):
        if member.recipient_id is None:
            continue
        recipient = db.scalar(select(Recipient).where(Recipient.id == member.recipient_id,
                                                    Recipient.owner_id == request.owner_id))
        if recipient is None or recipient.status != "active" or recipient.version != member.recipient_version:
            raise DomainError("STALE_COLLECTION", "AA成员状态已变化，请重新准备AA请求", 409)


def _binding_digest(db: Session, request: CollectionRequest, *, expires_at_utc: str | None,
                    token_session_id: str | None, member_id: str | None = None) -> str:
    """Bind a UI authorization to every fixed request/member parameter and its deadline."""
    members = _members(db, request.id)
    shares = equal_split(request.total_minor, len(members))
    if (len(members) < 2 or members[0].recipient_id is not None
            or any(row.recipient_id is None for row in members[1:])
            or [row.ordinal for row in members] != list(range(len(members)))
            or [row.share_minor for row in members] != shares
            or request.self_share_minor != shares[0]
            or request.expected_minor != sum(shares[1:])):
        raise DomainError("STALE_COLLECTION", "AA固定分摊参数已变化，请重新准备请求", 409)
    params = {"destination_account_id": request.destination_account_id,
              "total_minor": request.total_minor,
              "recipient_ids": [row.recipient_id for row in members[1:]],
              "title": request.title, "agent_task_id": request.agent_task_id, "split_method": "equal"}
    current = _digest(json.dumps(params, sort_keys=True, ensure_ascii=False))
    if not hmac.compare_digest(current, request.request_digest):
        raise DomainError("STALE_COLLECTION", "AA固定需求参数已变化，请重新准备请求", 409)
    frozen = {"request_id": request.id, "owner_id": request.owner_id,
              "dataset_version": request.dataset_version, "request_digest": request.request_digest,
              "destination_account_id": request.destination_account_id,
              "account_version": request.account_version, "total_minor": request.total_minor,
              "self_share_minor": request.self_share_minor, "expected_minor": request.expected_minor,
              "title": request.title, "expires_at_utc": expires_at_utc,
              "token_session_id": token_session_id, "payment_member_id": member_id,
              "members": [{"id": row.id, "ordinal": row.ordinal,
                           "recipient_id": row.recipient_id, "recipient_version": row.recipient_version,
                           "name": row.display_name, "account_last4": row.account_last4,
                           "share_minor": row.share_minor} for row in members]}
    return _digest(json.dumps(frozen, sort_keys=True, ensure_ascii=False))


def _valid_token(token: str, expected_hash: str | None, token_session_id: str | None,
                 session_id: str, binding_digest: str) -> bool:
    return bool(isinstance(token, str) and token and expected_hash and token_session_id == session_id
                and hmac.compare_digest(_digest(token), expected_hash)
                and hmac.compare_digest(token.partition(".")[0], binding_digest))


def issue_collection_confirmation_token(db: Session, *, session_id: str, collection_id: str,
                                      now: datetime | None = None) -> dict:
    instant = _now(now)
    with atomic_write(db):
        session = _session(db, session_id, instant)
        request = _request(db, session, collection_id)
        if request.status != "pending_confirmation" or datetime.fromisoformat(request.expires_at_utc) <= instant:
            raise DomainError("COLLECTION_NOT_PENDING", "AA请求已失效或已发起", 409)
        _check_request_snapshot(db, request)
        request.token_session_id = session.id
        request.token_expires_at_utc = min(request.expires_at_utc,
            _stamp(instant + timedelta(seconds=TOKEN_SECONDS)))
        binding = _binding_digest(db, request, expires_at_utc=request.token_expires_at_utc,
                                  token_session_id=session.id)
        token = binding + "." + secrets.token_urlsafe(32)
        request.token_hash = _digest(token)
        _audit(db, request, "collection_confirmation_issued", instant)
        return {"confirmation_token": token, "expires_at_utc": request.token_expires_at_utc}


from app.domain.security import secured

@secured('collection', 'collection_id')
def confirm_collection(db: Session, *, session_id: str, collection_id: str,
                       confirmation_token: str, now: datetime | None = None) -> dict:
    instant = _now(now)
    with atomic_write(db):
        session = _session(db, session_id, instant)
        request = _request(db, session, collection_id)
        if not _valid_token(confirmation_token, request.token_hash, request.token_session_id, session.id,
                            _binding_digest(db, request, expires_at_utc=request.token_expires_at_utc,
                                            token_session_id=request.token_session_id)):
            raise DomainError("INVALID_CONFIRMATION_TOKEN", "确认凭据无效，请使用专用确认按钮", 409)
        if request.status in {"collecting", "completed"}:
            return _view(db, request)
        if (request.status != "pending_confirmation" or request.token_expires_at_utc is None
                or datetime.fromisoformat(request.token_expires_at_utc) <= instant
                or datetime.fromisoformat(request.expires_at_utc) <= instant):
            raise DomainError("COLLECTION_NOT_PENDING", "AA请求已失效或不再待确认", 409)
        _check_request_snapshot(db, request)
        request.status = "collecting"
        request.launched_at_utc = _stamp(instant)
        request.updated_at_utc = _stamp(instant)
        _audit(db, request, "collection_launched", instant)
        db.flush()
        return _view(db, request)


def cancel_collection(db: Session, *, session_id: str, collection_id: str,
                      now: datetime | None = None) -> dict:
    instant = _now(now)
    with atomic_write(db):
        session = _session(db, session_id, instant)
        request = _request(db, session, collection_id)
        if request.status == "completed":
            raise DomainError("COLLECTION_COMPLETED", "AA已全部到账，不能撤销已入账款项", 409)
        if request.status in {"cancelled", "expired"}:
            return _view(db, request)
        request.status = "cancelled"
        request.updated_at_utc = _stamp(instant)
        request.token_hash = None
        for member in _members(db, request.id):
            member.token_hash = None
            if member.status == "unpaid":
                member.status = "cancelled"
        _audit(db, request, "collection_cancelled", instant)
        db.flush()
        return _view(db, request)


def issue_member_payment_token(db: Session, *, session_id: str, collection_id: str,
                              member_id: str, now: datetime | None = None) -> dict:
    instant = _now(now)
    with atomic_write(db):
        session = _session(db, session_id, instant)
        request = _request(db, session, collection_id)
        member = _member(db, request, member_id)
        if request.status != "collecting" or member.status != "unpaid":
            raise DomainError("MEMBER_NOT_PAYABLE", "AA未发起、已结束，或该成员无需再付款", 409)
        owned_account(db, request.owner_id, request.destination_account_id)
        recipient = db.scalar(select(Recipient).where(Recipient.id == member.recipient_id,
                                                     Recipient.owner_id == request.owner_id))
        if recipient is None or recipient.status != "active":
            raise DomainError("INVALID_RECIPIENT_STATE", "成员当前不可用")
        member.token_session_id = session.id
        member.token_expires_at_utc = _stamp(instant + timedelta(seconds=TOKEN_SECONDS))
        binding = _binding_digest(db, request, expires_at_utc=member.token_expires_at_utc,
                                  token_session_id=session.id, member_id=member.id)
        token = binding + "." + secrets.token_urlsafe(32)
        member.token_hash = _digest(token)
        _audit(db, request, "collection_simulated_payment_issued", instant, member.id)
        return {"payment_token": token, "member_id": member.id, "amount_minor": member.share_minor,
                "expires_at_utc": member.token_expires_at_utc, "simulated": True}


def simulate_member_payment(db: Session, *, session_id: str, collection_id: str, member_id: str,
                            payment_token: str, expected_amount_minor: int, simulated: bool,
                            now: datetime | None = None) -> dict:
    instant = _now(now)
    if simulated is not True:
        raise DomainError("SIMULATION_CONFIRMATION_REQUIRED", "请明确确认使用模拟成员付款演示通道")
    amount = validate_minor(expected_amount_minor)
    with atomic_write(db):
        session = _session(db, session_id, instant)
        request = _request(db, session, collection_id)
        member = _member(db, request, member_id)
        if member.recipient_id is None or amount != member.share_minor:
            raise DomainError("INVALID_PAYMENT_AMOUNT", "成员或金额与AA应付份额不一致", 409)
        payment = db.scalar(select(CollectionPayment).where(CollectionPayment.member_id == member.id))
        if payment:
            if not _valid_token(payment_token, payment.authorization_hash,
                                payment.authorization_session_id, session.id,
                                _binding_digest(db, request, expires_at_utc=member.token_expires_at_utc,
                                    token_session_id=payment.authorization_session_id, member_id=member.id)):
                raise DomainError("INVALID_PAYMENT_TOKEN", "模拟付款凭据无效", 409)
            return {"collection": _view(db, request), "receipt": _receipt(payment), "replayed": True}
        if request.status != "collecting" or member.status != "unpaid":
            raise DomainError("MEMBER_NOT_PAYABLE", "AA未发起、已取消，或成员无需付款", 409)
        if (not _valid_token(payment_token, member.token_hash, member.token_session_id, session.id,
                            _binding_digest(db, request, expires_at_utc=member.token_expires_at_utc,
                                token_session_id=member.token_session_id, member_id=member.id))
                or member.token_expires_at_utc is None
                or datetime.fromisoformat(member.token_expires_at_utc) <= instant):
            raise DomainError("INVALID_PAYMENT_TOKEN", "模拟付款凭据已失效，请使用成员付款按钮", 409)
        owned_account(db, request.owner_id, request.destination_account_id)
        recipient = db.scalar(select(Recipient).where(Recipient.id == member.recipient_id,
                                                     Recipient.owner_id == request.owner_id))
        if recipient is None or recipient.status != "active":
            raise DomainError("INVALID_RECIPIENT_STATE", "成员当前不可用")
        business_id = "aa:" + member.id
        transaction = post_entry(db, owner_id=request.owner_id, account_id=request.destination_account_id,
            kind="income", direction="credit", amount_minor=amount, business_at=instant,
            system_now=instant, business_id=business_id, merchant="AA收款 · " + member.display_name)
        payment = CollectionPayment(id=uuid4().hex, request_id=request.id, member_id=member.id,
            owner_id=request.owner_id, transaction_id=transaction.id, business_id=business_id,
            amount_minor=amount, channel="simulated_member_payment", authorization_hash=member.token_hash,
            authorization_session_id=session.id, created_at_utc=_stamp(instant))
        db.add(payment)
        member.status = "paid"
        member.paid_at_utc = _stamp(instant)
        member.token_hash = None
        request.updated_at_utc = _stamp(instant)
        db.flush()
        members = _members(db, request.id)
        if all(row.status in {"self_share", "paid"} for row in members):
            request.status = "completed"
        _audit(db, request, "collection_simulated_member_paid", instant, payment.id)
        db.flush()
        return {"collection": _view(db, request), "receipt": _receipt(payment), "replayed": False}


def collection_ledger_errors(db: Session) -> list[str]:
    """Cross-check AA receipts against real postings and conserved member shares."""
    errors = []
    for request in db.scalars(select(CollectionRequest)):
        members = _members(db, request.id)
        self_members = [row for row in members if row.recipient_id is None]
        if (len(self_members) != 1 or sum(row.share_minor for row in members) != request.total_minor
                or (self_members and self_members[0].share_minor != request.self_share_minor)):
            errors.append("collection_split:" + request.id)
        payments = list(db.scalars(select(CollectionPayment).where(CollectionPayment.request_id == request.id)))
        if sum(row.amount_minor for row in payments) > request.expected_minor:
            errors.append("collection_overpaid:" + request.id)
        for payment in payments:
            member = db.get(CollectionMember, payment.member_id)
            transaction = db.get(Transaction, payment.transaction_id)
            if (member is None or member.request_id != request.id or member.recipient_id is None
                    or member.status != "paid" or member.share_minor != payment.amount_minor
                    or payment.owner_id != request.owner_id or transaction is None
                    or transaction.owner_id != request.owner_id
                    or transaction.account_id != request.destination_account_id
                    or transaction.type != "income" or transaction.direction != "credit"
                    or transaction.status != "posted" or transaction.currency != "CNY"
                    or transaction.amount_minor != payment.amount_minor
                    or transaction.business_id != payment.business_id):
                errors.append("collection_payment:" + payment.id)
        paid_ids = {row.member_id for row in payments}
        if any(row.status == "paid" and row.id not in paid_ids for row in members):
            errors.append("collection_missing_receipt:" + request.id)
        if request.status == "completed" and (sum(row.amount_minor for row in payments) != request.expected_minor
                or any(row.status not in {"self_share", "paid"} for row in members)):
            errors.append("collection_completion:" + request.id)
    return errors
