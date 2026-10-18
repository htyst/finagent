"""Owner/session scoped decision evidence, without private dialogue or proofs."""
import hashlib
import json
from sqlalchemy import select
from app.data.models import AgentStep, AuditEvent
from app.domain.security import POLICY_VERSION
from app.agent.privacy import model_payload


def task_audit(db, session, task_id):
    from app.agent.service import _task, view
    task = _task(db, session, task_id)
    current = view(db, task)
    steps = db.scalars(select(AgentStep).where(AgentStep.task_id == task.id)
                       .order_by(AgentStep.plan_version, AgentStep.ordinal)).all()
    proposal_ids = {s.result_ref for s in steps if s.tool_name in {'prepare_transfer', 'prepare_card_action'} and s.result_ref}
    if task.proposal_id: proposal_ids.add(task.proposal_id)
    events = db.scalars(select(AuditEvent).where(AuditEvent.owner_id == task.owner_id,
        AuditEvent.proposal_id.in_(proposal_ids)).order_by(AuditEvent.created_at_utc, AuditEvent.id)).all() if proposal_ids else []
    context = json.loads(task.context_json)
    resources = [{"kind": key, "id": current[key].get('id'), "status": current[key].get('status')}
                 for key in ('proposal', 'wealth_order', 'card_request', 'subscription_request', 'birthday_request', 'schedule', 'collection')
                 if isinstance(current.get(key), dict)]
    return model_payload({'trace_id': task.id, 'scope': 'agent_task_and_linked_business_resources',
        'decision': {'mode': task.mode, 'intent': task.intent, 'status': current['status'],
            'plan_version': task.plan_version, 'policy_version': POLICY_VERSION,
            'goals': json.loads(task.requested_outcomes_json),
            'violations': context.get('plan_violations', []), 'control': current['control']},
        'dialogue': {key: context.get('dialogue_memory', {}).get(key) for key in
            ('version', 'last_behavior', 'missing_fields', 'fields', 'conditions', 'prohibitions', 'time_requirements', 'last_budget')},
        'resources': resources,
        'execution_graph':current.get('execution_graph'),
        'correlation_events':context.get('dynamic_runtime',{}).get('events',[]),
        'authorization_links':[dict(task_id=task.id,plan_version=task.plan_version,action_id=action,resource_ref=ref) for action,ref in context.get('authorization_receipts',{}).items()],
        'steps': [{'ordinal': s.ordinal, 'plan_version': s.plan_version, 'tool': s.tool_name,
            'status': s.status, 'result_ref': s.result_ref,
            'parameters_sha256': hashlib.sha256(s.args_json.encode()).hexdigest(),
            'result_sha256': hashlib.sha256(s.result_json.encode()).hexdigest(),
            'created_at_utc': s.created_at_utc} for s in steps],
        'proposal_events': [{'id': e.id, 'event': e.event, 'code': e.detail_code,
            'proposal_id': e.proposal_id, 'created_at_utc': e.created_at_utc} for e in events],
        'limitations': ['模型调用、目标、节点和固定单回执按任务关联；领域及worker保留资源ID，未接入第三方端到端trace协议',
                        '不导出模型内部思维链、原始聊天、认证材料和收货资料']})
