"""Durable checkpoints and a simulated operator queue; never grants authority."""
import json
from uuid import uuid4
from app.data.models import AgentTask
from app.data.security_models import TaskControl
from app.data.atomic import atomic_write
from app.domain.errors import DomainError
from app.domain.security import audit, now


def assert_running(db, task_id):
    task = db.get(AgentTask, task_id)
    if not task or task.stop_requested or state(db, task_id).get('state') in {'paused', 'handoff', 'awaiting_reverification', 'terminated'}:
        raise DomainError('TASK_PAUSED', '任务已暂停或正在人工接管，未执行新的业务步骤', 409)


def state(db, task_id):
    record = db.get(TaskControl, task_id)
    return json.loads(record.data_json) if record else {}


def begin_request(db, task_id):
    """Invalidate late composite-model responses when a new message arrives."""
    with atomic_write(db):
        assert_running(db, task_id)
        task = db.get(AgentTask, task_id)
        record = db.get(TaskControl, task_id)
        control = state(db, task_id)
        request_id = uuid4().hex
        control.update(request_id=request_id)
        control.setdefault('state', 'running')
        if record is None:
            record = TaskControl(task_id=task_id, owner_id=task.owner_id, state=control['state'], data_json='{}')
            db.add(record)
        record.data_json = json.dumps(control, ensure_ascii=False)
        return request_id


def assert_request(db, task_id, request_id):
    assert_running(db, task_id)
    if state(db, task_id).get('request_id') != request_id:
        raise DomainError('STALE_PLAN_RESPONSE', '已有更新的消息或任务控制，已丢弃迟到的规划结果', 409)


def change(db, task, action, reason='', operator=False):
    with atomic_write(db):
        task = db.get(AgentTask, task.id)
        context = json.loads(task.context_json)
        record = db.get(TaskControl, task.id)
        control = json.loads(record.data_json) if record else {}
        if action == 'pause' and control.get('state') == 'handoff' and not operator:
            raise DomainError('OPERATOR_REQUIRED', '任务正在人工接管，不能用暂停绕过坐席交还流程', 403)
        if action in {'pause', 'handoff', 'awaiting_reverification'}:
            control['request_id'] = uuid4().hex
            if control.get('state') not in {'paused', 'handoff', 'awaiting_reverification'}:
                control['previous_status'] = 'running_readonly' if context.get('unified_plan') else (
                    'gathering_info' if task.status in {'paused', 'handoff', 'awaiting_reverification'} else task.status)
            control.update(state='paused' if action == 'pause' else action, reason=reason,
                           checkpoint_version=task.plan_version, at=now().isoformat())
            task.status = control['state']
        elif action == 'resume':
            if task.stop_requested:
                raise DomainError('INVALID_TASK_STATE', '已终止任务不能恢复，请创建新任务', 409)
            if control.get('state') == 'handoff' and not operator:
                raise DomainError('OPERATOR_REQUIRED', '请由模拟坐席结束接管', 403)
            if control.get('checkpoint_version', task.plan_version) != task.plan_version:
                raise DomainError('PLAN_CHANGED', '计划版本已变化，请核对后新建任务；未重放旧步骤', 409)
            was_handoff = control.get('state') == 'handoff'
            control.update(state='running', resolution=reason)
            task.status = control.get('previous_status', 'gathering_info')
            # Operator resumes processing only. Expired/user-bound proofs are
            # never minted or bypassed by an operator action.
            if was_handoff:
                control['requires_user_authorization'] = True
        elif action == 'terminate':
            task.stop_requested = True
            task.status = 'cancelled'
            control.update(state='terminated', resolution=reason)
        elif action == 'suggest':
            control['suggestion'] = reason
        else:
            raise DomainError('INVALID_INPUT', '未知控制动作')
        task.updated_at_utc = now().isoformat()
        context['control'] = control
        if record is None:
            record = TaskControl(task_id=task.id, owner_id=task.owner_id, state=control['state'], data_json='{}')
            db.add(record)
        record.state = control['state']
        record.data_json = json.dumps(control, ensure_ascii=False)
        task.context_json = json.dumps(context, ensure_ascii=False)
        audit(db, task.owner_id, ('operator_' if operator else 'user_') + action, 'checkpoint:' + str(task.plan_version))
        return control
