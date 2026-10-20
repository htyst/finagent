"""Behavior contracts: no real models, no application database or payments."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4
import pytest
from sqlalchemy import select, func

from app.data.models import AgentTask, DemoSession, Account, Transaction, DemoSetting
from app.data.workflow_models import WorkflowReview, WorkflowReviewRun, WorkflowJob
from app.domain import workflows as w
from app.domain.errors import DomainError
from app.agent import execution_graph as graph, authorization
from app.agent.control import change
from app.agent.service import create
from test_dynamic_tools import dynamic, sequence, calls, reply, session, transfers, state
from test_http import client


def task(db):
    instant = w.stamp(w.now())
    row = AgentTask(id=uuid4().hex, owner_id='u1', session_id='s1',
        dataset_version=session(db).dataset_version, mode='live', status='completed',
        context_json='{}', created_at_utc=instant, updated_at_utc=instant)
    db.add(row)
    db.commit()
    return row


def review(db, task_id, **overrides):
    instant = w.now()
    values = dict(task_id=task_id, title='旅行目标资金复查', run_at=w.stamp(instant+timedelta(minutes=1)),
        end_at=w.stamp(instant+timedelta(days=91)), interval_days=30,
        target_minor=2000000, consent=True, request_id='review-request-1')
    values.update(overrides)
    return w.ReviewInput(**values)


def test_ready_selection_runs_independent_work_before_blocked_node():
    r = dict(nodes=[dict(id='old',status='failed')],queue=[
        dict(id='dependent',name='calculate',depends_on=['old']),
        dict(id='independent',name='list_accounts',depends_on=[])])
    assert graph.next_ready(r)['id'] == 'independent'
    assert graph.next_ready(r)['id'] == 'dependent'


def test_review_needs_explicit_scope_and_owned_task(bank):
    db, _ = bank
    row = task(db)
    with pytest.raises(DomainError, match='只读'):
        w.create_review(db, session(db), review(db,row.id,consent=False))
    with pytest.raises(DomainError):
        w.create_review(db, db.get(DemoSession,'other'), review(db,row.id))
    assert db.scalar(select(func.count()).select_from(WorkflowReview)) == 0


def test_review_idempotency_and_changed_terms(bank):
    db, _ = bank
    p = review(db,task(db).id)
    a = w.create_review(db,session(db),p)
    assert w.create_review(db,session(db),p)['id'] == a['id']
    with pytest.raises(DomainError,match='条款'):
        w.create_review(db,session(db),p.model_copy(update={'title':'另一个计划'}))
    assert db.scalar(select(func.count()).select_from(WorkflowReview)) == 1


def test_due_review_survives_new_session_and_never_changes_money(bank):
    db,factory = bank
    p = review(db,task(db).id)
    result = w.create_review(db,session(db),p)
    balances = list(db.execute(select(Account.id,Account.balance_minor).order_by(Account.id)))
    transactions = db.scalar(select(func.count()).select_from(Transaction))
    instant = w.parse_time(p.run_at)+timedelta(seconds=1)
    assert len(w.run_due_reviews(factory,instant=instant)) == 1
    assert w.run_due_reviews(factory,instant=instant) == []
    db.expire_all()
    row = db.get(WorkflowReview,result['id'])
    assert json.loads(row.latest_result_json)['requires_review'] is True
    assert list(db.execute(select(Account.id,Account.balance_minor).order_by(Account.id))) == balances
    assert db.scalar(select(func.count()).select_from(Transaction)) == transactions
    assert db.scalar(select(func.count()).select_from(WorkflowReviewRun)) == 1
    assert w.parse_time(row.next_run_at_utc) > instant


def test_parallel_review_workers_coalesce_one_trigger(bank):
    db,factory = bank
    p = review(db,task(db).id)
    w.create_review(db,session(db),p)
    instant = w.parse_time(p.run_at)+timedelta(days=60)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _:w.run_due_reviews(factory,instant=instant),range(2)))
    assert sum(map(len,results)) == 1
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(WorkflowReviewRun)) == 1


def test_pause_version_and_cancel_do_not_restart_plan(bank):
    db,factory = bank
    p = review(db,task(db).id)
    r = w.create_review(db,session(db),p)
    paused = w.change_review(db,session(db),r['id'],'pause',1)
    assert w.run_due_reviews(factory,instant=w.parse_time(p.run_at)+timedelta(seconds=1)) == []
    with pytest.raises(DomainError,match='变化'):
        w.change_review(db,session(db),r['id'],'resume',1)
    w.change_review(db,session(db),r['id'],'cancel',paused['version'])
    with pytest.raises(DomainError):
        w.change_review(db,session(db),r['id'],'resume',3)


@pytest.mark.parametrize('cause',['dataset','source_pause','expired'])
def test_stale_or_paused_reviews_do_not_read_accounts(bank,cause):
    db,factory = bank
    t=task(db);p=review(db,t.id);w.create_review(db,session(db),p)
    instant=w.parse_time(p.run_at)+timedelta(seconds=1)
    if cause=='dataset':
        db.get(DemoSetting,1).dataset_version='new-dataset';db.commit()
    elif cause=='source_pause':change(db,t,'pause')
    else:instant=w.parse_time(p.end_at)+timedelta(seconds=1)
    assert w.run_due_reviews(factory,instant=instant)==[]
    assert db.scalar(select(func.count()).select_from(WorkflowReviewRun))==0


def test_workflow_api_csrf_scope_and_schema(bank,client):
    db,_=bank;p=review(db,task(db).id).model_dump()
    client.cookies.set('finagent_demo_session','s1')
    assert client.post('/api/workflows/reviews',json=p).status_code==403
    headers={'Origin':'http://testserver','X-FinAgent-CSRF':'test-csrf'}
    assert client.post('/api/workflows/reviews',headers=headers,json=p|{'authorized':True}).status_code==422
    assert client.post('/api/workflows/reviews',headers=headers,json=p).status_code==200
    assert len(client.get('/api/workflows').json()['data']['reviews'])==1
    client.cookies.set('finagent_demo_session','other')
    data=client.get('/api/workflows').json()['data']
    assert data['plans']==[] and data['reviews']==[] and data['jobs']==[]


def prepare_paid(dynamic,monkeypatch):
    db,factory=dynamic
    sequence(monkeypatch,calls(('prepare_transfer',dict(source='活期账户',target='储蓄账户',amount_yuan='100'))),reply('已核对原回执。'))
    result=create(db,session(db),'live','从活期账户转100元到储蓄账户')
    op=result['authorization_operations'][0]
    token=authorization.issue_token(db,session(db),result['id'],'proposal',op['resource_id'],op['action_id'])['confirmation_token']
    authorization.confirm(db,session(db),result['id'],'proposal',op['resource_id'],op['action_id'],token)
    return result,factory


def test_commit_without_http_background_is_discovered_in_queue(dynamic,monkeypatch):
    db,_=dynamic;result,factory=prepare_paid(dynamic,monkeypatch)
    assert transfers(db)==1
    job=db.scalar(select(WorkflowJob))
    assert job.status=='pending'
    assert w.run_job(factory,job.id)
    assert not w.run_job(factory,job.id)
    db.expire_all()
    assert state(db,result['id'])['wait'] is None
    assert transfers(db)==1 and db.get(WorkflowJob,job.id).status=='completed'


def test_expired_claim_after_process_loss_recovers_receipt_once(dynamic,monkeypatch):
    db,_=dynamic;result,factory=prepare_paid(dynamic,monkeypatch)
    job=db.scalar(select(WorkflowJob));instant=w.now()
    assert w.claim_job(factory,job.id,instant=instant)
    assert w.claim_job(factory,job.id,instant=instant) is None
    assert w.run_job(factory,job.id,instant=instant+timedelta(minutes=6))
    db.expire_all()
    assert transfers(db)==1 and state(db,result['id'])['wait'] is None


def test_pause_blocks_durable_followup_without_reverting_payment(dynamic,monkeypatch):
    db,_=dynamic;result,factory=prepare_paid(dynamic,monkeypatch)
    change(db,db.get(AgentTask,result['id']),'pause')
    job=db.scalar(select(WorkflowJob));assert w.run_job(factory,job.id)
    db.expire_all()
    assert db.get(WorkflowJob,job.id).status=='blocked'
    assert transfers(db)==1


def test_real_receipt_repairs_commit_before_acknowledgement_gap(dynamic,monkeypatch):
    db,_=dynamic;result,factory=prepare_paid(dynamic,monkeypatch)
    job=db.scalar(select(WorkflowJob));db.delete(job)
    t=db.get(AgentTask,result['id']);ctx=json.loads(t.context_json)
    ctx.pop('authorization_receipts',None);t.context_json=json.dumps(ctx);db.commit()
    assert w.discover_receipts(factory)==1
    assert w.discover_receipts(factory)==0
    assert w.run_job(factory)
    db.expire_all()
    assert transfers(db)==1 and state(db,result['id'])['wait'] is None


def test_expired_request_retry_returns_existing_review_not_new_work(bank):
    db,_=bank;p=review(db,task(db).id)
    first=w.create_review(db,session(db),p)
    assert w.create_review(db,session(db),p,instant=w.parse_time(p.run_at)+timedelta(days=1))['id']==first['id']


def test_explicit_test_reset_clears_orchestration_children(bank):
    from app.data.seed import reset_demo
    from datetime import date
    db,factory=bank;p=review(db,task(db).id);w.create_review(db,session(db),p)
    w.run_due_reviews(factory,instant=w.parse_time(p.run_at))
    reset_demo(db,date(2026,9,24));db.commit()
    assert db.scalar(select(func.count()).select_from(WorkflowReview))==0
    assert db.scalar(select(func.count()).select_from(WorkflowReviewRun))==0


def test_one_domain_adapter_failure_does_not_block_other_due_work(bank,monkeypatch):
    from app.domain import scheduled_transfers,subscriptions,birthdays
    _,factory=bank;seen=[]
    def broken(*a,**kw):raise RuntimeError('isolated simulated trigger failure')
    monkeypatch.setattr(scheduled_transfers,'run_due_schedules',broken)
    monkeypatch.setattr(subscriptions,'run_due_subscriptions',lambda *a,**kw:seen.append('subscription'))
    monkeypatch.setattr(birthdays,'run_due_birthdays',lambda *a,**kw:seen.append('birthday'))
    for flag in ('FINAGENT_SCHEDULER_ENABLED','FINAGENT_SUBSCRIPTIONS_ENABLED','FINAGENT_BIRTHDAYS_ENABLED'):
        monkeypatch.setenv(flag,'1')
    monkeypatch.setenv('FINAGENT_WORKFLOWS_ENABLED','0')
    w.WorkflowWorker(factory).tick()
    assert seen==['subscription','birthday']


def test_actual_worker_picks_due_review_without_http_request(bank,monkeypatch):
    import time
    db,factory=bank;p=review(db,task(db).id);w.create_review(db,session(db),p)
    instant=w.parse_time(p.run_at)
    monkeypatch.setattr(w,'now',lambda:instant)
    for flag in ('FINAGENT_SCHEDULER_ENABLED','FINAGENT_SUBSCRIPTIONS_ENABLED','FINAGENT_BIRTHDAYS_ENABLED'):
        monkeypatch.setenv(flag,'0')
    monkeypatch.setenv('FINAGENT_WORKFLOWS_ENABLED','1')
    worker=w.WorkflowWorker(factory,interval_seconds=.05).start()
    try:
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            with factory() as fresh:
                count=fresh.scalar(select(func.count()).select_from(WorkflowReviewRun))
            if count:break
            time.sleep(.02)
        assert count==1
    finally:
        worker.stop()
    assert not worker.thread.is_alive()
