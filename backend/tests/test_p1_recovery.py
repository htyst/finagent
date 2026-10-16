import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import func, select
from app.agent import language
from app.agent.service import create, send
from app.data.models import Account, AgentTask, DemoSession, Proposal, Recipient, TransferOrder
from app.domain.errors import DomainError
from app.domain.operations import prepare_transfer, issue_confirmation_token, confirm_proposal
from test_background import schedule


def test_worker_restart_in_two_real_processes_executes_once(bank):
    db, _ = bank
    sid, due = schedule(db); db.commit()
    database = db.get_bind().url.database
    script = Path(database).parent / 'restart-worker.py'
    script.write_text("""import sys
from datetime import datetime
from app.data.db import SessionLocal
from app.domain.scheduled_transfers import run_due_schedules
result=run_due_schedules(SessionLocal,now=datetime.fromisoformat(sys.argv[1]))
print('persisted-worker-restart-ok')
""", encoding='utf-8')
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]), FINAGENT_DB_PATH=database,
               MODEL_API_KEY='', FINAGENT_MODE='mock')
    for _ in range(2):
        completed = subprocess.run([sys.executable, str(script), due.isoformat()], env=env,
            capture_output=True, timeout=20, check=True)
        assert b'persisted-worker-restart-ok' in completed.stdout
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(TransferOrder)) == 1


def test_write_success_then_read_failure_handoff_and_restart_only_retries_read(bank, monkeypatch):
    from app.agent import registry
    from app.agent import orchestrator
    from app.agent.orchestrator import install, run, default_plan
    from app.agent.control import change
    db, factory = bank
    task = db.get(AgentTask, create(db, db.get(DemoSession, 's1'), 'mock', '查询余额')['id'])
    goals = [{'id': 'g1', 'tool': 'prepare_transfer', 'params': {'source_account_id': 'a1c',
        'target': {'type': 'own_account', 'account_id': 'a1s'}, 'amount_minor': 10000}},
        {'id': 'g2', 'tool': 'read_wealth', 'params': {}}]
    install(db, task, goals, default_plan(goals), []); run(db, task)
    pid = task.proposal_id
    token = issue_confirmation_token(db, session_id='s1', proposal_id=pid)['confirmation_token']
    receipt = confirm_proposal(db, session_id='s1', proposal_id=pid, confirmation_token=token)
    execute = registry.execute
    def fail_read(*args, **kwargs):
        if kwargs.get('name') == 'read_wealth': raise DomainError('READ_UNAVAILABLE', 'temporary read failure', 503)
        return execute(*args, **kwargs)
    monkeypatch.setattr(orchestrator, 'execute', fail_read)
    result = send(db, db.get(DemoSession, 's1'), task.id, '继续')
    assert result['control']['state'] == 'handoff'
    assert result['proposal']['receipt']['order_id'] == receipt['order_id']
    monkeypatch.setattr(orchestrator, 'execute', execute)
    with factory() as restarted:
        fresh = restarted.get(AgentTask, task.id)
        change(restarted, fresh, 'resume', operator=True)
        result = send(restarted, restarted.get(DemoSession, 's1'), task.id, '继续')
        assert result['status'] == 'completed'
        assert result['proposal']['receipt']['order_id'] == receipt['order_id']
        assert restarted.scalar(select(func.count()).select_from(TransferOrder)) == 1


def test_late_live_reply_after_cancel_cannot_prepare(bank, monkeypatch):
    from app.agent.service import cancel
    db, _ = bank
    def late(payload):
        task = db.scalar(select(AgentTask).order_by(AgentTask.created_at_utc.desc()))
        cancel(db, db.get(DemoSession, 's1'), task.id)
        return language.Understanding(intent='transfer', source='活期账户', target='储蓄账户', amount_yuan='100')
    monkeypatch.setattr(language, 'interpret', late)
    result = create(db, db.get(DemoSession, 's1'), 'live', '把活期的一百块挪到储蓄')
    assert result['status'] == 'cancelled' and result['proposal'] is None
    assert db.scalar(select(func.count()).select_from(TransferOrder)) == 0


def test_transfer_edit_balance_interrupt_and_cancel_preserve_no_execution(bank):
    db, _ = bank
    session = db.get(DemoSession, 's1')
    result = create(db, session, 'mock', '从活期账户转100元到储蓄账户')
    first = result['proposal']['id']
    result = send(db, session, result['id'], '查询余额')
    assert result['proposal']['id'] == first
    for text, amount in [('金额改成80元',8000),('改成60元',6000),('调整为90元',9000)]:
        previous_id = result['proposal']['id']
        result = send(db, session, result['id'], text)
        assert result['proposal']['summary']['amount_minor'] == amount
        assert db.get(Proposal, previous_id).status == 'invalidated'
    assert db.get(Proposal, first).status == 'invalidated'
    from app.agent.service import cancel
    assert cancel(db, session, result['id'])['status'] == 'cancelled'
    assert db.scalar(select(func.count()).select_from(TransferOrder)) == 0


def test_disabled_recipient_and_invalid_new_account_are_rejected(bank):
    db, _ = bank
    db.get(Recipient, 'r3').status = 'disabled'; db.commit()
    with pytest.raises(DomainError):
        prepare_transfer(db, session_id='s1', source_account_id='a1c', target={'type':'saved_recipient','recipient_id':'r3'}, amount_minor=10000)
    with pytest.raises(DomainError):
        prepare_transfer(db, session_id='s1', source_account_id='a1c', target={'type':'external_account','name':'合成测试','bank_name':'测试银行','account_number':'12!?bad'}, amount_minor=10000)


def test_saved_remark_short_form_is_not_transfer_memo(bank):
    from app.domain.recipient_directory import note_reference
    db, _ = bank
    assert note_reference('从活期账户转100元给备注房东')[0] == '房东'
    assert note_reference('从活期账户转100元给李文，转账备注为房东')[0] is None
    result = create(db, db.get(DemoSession, 's1'), 'mock', '从活期账户转100元给备注房东')
    assert result['proposal']['summary']['target']['id'] == 'r_phone_demo'


def test_settled_wealth_does_not_complete_new_card_request_on_same_task(bank):
    from app.domain import wealth
    from test_security import mfa
    db, _ = bank
    session = db.get(DemoSession, 's1')
    wealth.submit_assessment(db, session_id='s1', answers=[0,0,0,0])
    result = create(db, session, 'mock', '从活期账户申购100元P1')
    rid = result['wealth_order']['id']; mfa(db, rid, kind='wealth')
    token = wealth.issue_token(db, session_id='s1', order_id=rid)['confirmation_token']
    wealth.confirm_order(db, session_id='s1', order_id=rid, confirmation_token=token)
    result = send(db, session, result['id'], '申请一张虚拟卡')
    assert result['status'] == 'gathering_info' and result['intent'] == 'card_management'
    assert result['wealth_order']['status'] == 'settled' and result['card_request'] is None


def test_analysis_omitted_followups_and_annual_extreme_keep_date_scope(bank):
    db, _ = bank
    session = db.get(DemoSession,'s1')
    previous=create(db,session,'mock','查询上月餐饮消费')
    assert previous['steps'][-1]['arguments']['start']=='2026-08-01'
    current=create(db,session,'mock','那本月呢')
    assert current['steps'][-1]['result']['current']['period']['start_date']=='2026-09-01'
    annual=create(db,session,'mock','生成2025年年度账单报告')
    follow=create(db,session,'mock','全年哪几个月净消费最高')
    assert follow['steps'][-1]['tool_name']=='generate_statement'
    assert follow['steps'][-1]['arguments']['start']=='2025-01-01'
    assert follow['steps'][-1]['result']['highest_months']==[]  # incomplete months are not zeros
