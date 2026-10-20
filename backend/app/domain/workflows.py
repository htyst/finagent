"""One orchestration index and bounded, durable read-only follow-ups.

Financial submission remains in the existing authorized domain handlers. The
queue can consume their receipts, but never creates a token or executes payment.
"""
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from threading import Event, Thread
from uuid import uuid4
from hashlib import sha256

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from sqlalchemy import select, or_, and_, func

from app.data.atomic import atomic_write
from app.data.models import AgentTask, DemoSession, DemoSetting, Account
from app.data.workflow_models import WorkflowJob, WorkflowReview, WorkflowReviewRun
from app.domain.errors import DomainError

LOGGER = logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc)


def stamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds')


def parse_time(value):
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError):
        raise DomainError('INVALID_REVIEW_TIME', '复查时间须包含时区', 422)


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    task_id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=100)
    run_at: str
    interval_days: StrictInt = Field(default=0, ge=0, le=90)
    end_at: str
    target_minor: StrictInt | None = Field(default=None, gt=0, le=100000000000)
    consent: bool
    request_id: str = Field(min_length=8, max_length=100)

    @field_validator('title')
    @classmethod
    def title_not_blank(cls, value):
        if not value.strip():
            raise ValueError('复查名称不能为空')
        return value.strip()


def create_review(db, session, payload, *, instant=None):
    from app.agent.service import _task
    instant = instant or now()
    due, end = parse_time(payload.run_at), parse_time(payload.end_at)
    if not payload.consent:
        raise DomainError('REVIEW_CONSENT_REQUIRED', '请明确同意只读资金复查范围及期限', 403)
    # A stable client request identifier makes lost-response retries idempotent.
    identifier = 'review-' + sha256((session.id + ':' + payload.request_id).encode()).hexdigest()[:32]
    terms = payload.model_dump(exclude={'request_id'})
    with atomic_write(db):
        _task(db, session, payload.task_id)
        old = db.get(WorkflowReview, identifier)
        if old:
            if json.loads(old.consent_json) != terms:
                raise DomainError('REVIEW_REQUEST_CHANGED', '复查条款已变化，请重新核对', 409)
            return review_view(old)
        from app.agent.control import assert_running
        assert_running(db, payload.task_id)
        if not instant < due <= end <= instant + timedelta(days=366):
            raise DomainError('INVALID_REVIEW_TIME', '请选择未来时间，复查期限最多一年', 422)
        active = db.scalar(select(func.count()).select_from(WorkflowReview).where(
            WorkflowReview.owner_id == session.owner_id,
            WorkflowReview.dataset_version == session.dataset_version,
            WorkflowReview.status.in_(['scheduled', 'paused'])))
        if active >= 32:
            raise DomainError('WORKFLOW_REVIEW_LIMIT', '已有较多复查计划，请先完成或取消未执行计划', 409)
        row = WorkflowReview(id=identifier, task_id=payload.task_id, owner_id=session.owner_id,
            dataset_version=session.dataset_version, title=payload.title.strip(), status='scheduled',
            next_run_at_utc=stamp(due), end_at_utc=stamp(end), interval_days=payload.interval_days,
            target_minor=payload.target_minor, version=1, consent_json=json.dumps(terms, ensure_ascii=False),
            latest_result_json='{}', created_at_utc=stamp(instant), updated_at_utc=stamp(instant))
        db.add(row)
        task=db.get(AgentTask,payload.task_id)
        context=json.loads(task.context_json)
        if context.get('workflow_review_draft',{}).get('status')=='draft':
            context['workflow_review_draft'].update(status='confirmed',review_id=identifier)
            runtime=context.get('dynamic_runtime') or {}
            for node in runtime.get('nodes',[]):
                if node['id']==context['workflow_review_draft'].get('node_id') and node['name']=='prepare_review_plan' and node['status']=='succeeded':
                    node['domain_status']='scheduled'
                    for goal in runtime.get('goals',[]):
                        if goal['id'] in node.get('goal_ids',[]):goal.update(needs_input=False,answer_observed=True)
            task.context_json=json.dumps(context,ensure_ascii=False)
        from app.domain.security import audit
        audit(db, session.owner_id, 'workflow_review_created', row.id)
        db.flush()
        return review_view(row)


def review_view(row):
    return {key: getattr(row, key) for key in ('id', 'task_id', 'title', 'status', 'version',
        'next_run_at_utc', 'end_at_utc', 'interval_days', 'target_minor', 'error_code')} | {
        'result': json.loads(row.latest_result_json),
        'scope': '本人人民币账户可用资金；不读取订阅合同，不转账、不锁款、不自动投资。'}


def change_review(db, session, identifier, action, expected_version):
    with atomic_write(db):
        row = db.get(WorkflowReview, identifier)
        if not row or row.owner_id != session.owner_id or row.dataset_version != session.dataset_version:
            raise DomainError('RESOURCE_NOT_FOUND', '复查计划不存在', 404)
        if row.version != expected_version:
            raise DomainError('STALE_RESOURCE', '计划已变化，请刷新后重试', 409)
        if row.status in {'completed', 'cancelled', 'blocked'}:
            raise DomainError('INVALID_PLAN_STATE', '该复查计划已结束，不能恢复', 409)
        if action == 'resume' and now() > parse_time(row.end_at_utc):
            raise DomainError('REVIEW_EXPIRED', '复查授权期限已过，请重新设置', 409)
        row.status = {'pause': 'paused', 'resume': 'scheduled', 'cancel': 'cancelled'}[action]
        row.version += 1
        row.updated_at_utc = stamp(now())
        from app.domain.security import audit
        audit(db, session.owner_id, 'workflow_review_' + action, row.id + ':v' + str(row.version))
        return review_view(row)


def enqueue_receipt(db, task, kind, rid, action_id, *, cancelled=False):
    """Called inside the transaction recording the authorization receipt."""
    runtime = json.loads(task.context_json).get('dynamic_runtime') or {}
    wait = runtime.get('wait') or {}
    if wait.get('kind') != 'authorization' or wait.get('action_id') != action_id:
        return None
    if wait.get('resource_kind') != kind or wait.get('resource_id') != rid:
        return None
    key = 'receipt:' + task.id + ':' + action_id + ':' + str(cancelled)
    old = db.scalar(select(WorkflowJob).where(WorkflowJob.event_key == key))
    if old:
        return old.id
    instant = stamp(now())
    row = WorkflowJob(id=uuid4().hex, event_key=key, task_id=task.id,
        owner_id=task.owner_id, session_id=task.session_id, dataset_version=task.dataset_version,
        payload_json=json.dumps(dict(runtime_id=runtime['id'], request_id=runtime.get('request_id'),
            kind=kind, resource_id=rid, action_id=action_id, cancelled=cancelled,
            plan_version=task.plan_version)), status='pending', attempts=0,
        next_run_at_utc=instant, created_at_utc=instant, updated_at_utc=instant)
    db.add(row)
    db.flush()
    return row.id


def claim_job(factory, identifier=None, *, instant=None):
    instant = instant or now()
    with factory() as db, atomic_write(db):
        query = select(WorkflowJob).where(or_(
            and_(WorkflowJob.status == 'pending', WorkflowJob.next_run_at_utc <= stamp(instant)),
            and_(WorkflowJob.status == 'running', WorkflowJob.lease_until_utc <= stamp(instant))))
        if identifier:
            query = query.where(WorkflowJob.id == identifier)
        row = db.scalar(query.order_by(WorkflowJob.created_at_utc).limit(1))
        if not row:
            return None
        if row.attempts >= 3:
            row.status, row.error_code = 'blocked', 'FOLLOWUP_RETRY_EXHAUSTED'
            return None
        recovering = row.status == 'running'
        row.status, row.attempts = 'running', row.attempts + 1
        row.lease_token = uuid4().hex
        # Greater than the bounded model stage; reclaim invalidates the old request.
        row.lease_until_utc = stamp(instant + timedelta(minutes=5))
        row.updated_at_utc = stamp(instant)
        return row.id, row.lease_token, recovering


def discover_receipts(factory, *, limit=50):
    """Repair the crash window between a domain commit and its HTTP acknowledgement.

    Only the exact still-waiting owned resource is inspected. A real receipt is
    required; no model assertion, new confirmation or financial executor is used.
    """
    from app.agent.service import view
    from app.agent.authorization import RESOURCES
    from app.domain.operations import _session
    with factory() as db:
        ids = list(db.scalars(select(AgentTask.id).where(AgentTask.stop_requested == 0,
            func.json_extract(AgentTask.context_json, '$.dynamic_runtime.wait.kind') == 'authorization')
            .order_by(AgentTask.updated_at_utc).limit(limit)))
    repaired = 0
    for tid in ids:
        with factory() as db, atomic_write(db):
            task = db.get(AgentTask, tid)
            ctx = json.loads(task.context_json)
            runtime = ctx.get('dynamic_runtime') or {}
            wait = runtime.get('wait') or {}
            if wait.get('kind') != 'authorization' or wait.get('plan_version') != task.plan_version:
                continue
            try:
                session, _ = _session(db, task.session_id, now())
            except DomainError:
                continue
            kind, rid, action = wait.get('resource_kind'), wait.get('resource_id'), wait.get('action_id')
            if kind not in RESOURCES or not action or task.owner_id != session.owner_id:
                continue
            record = view(db, task).get(RESOURCES[kind][0])
            if not record or record.get('id') != rid:
                continue
            cancelled = record.get('status') in {'cancelled', 'expired', 'invalidated'}
            if not record.get('receipt') and not cancelled:
                continue
            key = 'receipt:' + task.id + ':' + action + ':' + str(cancelled)
            if db.scalar(select(WorkflowJob.id).where(WorkflowJob.event_key == key)):
                continue
            if not cancelled:
                ctx = json.loads(task.context_json)
                ctx.setdefault('authorization_receipts', {})[action] = {'kind': kind, 'resource_id': rid}
                task.context_json = json.dumps(ctx, ensure_ascii=False)
            if enqueue_receipt(db, task, kind, rid, action, cancelled=cancelled):
                repaired += 1
    return repaired


def run_job(factory, identifier=None, *, instant=None):
    claim = claim_job(factory, identifier, instant=instant)
    if not claim:
        return False
    jid, token, recovering = claim
    status, code = 'completed', None
    try:
        from app.agent import dynamic_runtime as rt
        from app.agent.control import assert_running
        from app.agent.service import _task, view
        from app.domain.operations import _session
        with factory() as db:
            row = db.get(WorkflowJob, jid)
            payload = json.loads(row.payload_json)
            session, _ = _session(db, row.session_id, now())
            task = _task(db, session, row.task_id)
            if task.owner_id != row.owner_id or task.dataset_version != row.dataset_version:
                raise DomainError('STALE_RESOURCE', '任务归属已变化', 409)
            r = rt.get(task) or {}
            if r.get('id') != payload['runtime_id'] or r.get('request_id') != payload['request_id'] or task.plan_version != payload['plan_version']:
                status, code = 'cancelled', 'STALE_FOLLOWUP'
            else:
                assert_running(db, task.id)
                wait = r.get('wait') or {}
                if wait.get('action_id') == payload['action_id']:
                    rt.after_authorization(db, session, task.id, payload['kind'], payload['resource_id'],
                        payload['action_id'], view(db, task), cancelled=payload['cancelled'])
                elif recovering and r.get('status') in {'running', 'partially_completed'}:
                    rt.resume_control(db, session, task.id, view(db, task))
                # No waiter means the receipt was already consumed; never resubmit.
                latest = rt.get(db.get(AgentTask, task.id)) or {}
                if latest.get('status') == 'partially_completed':
                    status, code = 'blocked', 'FOLLOWUP_INCOMPLETE'
    except DomainError as error:
        status, code = 'blocked', error.code
    except Exception:
        LOGGER.exception('Durable follow-up failed; receipt remains authoritative')
        status, code = 'blocked', 'FOLLOWUP_INTERNAL_ERROR'
    with factory() as db, atomic_write(db):
        row = db.get(WorkflowJob, jid)
        if row.status == 'running' and row.lease_token == token:
            row.status, row.error_code = status, code
            row.lease_token = row.lease_until_utc = None
            row.updated_at_utc = stamp(now())
    return True


def run_due_reviews(factory, *, instant=None, limit=20):
    """One finite read-only run per trigger; no model, no subscription or payment API."""
    instant = instant or now()
    with factory() as db:
        ids = list(db.scalars(select(WorkflowReview.id).where(WorkflowReview.status == 'scheduled',
            WorkflowReview.next_run_at_utc <= stamp(instant)).order_by(WorkflowReview.next_run_at_utc).limit(limit)))
    results = []
    for identifier in ids:
        with factory() as db, atomic_write(db):
            row = db.get(WorkflowReview, identifier)
            if row.status != 'scheduled' or parse_time(row.next_run_at_utc) > instant:
                continue
            setting = db.get(DemoSetting, 1)
            if not setting or setting.dataset_version != row.dataset_version:
                row.status, row.error_code = 'blocked', 'DATASET_CHANGED'
                continue
            if instant > parse_time(row.end_at_utc):
                row.status, row.error_code = 'completed', 'REVIEW_WINDOW_ENDED'
                continue
            from app.domain.ledger import available_balance
            from app.agent.control import state
            control = state(db, row.task_id).get('state')
            task = db.get(AgentTask, row.task_id)
            if not task or task.stop_requested or control in {'paused', 'handoff', 'awaiting_reverification', 'terminated'}:
                row.status, row.error_code = 'paused', 'SOURCE_TASK_PAUSED'
                continue
            key = row.id + ':' + row.next_run_at_utc
            if db.scalar(select(WorkflowReviewRun).where(WorkflowReviewRun.event_key == key)):
                continue
            accounts = list(db.scalars(select(Account).where(Account.owner_id == row.owner_id, Account.currency == 'CNY')))
            available = sum(available_balance(db, a) for a in accounts)
            result = dict(as_of_utc=stamp(instant), scheduled_for_utc=row.next_run_at_utc,
                available_minor=available, target_minor=row.target_minor,
                gap_minor=max(0, row.target_minor - available) if row.target_minor else None,
                account_count=len(accounts), requires_review=True,
                note='当前可用资金快照；余额达到参考额不代表新增储蓄，也未预留或转移资金。')
            db.add(WorkflowReviewRun(id=uuid4().hex, review_id=row.id, event_key=key,
                result_json=json.dumps(result, ensure_ascii=False), created_at_utc=stamp(instant)))
            row.latest_result_json = json.dumps(result, ensure_ascii=False)
            # Coalesce missed intervals, rather than replaying months of stale checks.
            next_due = parse_time(row.next_run_at_utc)
            if row.interval_days:
                step = timedelta(days=row.interval_days)
                next_due += step * ((instant - next_due) // step + 1)
            row.status = 'scheduled' if row.interval_days and next_due <= parse_time(row.end_at_utc) else 'completed'
            row.next_run_at_utc, row.updated_at_utc = stamp(next_due), stamp(instant)
            row.version += 1
            from app.domain.security import audit
            audit(db, row.owner_id, 'workflow_review_completed_run', key)
            results.append(result)
    return results


def portfolio(db, session):
    """Project existing domain records; no duplicate execution state or invented history."""
    from app.agent.service import view
    from app.agent.failure_policy import classify
    from app.data.birthday_models import BirthdayPlan, BirthdayReservation
    tasks = list(db.scalars(select(AgentTask).where(AgentTask.owner_id == session.owner_id,
        AgentTask.session_id == session.id, AgentTask.dataset_version == session.dataset_version)
        .order_by(AgentTask.updated_at_utc.desc()).limit(100)))
    plans = []
    for task in tasks:
        graph = view(db, task).get('execution_graph')
        plans.append(dict(id=task.id, kind='goal', title=(graph['goals'][0]['text'] if graph else '对话任务'),
            status=graph['status'] if graph else task.status, updated_at_utc=task.updated_at_utc,
            goals=graph['goals'] if graph else [], task_id=task.id, route='assistant',
            can_review=not task.stop_requested and task.status not in {'cancelled', 'expired'},
            recoveries=[dict(node_id=n['id'], title=n['title'], **classify(n.get('error_code') or '', n.get('policy', {}).get('effect', 'read')))
                        for n in (graph or {}).get('nodes', []) if n.get('error_code') and not n.get('invalidated') and not n.get('recovered_by')]))
    birthdays = db.scalars(select(BirthdayPlan).where(BirthdayPlan.owner_id == session.owner_id,
        BirthdayPlan.dataset_version == session.dataset_version).order_by(BirthdayPlan.updated_at_utc.desc()).limit(100))
    for row in birthdays:
        plans.append(dict(id=row.id, kind='occasion', title='生日礼物安排 · ' + row.birthday_date,
            status=row.status, updated_at_utc=row.updated_at_utc, goals=[], task_id=row.agent_task_id,
            route='tasks/occasions', budget_minor=row.budget_minor, spent_minor=row.spent_minor, can_review=False))
    reviews = db.scalars(select(WorkflowReview).where(WorkflowReview.owner_id == session.owner_id,
        WorkflowReview.dataset_version == session.dataset_version).order_by(WorkflowReview.created_at_utc.desc()).limit(100))
    jobs = db.scalars(select(WorkflowJob).where(WorkflowJob.owner_id == session.owner_id,
        WorkflowJob.session_id == session.id, WorkflowJob.dataset_version == session.dataset_version,
        WorkflowJob.status.in_(['pending', 'running', 'blocked', 'failed'])).order_by(WorkflowJob.created_at_utc.desc()).limit(100))
    holds = db.scalars(select(BirthdayReservation).where(BirthdayReservation.owner_id == session.owner_id,
        BirthdayReservation.dataset_version == session.dataset_version, BirthdayReservation.status == 'active'))
    drafts=[json.loads(t.context_json).get('workflow_review_draft') for t in tasks if not t.stop_requested]
    return dict(plans=plans, reviews=[review_view(r) for r in reviews],
        drafts=[d for d in drafts if d and d.get('status')=='draft'],
        jobs=[dict(id=j.id, task_id=j.task_id, status=j.status, attempts=j.attempts, error_code=j.error_code) for j in jobs],
        reservations=[dict(plan_id=h.plan_id, account_id=h.account_id, amount_minor=h.remaining_minor,
            purpose='生日礼物安排', route='tasks/occasions') for h in holds],
        scope='当前会话目标及本人已授权安排；复查为独立只读执行，不授予资金操作权限。')


class WorkflowWorker:
    def __init__(self, factory, interval_seconds=1):
        self.factory, self.interval = factory, max(.05, interval_seconds)
        self.stop_event = Event()
        self.thread = Thread(target=self.loop, name='finagent-workflows', daemon=True)
        self.followup_thread = None
        self.last_discovery = None

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=5)

    def loop(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                LOGGER.exception('Workflow poll failed; durable work remains stored')
            self.stop_event.wait(self.interval)

    def tick(self):
        # Existing domains retain their own grants, transactions and idempotency.
        # Only triggering is unified. Slow synthesis cannot delay due payments.
        from importlib import import_module
        adapters = [
            ('FINAGENT_SCHEDULER_ENABLED', 'scheduled_transfers', 'run_due_schedules', None),
            ('FINAGENT_SUBSCRIPTIONS_ENABLED', 'subscriptions', 'run_due_subscriptions', 'guarded_subscription_clock'),
            ('FINAGENT_BIRTHDAYS_ENABLED', 'birthdays', 'run_due_birthdays', 'guarded_birthday_clock')]
        for flag, module, handler, clock in adapters:
            if os.getenv(flag, '1') == '0':
                continue
            try:
                adapter = import_module('app.domain.' + module)
                options = {'now': getattr(adapter, clock)(self.factory)()} if clock else {}
                getattr(adapter, handler)(self.factory, **options)
            except Exception:
                LOGGER.exception('Domain trigger failed: %s; independent work continues', module)
        if os.getenv('FINAGENT_WORKFLOWS_ENABLED', '1') == '0':
            return
        run_due_reviews(self.factory)
        if not self.last_discovery or now() - self.last_discovery >= timedelta(seconds=30):
            discover_receipts(self.factory)
            self.last_discovery = now()
        if not self.followup_thread or not self.followup_thread.is_alive():
            self.followup_thread = Thread(target=run_job, args=(self.factory,),
                name='finagent-workflow-followup', daemon=True)
            self.followup_thread.start()
