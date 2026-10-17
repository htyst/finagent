import hmac
import json
from typing import Literal
from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.config import ROOT, is_demo_admin_request
from app.data.models import AgentTask, DemoSession
from app.domain.errors import DomainError
from app.agent.control import change, state


class ControlPayload(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['pause', 'handoff', 'resume', 'terminate', 'suggest']
    reason: str = Field(default='', max_length=500)


def operator(request):
    path = ROOT / 'var' / 'operator.key'
    supplied = request.headers.get('x-finagent-operator', '')
    if not is_demo_admin_request(request.client.host) or not path.exists() or not supplied or not hmac.compare_digest(path.read_text().strip(), supplied):
        raise DomainError('OPERATOR_REQUIRED', '仅持本机模拟坐席凭据可查看接管队列；坐席无用户业务授权权限', 403)


def register_routes(app, get_db, require_demo_session, protect, envelope):
    @app.post('/api/tasks/{task_id}/control')
    def control(task_id: str, payload: ControlPayload, request: Request, session: DemoSession = Depends(require_demo_session), db: Session = Depends(get_db)):
        from app.agent.service import _task, view
        protect(request, session)
        task = _task(db, session, task_id)
        if payload.action in {'terminate', 'suggest'}:
            raise DomainError('INVALID_INPUT', '请用取消任务或模拟坐席处理')
        change(db, task, payload.action, payload.reason)
        if payload.action == 'resume':
            from app.agent.dynamic_runtime import resume_control
            return envelope(resume_control(db, session, task_id, view(db, task)))
        return envelope(view(db, task))

    @app.get('/api/operator/queue')
    def queue(request: Request, db: Session = Depends(get_db)):
        operator(request)
        return envelope([{'id': task.id, 'owner_id': task.owner_id, 'plan_version': task.plan_version,
                          'control': state(db, task.id)}
                         for task in db.scalars(select(AgentTask))
                         if state(db, task.id).get('state') == 'handoff'])

    @app.post('/api/operator/tasks/{task_id}')
    def resolve(task_id: str, payload: ControlPayload, request: Request, session: DemoSession = Depends(require_demo_session), db: Session = Depends(get_db)):
        operator(request)
        protect(request, session)
        task = db.get(AgentTask, task_id)
        if not task:
            raise DomainError('RESOURCE_NOT_FOUND', '任务不存在', 404)
        if payload.action not in {'resume', 'terminate', 'suggest'}:
            raise DomainError('INVALID_INPUT', '坐席只能建议、终止或恢复检查点')
        if state(db, task.id).get('state') != 'handoff':
            raise DomainError('INVALID_TASK_STATE', '该任务不在人工接管队列', 409)
        change(db, task, payload.action, payload.reason, operator=True)
        return envelope({'task_id': task.id, 'control': json.loads(task.context_json)['control'],
                         'message': '坐席处理已记录；任何业务仍须用户确认或独立强验证'})
