"""Registry verifier. Certificates attest to encoded rules, not legal compliance."""
from dataclasses import dataclass, field, asdict
from typing import Callable
from uuid import uuid4
import hashlib
import json
from sqlalchemy import select
from app.clock import now
from app.data.models import Account, AgentMessage, AuditEvent
from app.neuro_models import VerificationRecord
from app.agent.guardrails import has_condition
from app.domain.errors import DomainError

RULESET_VERSION = 'finagent-ns-1.1'
@dataclass
class Violation:
    rule_id: str
    field: str
    observed: object
    expected: object
    hint: str
@dataclass
class VerifyResult:
    approved: bool
    certificate: dict | None
    violations: list = field(default_factory=list)
    would_block: list = field(default_factory=list)
    def as_dict(self):
        return asdict(self)
@dataclass
class Rule:
    rule_id: str
    description: str
    severity: str
    mode: str
    check: Callable
RULES = {}
def register(rule_id, description, severity='error', mode='observe'):
    if mode not in {'enforce', 'observe'}:
        raise ValueError('invalid rule mode')
    def decorate(fn):
        RULES[rule_id] = Rule(rule_id, description, severity, mode, fn)
        return fn
    return decorate

@dataclass
class Clarification:
    rule_id: str
    field: str
    observed: object
    expected: object
    message: str
    suggested_value: object = None
CLARIFICATION_RULES = {}
def register_clarification(rule_id, description):
    def decorate(fn):
        CLARIFICATION_RULES[rule_id] = Rule(rule_id,description,'info','clarify',fn)
        return fn
    return decorate
@register_clarification('R10','周期字段完整性')
def recurrence_complete(action, params, context):
    r=params.get('recurrence')
    if action=='plan' and context.get('recurrence_grounded') and r and r.get('freq')=='monthly' and r.get('day') is None:
        rejected=context.get('default_rejected',False)
        message='请告诉我每月几号转账（1至31号）。' if rejected else '每月转账需要固定日期，设置为每月1号可以吗？'
        return Clarification('R10','recurrence.day',None,'1至31号的明确日期',message,
                             None if rejected else 1)
def clarification_rules(action, params, context):
    return [result for rule in CLARIFICATION_RULES.values()
            if (result:=rule.check(action,params,context)) is not None]
def record_clarification(db, owner_id, items):
    payload={'approved':False,'certificate':None,'violations':[],'would_block':[],
             'clarifications':[asdict(item) for item in items],'ruleset_version':RULESET_VERSION}
    row=VerificationRecord(id=uuid4().hex,owner_id=owner_id,proposal_id=None,phase='plan_clarification',
        payload_json=json.dumps(payload,ensure_ascii=False),created_at=now().isoformat())
    db.add(row)
    for item in items:
        db.add(AuditEvent(id=uuid4().hex,owner_id=owner_id,proposal_id=None,event='clarification_requested',
                         detail_code=item.rule_id+':'+row.id,created_at_utc=now().isoformat(timespec='microseconds')))
    db.flush()
    return row.id

@register('R1', '条件完整性', mode='enforce')
def condition_rule(action, params, context):
    if action.startswith('prepare_') and has_condition(context.get('input', '')) and not params.get('condition'):
        return Violation('R1', 'condition', None, '完整结构化条件', '条件未能完整解析，不能生成无条件操作单，请重新说明条件。')
@register('R2', '余额充足', mode='enforce')
def balance_rule(action, params, context):
    if action != 'prepare_transfer':
        return
    db, owner = context.get('db'), context.get('owner_id')
    if db is None:
        return
    account = db.scalar(select(Account).where(Account.id == params.get('source_account_id'), Account.owner_id == owner))
    if account and account.balance_minor < params.get('amount_minor', 0):
        return Violation('R2', 'amount_minor', params['amount_minor'], account.balance_minor, '账户余额不足，请减少金额或选择其他账户。')
@register('R3', '资源快照一致', mode='enforce')
def snapshot_rule(action, params, context):
    proposal = context.get('proposal')
    if proposal:
        from app.domain.operations import _check_snapshot
        snapshot = json.loads(proposal.snapshot_json)
        if not _check_snapshot(context['db'], context['owner_id'], proposal, params, snapshot):
            return Violation('R3', 'snapshot', '资源已变化', '准备时资源版本', '账户或卡片状态已变化，请重新准备。')
@register('R4', '周期指令只登记长期计划', mode='observe')
def recurrence_rule(action, params, context):
    if context.get('recurrence') and context.get('destination') != 'standing_orders':
        return Violation('R4', 'recurrence', context['recurrence'], 'standing_orders', '周期指令应先登记并确认，不能作为一次性写操作。')

def verify(action, params, context):
    passed, violations, observed = [], [], []
    for rule in tuple(RULES.values()):
        violation = rule.check(action, params, context)
        if violation:
            (violations if rule.mode == 'enforce' else observed).append(violation)
        else:
            passed.append(rule.rule_id)
    digest = hashlib.sha256(json.dumps({'action':action, 'params':params}, ensure_ascii=False,
                                    sort_keys=True, default=str).encode()).hexdigest()
    certificate = None if violations else {'ruleset_version':RULESET_VERSION,
        'rules_passed':passed, 'clock_now':now().isoformat(), 'action_digest':digest,
        'rule_modes':{r.rule_id:r.mode for r in RULES.values()}}
    return VerifyResult(not violations, certificate, violations, observed)

def record(db, owner_id, proposal_id, phase, result):
    row = VerificationRecord(id=uuid4().hex, owner_id=owner_id, proposal_id=proposal_id,
        phase=phase, payload_json=json.dumps(result.as_dict(), ensure_ascii=False), created_at=now().isoformat())
    db.add(row)
    db.add(AuditEvent(id=uuid4().hex, owner_id=owner_id, proposal_id=proposal_id,
        event='verified' if result.approved else 'verification_rejected', detail_code=row.id,
        created_at_utc=now().isoformat(timespec='microseconds')))
    for violation in result.would_block:
        db.add(AuditEvent(id=uuid4().hex, owner_id=owner_id, proposal_id=proposal_id,
            event='would_block', detail_code=violation.rule_id + ':' + row.id,
            created_at_utc=now().isoformat(timespec='microseconds')))
    db.flush()
    return row.id

def boundary(db, owner_id, action, params, proposal=None, agent_task_id=None, phase='prepare'):
    text = ''
    if agent_task_id:
        messages = db.scalars(select(AgentMessage).where(AgentMessage.task_id == agent_task_id,
                 AgentMessage.role == 'user').order_by(AgentMessage.ordinal)).all()
        text = ' '.join(m.content for m in messages)
    if proposal and params.get('condition'):
        text = '如果'
    result = verify(action, params, {'db':db, 'owner_id':owner_id, 'proposal':proposal,
                                   'input':text, 'emotion':None})
    record(db, owner_id, proposal.id if proposal else None, phase, result)
    if not result.approved:
        violation = result.violations[0]
        code = {'R2':'INSUFFICIENT_FUNDS', 'R3':'STALE_RESOURCE'}.get(violation.rule_id, 'INVALID_CONDITION')
        return DomainError(code, violation.hint, 400 if code == 'INSUFFICIENT_FUNDS' else 409)
    return None
