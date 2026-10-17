"""Server-owned policy, one-use transaction-bound challenges and bounded grants.

SMS delivery is a LOCAL SIMULATOR, not bank authentication. Codes are available
through the local testing popup or simulator CLI, never through model tools.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from uuid import uuid4

from sqlalchemy import func, select
from app.data.atomic import atomic_write
from app.data.models import AuditEvent, AgentTask, DemoSession, DemoSetting, Transaction
from app.data.security_models import SecurityAuthorization, SecurityChallenge, SecurityState
from app.domain.clock import SHANGHAI
from app.domain.errors import DomainError

POLICY_VERSION = 'p0-2026-10-14-v1'
FAILURE_LIMIT = 5
LOCK_MINUTES = 15
CHALLENGE_SECONDS = 180
TERMINAL = {'executed', 'settled', 'submitted', 'issued', 'scheduled', 'rejected'}


def now():
    return datetime.now(timezone.utc)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def audit(db, owner, event, code=None):
    db.add(AuditEvent(id=uuid4().hex, owner_id=owner, event=event,
                      detail_code=code, created_at_utc=now().isoformat()))


def assert_unlocked(db, owner):
    state = db.get(SecurityState, owner)
    if state and state.locked_until and datetime.fromisoformat(state.locked_until) > now():
        raise DomainError('SECURITY_LOCKED', '安全锁定中，15分钟冷却后可重新验证；仍可查询、撤销未执行授权或求助', 423)


def failure(db, owner, code):
    """Called after the rejected business transaction rolled back."""
    with atomic_write(db):
        state = db.get(SecurityState, owner)
        if state is None:
            state = SecurityState(owner_id=owner, failures=0)
            db.add(state)
        if state.locked_until and datetime.fromisoformat(state.locked_until) <= now():
            state.failures, state.locked_until = 0, None
        state.failures += 1
        if state.failures >= FAILURE_LIMIT:
            state.locked_until = (now() + timedelta(minutes=LOCK_MINUTES)).isoformat()
        audit(db, owner, 'security_rejected', code)


def daily_total(db, owner):
    """Real Shanghai security day, across ALL owned accounts; never demo clock.

    Count posted transfer debits by actual creation time, including own transfers.
    Refunds/credits never reduce this gross security counter.
    """
    start = now().astimezone(SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return db.scalar(select(func.coalesce(func.sum(Transaction.amount_minor), 0)).where(
        Transaction.owner_id == owner, Transaction.direction == 'debit', Transaction.status == 'posted',
        Transaction.type.in_(['internal_transfer', 'external_transfer']),
        Transaction.created_at_utc >= start.astimezone(timezone.utc).isoformat(timespec='seconds'),
        Transaction.created_at_utc < end.astimezone(timezone.utc).isoformat(timespec='seconds'))) or 0


def policy(db, owner, action, amount=0):
    total = daily_total(db, owner) if action in {'transfer', 'schedule'} else None
    red = (action in {'buy', 'replace_card', 'credit_limit', 'loss', 'unloss', 'unfreeze', 'online_unblock', 'limit_increase'} or
           (total is not None and total + amount > 100000))
    return {'level': 'red' if red else 'yellow', 'required': '模拟短信验证码＋页面确认' if red else '明确页面确认',
            'action': action, 'amount_minor': amount, 'daily_total_minor': total,
            'including_this_minor': total + amount if total is not None else None,
            'policy_version': POLICY_VERSION, 'simulated_authentication': True}


def resolve(db, session_id, kind, rid):
    from app.domain.operations import _session, _now
    session, _ = _session(db, session_id, _now())
    from app.data.models import Proposal
    from app.data.wealth_models import WealthOrder
    from app.data.card_models import CardRequest
    from app.data.birthday_models import BirthdayRequest
    from app.data.subscription_models import SubscriptionRequest
    from app.data.scheduled_models import ScheduledTransfer
    from app.data.collection_models import CollectionRequest
    from app.data.servicing_models import ServiceRequest
    models = {'service':ServiceRequest,'proposal': Proposal, 'wealth': WealthOrder, 'card': CardRequest,
              'birthday': BirthdayRequest, 'subscription': SubscriptionRequest,
              'schedule': ScheduledTransfer, 'collection': CollectionRequest}
    row = db.get(models[kind], rid) if kind in models else None
    if row is None or row.owner_id != session.owner_id or row.session_id != session.id or row.dataset_version != session.dataset_version:
        raise DomainError('RESOURCE_NOT_FOUND', '操作单不存在', 404)
    raw = getattr(row, 'terms_json', None) or getattr(row, 'params_json', '{}')
    terms = json.loads(raw)
    if kind == 'collection':
        terms = {'title': row.title, 'destination_account_id': row.destination_account_id,
                 'total_minor': row.total_minor, 'request_digest': row.request_digest}
    action = row.kind if hasattr(row, 'kind') else kind
    if kind == 'proposal':
        action = terms['action']
    if kind == 'card' and action == 'limit':
        action = 'limit_increase' if terms['limit_minor'] > terms['controls']['limit_minor'] else 'limit_decrease'
    if kind == 'schedule':
        action = 'schedule'
    amount = terms.get('amount_minor', terms.get('budget_minor', terms.get('total_minor', 0)))
    task = db.get(AgentTask, getattr(row, 'agent_task_id', None)) if getattr(row, 'agent_task_id', None) else None
    if kind == 'proposal':
        task = db.scalar(select(AgentTask).where(AgentTask.proposal_id == rid))
    binding = digest({'owner': session.owner_id, 'session': session.id, 'resource': kind + ':' + rid,
        'terms': terms, 'snapshot': getattr(row, 'snapshot_json', None), 'dataset': session.dataset_version,
        'execute_at': getattr(row, 'execute_at_utc', None), 'target_binding': getattr(row, 'target_binding_json', None),
        'expires_at': row.expires_at_utc,
        'plan_version': task.plan_version if task else 0, 'policy': POLICY_VERSION})
    return session, row, terms, binding, policy(db, session.owner_id, action, amount)


def describe(db, session_id, kind, rid):
    session, row, terms, binding, decision = resolve(db, session_id, kind, rid)
    display_terms = dict(terms)
    if kind == 'proposal':
        from app.domain.operations import proposal_view
        display_terms['核对摘要'] = proposal_view(db, row)['summary']
    elif kind == 'schedule':
        display_terms.update(execute_at_utc=row.execute_at_utc, timezone=row.timezone,
                             target_binding=json.loads(row.target_binding_json))
    elif kind == 'collection':
        from app.data.collection_models import CollectionMember
        display_terms['members'] = [{'name': member.display_name, 'share_minor': member.share_minor}
            for member in db.scalars(select(CollectionMember).where(CollectionMember.request_id == row.id)
                                     .order_by(CollectionMember.ordinal))]
    return {**decision, 'resource_kind': kind, 'resource_id': rid, 'status': row.status,
            'expires_at': row.expires_at_utc, 'terms': display_terms}


def assert_pending(row):
    if row.status != 'pending_confirmation' or datetime.fromisoformat(row.expires_at_utc) <= now():
        raise DomainError('INVALID_ORDER_STATE', '操作单已失效，请重新准备', 409)


def start_challenge(db, session_id, kind, rid):
    with atomic_write(db):
        session, row, terms, binding, decision = resolve(db, session_id, kind, rid)
        assert_unlocked(db, session.owner_id)
        assert_pending(row)
        # One live challenge per binding. Reissuing invalidates even verified proofs.
        for old in db.scalars(select(SecurityChallenge).where(SecurityChallenge.binding == binding,
                SecurityChallenge.status.in_(['pending', 'verified']))):
            old.status = 'invalidated'
        identifier, code = uuid4().hex, f'{secrets.randbelow(1000000):06d}'
        expires = (now() + timedelta(seconds=CHALLENGE_SECONDS)).isoformat()
        challenge = SecurityChallenge(id=identifier, owner_id=session.owner_id, session_id=session.id,
            binding=binding, code_hash=digest({'id': identifier, 'code': code}), status='pending',
            attempts=0, expires_at=expires)
        db.add(challenge)
        # Private OS-local delivery outbox; explicitly excluded from source archives.
        from app.config import ROOT
        directory = ROOT / 'var' / 'auth-simulator'
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (identifier + '.json')
        path.write_text(canonical({'challenge_id': identifier, 'code': code, 'expires_at': expires}), encoding='utf-8')
        path.chmod(0o600)
        audit(db, session.owner_id, 'mfa_challenge_issued', kind)
        return {'challenge_id': identifier, 'expires_at': expires, 'delivery': 'local_sms_simulator',
                'message': '模拟短信已送达，可在测试小窗或运行 scripts/auth-code.ps1 查看；不会发送真实短信。'}


def read_simulated_sms(db, session_id, kind, rid, challenge_id):
    """Read the simulator inbox for this pending operation; does not verify it."""
    with atomic_write(db):
        session, row, terms, binding, decision = resolve(db, session_id, kind, rid)
        assert_unlocked(db, session.owner_id)
        assert_pending(row)
        challenge = db.get(SecurityChallenge, challenge_id)
        if (not challenge or challenge.owner_id != session.owner_id or challenge.session_id != session.id or
                challenge.binding != binding or challenge.status != 'pending' or
                datetime.fromisoformat(challenge.expires_at) <= now()):
            raise DomainError('MFA_INVALID', '验证码已失效，请重新获取模拟短信验证码', 403)
        from app.config import ROOT
        try:
            delivered = json.loads((ROOT / 'var' / 'auth-simulator' / (challenge.id + '.json')).read_text(encoding='utf-8'))
            code = delivered['code']
            if (delivered['challenge_id'] != challenge.id or not isinstance(code, str) or
                    len(code) != 6 or not code.isascii() or not code.isdigit() or
                    not hmac.compare_digest(challenge.code_hash, digest({'id': challenge.id, 'code': code}))):
                raise ValueError('Invalid simulator delivery')
        except (OSError, ValueError, KeyError, TypeError):
            raise DomainError('SIMULATOR_UNAVAILABLE', '模拟短信读取失败，请重新获取验证码', 503) from None
        return {'challenge_id': challenge.id, 'code': code, 'expires_at': challenge.expires_at, 'simulated': True}


def verify_challenge(db, session_id, kind, rid, challenge_id, code):
    owner = None
    try:
        with atomic_write(db):
            session, row, terms, binding, decision = resolve(db, session_id, kind, rid)
            owner = session.owner_id
            assert_unlocked(db, owner)
            assert_pending(row)
            challenge = db.get(SecurityChallenge, challenge_id)
            if (not challenge or challenge.owner_id != owner or challenge.session_id != session.id or
                    challenge.binding != binding or challenge.status != 'pending' or
                    datetime.fromisoformat(challenge.expires_at) <= now()):
                raise DomainError('MFA_INVALID', '验证码无效、已使用或已过期，请重新验证', 403)
            if not hmac.compare_digest(challenge.code_hash, digest({'id': challenge.id, 'code': code})):
                raise DomainError('MFA_INVALID', '验证码错误', 403)
            challenge.status = 'verified'
            audit(db, owner, 'mfa_verified', kind)
            return {'verified': True, 'expires_at': challenge.expires_at}
    except DomainError as error:
        if owner and error.code == 'MFA_INVALID':
            failure(db, owner, error.code)
            with atomic_write(db):
                challenge = db.get(SecurityChallenge, challenge_id)
                if challenge and challenge.owner_id == owner:
                    challenge.attempts += 1
                    if challenge.attempts >= FAILURE_LIMIT:
                        challenge.status = 'locked'
        raise


def consume_proof(db, session, binding):
    proof = db.scalar(select(SecurityChallenge).where(SecurityChallenge.binding == binding,
        SecurityChallenge.session_id == session.id, SecurityChallenge.owner_id == session.owner_id,
        SecurityChallenge.status == 'verified', SecurityChallenge.expires_at > now().isoformat()))
    if not proof:
        raise DomainError('MFA_REQUIRED', '此操作为红色等级，须完成本操作单独立验证码验证后再确认', 403)
    proof.status = 'consumed'


def grant(db, owner, resource, terms, level, expires, count):
    existing = db.scalar(select(SecurityAuthorization).where(SecurityAuthorization.resource == resource))
    if existing:
        db.delete(existing)
        db.flush()
    row = SecurityAuthorization(id=uuid4().hex, owner_id=owner, resource=resource,
        digest=digest(terms), terms_json=canonical(terms), level=level, policy_version=POLICY_VERSION,
        expires_at=expires, remaining=count, status='active')
    db.add(row)
    return row


def check_grant(db, owner, resource, terms, level='yellow', consume=False):
    assert_unlocked(db, owner)
    kind, _, rid = resource.partition(':')
    if kind in {'schedule', 'birthday'}:
        from app.data.scheduled_models import ScheduledTransfer
        from app.data.birthday_models import BirthdayPlan
        business = db.get(ScheduledTransfer if kind == 'schedule' else BirthdayPlan, rid)
        if business and business.agent_task_id:
            from app.agent.control import assert_running
            assert_running(db, business.agent_task_id)
    row = db.scalar(select(SecurityAuthorization).where(SecurityAuthorization.resource == resource,
        SecurityAuthorization.owner_id == owner))
    if (not row or row.status != 'active' or row.remaining <= 0 or row.policy_version != POLICY_VERSION or
            datetime.fromisoformat(row.expires_at) <= now() or row.digest != digest(terms) or
            (level == 'red' and row.level != 'red')):
        raise DomainError('REAUTHORIZATION_REQUIRED', '后台授权失效或等级不足，已停止；请回到页面重新授权', 409)
    if consume:
        row.remaining -= 1
        if not row.remaining:
            row.status = 'consumed'
    return row


def secured(kind, id_arg):
    """Whole business operation + proof consumption share the SQLite writer lock."""
    def decorate(fn):
        @wraps(fn)
        def wrapped(db, *args, **kwargs):
            owner = None
            deferred_error = None
            try:
                with atomic_write(db):
                    session, row, terms, binding, decision = resolve(db, kwargs['session_id'], kind, kwargs[id_arg])
                    owner = session.owner_id
                    if db.info.get('chat_authorization'):
                        from app.agent.authorization import check_domain_binding
                        check_domain_binding(db,session,kind,row.id)
                    # Return existing receipts without executing or requiring a new proof.
                    if row.status in TERMINAL:
                        return fn(db, *args, **kwargs)
                    task_id = getattr(row, 'agent_task_id', None)
                    if kind == 'proposal':
                        related = db.scalar(select(AgentTask).where(AgentTask.proposal_id == row.id))
                        task_id = related.id if related else None
                    if task_id:
                        from app.agent.control import assert_running
                        assert_running(db, task_id)
                    withdrawal = (kind == 'subscription' and row.kind == 'revoke') or (
                        kind == 'birthday' and row.kind in {'cancel', 'cancel_item'})
                    if not withdrawal:
                        assert_unlocked(db, owner)
                    delegated = db.info.get('scheduled_security_grant')
                    if kind == 'proposal' and delegated:
                        check_grant(db, owner, 'schedule:' + delegated['id'], delegated['terms'], decision['level'])
                        if terms != delegated['params']:
                            raise DomainError('REAUTHORIZATION_REQUIRED', '执行参数超出预约授权', 409)
                    elif decision['level'] == 'red':
                        consume_proof(db, session, binding)
                    audit(db, owner, 'policy_' + decision['level'], decision['action'])
                    result = fn(db, *args, **kwargs)
                    if isinstance(result, DomainError):
                        # Some proposal validations deliberately persist a
                        # terminal failed/invalidated state without any debit.
                        deferred_error = result
                    elif kind == 'schedule':
                        grant(db, owner, 'schedule:' + row.id, schedule_terms(row), decision['level'],
                            (datetime.fromisoformat(row.execute_at_utc) + timedelta(days=1)).isoformat(), 1)
                    elif kind == 'birthday' and row.kind not in {'cancel', 'cancel_item'}:
                        from app.data.birthday_models import BirthdayPlan
                        plan = db.get(BirthdayPlan, row.plan_id)
                        grant(db, owner, 'birthday:' + plan.id, birthday_terms(plan), decision['level'],
                              (now() + timedelta(days=40)).isoformat(), 2)
                    if not deferred_error and delegated and kind == 'proposal':
                        check_grant(db, owner, 'schedule:' + delegated['id'], delegated['terms'], decision['level'], consume=True)
                    state = db.get(SecurityState, owner)
                    if not deferred_error and state and (not state.locked_until or datetime.fromisoformat(state.locked_until) <= now()):
                        state.failures, state.locked_until = 0, None
                if deferred_error:
                    raise deferred_error
                return result
            except DomainError as error:
                if owner and error.code in {'CONFIRMATION_INVALID', 'INVALID_CONFIRMATION_TOKEN', 'MFA_INVALID'}:
                    failure(db, owner, error.code)
                raise
        return wrapped
    return decorate


def preparing(fn):
    """Check independent task control under the same lock as draft creation."""
    @wraps(fn)
    def wrapped(db, *args, **kwargs):
        with atomic_write(db):
            if kwargs.get('session_id'):
                from app.domain.servicing import ensure_no_pending
                ensure_no_pending(db,kwargs['session_id'],kwargs.get('agent_task_id') if fn.__module__=='app.domain.servicing' else None)
            if kwargs.get('agent_task_id'):
                from app.agent.control import assert_running
                assert_running(db, kwargs['agent_task_id'])
            return fn(db, *args, **kwargs)
    return wrapped


def schedule_terms(row):
    return {'params': json.loads(row.params_json), 'binding': row.target_binding_json,
            'dataset': row.dataset_version, 'execute_at': row.execute_at_utc}


def birthday_terms(plan):
    return {'terms_digest': plan.terms_digest, 'dataset': plan.dataset_version,
            'authorization_request': plan.authorization_request_id, 'budget_minor': plan.budget_minor}


def subscription_terms(auth):
    return {'owner': auth.owner_id, 'dataset': auth.dataset_version, 'account': auth.account_id,
            'merchant': auth.merchant, 'amount_minor': auth.amount_minor, 'period': auth.period,
            'charge_day': auth.charge_day, 'timezone': auth.timezone, **({'payment_card_id':auth.payment_card_id} if auth.payment_card_id else {})}


def assert_subscription_access(db, session_id):
    session = db.get(DemoSession, session_id)
    if not session:
        raise DomainError('SESSION_EXPIRED', '请重新选择用户', 401)
    row = db.scalar(select(SecurityAuthorization).where(
        SecurityAuthorization.resource == 'subscription-query:' + session.id,
        SecurityAuthorization.owner_id == session.owner_id))
    if not row or row.status != 'active' or row.digest != digest({'dataset': session.dataset_version}) or datetime.fromisoformat(row.expires_at) <= now():
        raise DomainError('SUBSCRIPTION_CONFIRMATION_REQUIRED', '订阅查询为黄色操作，请先在订阅面板明确确认查询范围', 403)


def confirm_subscription_access(db, session_id):
    with atomic_write(db):
        session = db.get(DemoSession, session_id)
        if not session:
            raise DomainError('SESSION_EXPIRED', '请重新选择用户', 401)
        grant(db, session.owner_id, 'subscription-query:' + session.id, {'dataset': session.dataset_version},
              'yellow', (now() + timedelta(minutes=5)).isoformat(), 1)
        audit(db, session.owner_id, 'subscription_query_confirmed')
        return {'confirmed': True, 'expires_in_seconds': 300}
