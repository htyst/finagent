from __future__ import annotations
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch
import pytest
from sqlalchemy import select, func, delete
from sqlalchemy.orm import Session
from test_m4 import demo, login, start, say
from app.data.models import (Account, AgentStep, AgentTask, BusinessResult, Card, CardActionOrder,
                             DataCoverage, Proposal, Transaction, TransferOrder)
from app.domain.ledger import post_refund, post_entry
from app.domain.operations import confirm_proposal, issue_confirmation_token
from app.agent.crossflow import recover, cancel_task
from app.agent.plans import validate_plan
from app.domain.errors import DomainError

STORY_A = "昨晚有笔不认识的消费，帮我查是哪张卡；如果是尾号1234，帮我临时冻结它，再告诉我账户可用余额。"
STORY_B = "如果本月餐饮超过500元，就从活期转100元到储蓄账户。"

def count(db, model):
    return db.scalar(select(func.count()).select_from(model)) or 0

def token(client, headers, proposal_id):
    response = client.post(f"/api/proposals/{proposal_id}/confirmation-token", headers=headers)
    assert response.status_code == 200, response.json()
    return response.json()["data"]["confirmation_token"]

def confirm(client, headers, task):
    pid = task["proposal"]["id"]
    return client.post(f"/api/proposals/{pid}/confirm", headers=headers,
                       json={"confirmation_token": token(client, headers, pid)})

def get(client, task):
    return client.get(f"/api/tasks/{task['id']}").json()["data"]

def test_conditional_transfer_success_and_durable_recovery(demo):
    client, engine = demo
    headers = login(client)
    task = start(client, headers, STORY_B)
    assert task["intent"] == "conditional_transfer" and task["status"] == "awaiting_confirmation", task
    assert task["proposal"]["summary"]["condition"]["threshold_minor"] == 50000
    response = confirm(client, headers, task)
    assert response.status_code == 200, response.json()
    receipt = response.json()["data"]
    task = get(client, task)
    assert task["status"] == "completed" and task["steps"][1]["status"] == "succeeded"
    assert task["proposal"]["receipt"] == receipt
    with Session(engine) as db:
        assert db.get(Account,"a1c").balance_minor == 990000
        assert count(db, TransferOrder) == count(db, BusinessResult) == 1
        recover(db, db.get(AgentTask, task["id"]))
        assert count(db, TransferOrder) == 1

def test_new_refund_invalidates_condition_before_version_check(demo):
    client, engine = demo
    headers = login(client)
    task = start(client, headers, STORY_B)
    with Session(engine) as db:
        post_refund(db, owner_id="u1", original_id="t_food_1000", amount_minor=80000,
                    business_at=datetime.fromisoformat("2026-09-24T12:00:00+08:00"))
        db.commit()
    response = confirm(client, headers, task)
    assert response.status_code == 409 and response.json()["error_code"] == "CONDITION_NOT_MET", response.json()
    task = get(client, task)
    assert task["status"] == "completed" and task["steps"][1]["status"] == "skipped"
    assert task["proposal"]["validation"]["evidence"]["current"]["totals"]["net_minor"] == 40000
    with Session(engine) as db:
        assert count(db, TransferOrder) == 0 and db.get(Account,"a1c").balance_minor == 1080000

def test_card_story_success_and_selected_evidence(demo):
    client, engine = demo
    headers = login(client)
    task = start(client, headers, STORY_A)
    assert task["status"] == "awaiting_confirmation", task
    assert task["proposal"]["summary"]["condition"]["transaction_id"] == "t_unknown_now"
    assert confirm(client, headers, task).status_code == 200
    task = get(client, task)
    assert task["status"] == "completed"
    assert task["steps"][2]["result"]["available_balance_minor"] == 1000000
    with Session(engine) as db:
        assert db.get(Card,"c1a").status == "frozen" and count(db, CardActionOrder) == 1

@pytest.mark.parametrize("only", [True, False])
def test_reject_only_operation_vs_cancel_whole_task(demo, only):
    client, engine = demo
    headers = login(client)
    task = start(client, headers, STORY_A)
    action = "reject-operation" if only else "cancel"
    response = client.post(f"/api/tasks/{task['id']}/{action}", headers=headers)
    assert response.status_code == 200, response.json()
    result = response.json()["data"]
    assert result["status"] == ("partially_completed" if only else "cancelled")
    assert result["steps"][2]["status"] == ("succeeded" if only else "cancelled")
    with Session(engine) as db:
        assert count(db, CardActionOrder) == 0 and db.get(Card,"c1a").status == "active"


def test_false_and_incomplete_conditions_never_prepare(demo):
    client, engine = demo
    headers = login(client)
    task = start(client, headers, STORY_B.replace("500元", "2000元"))
    assert task["status"] == "completed" and task["proposal"] is None
    assert task["steps"][1]["status"] == "skipped"
    with Session(engine) as db:
        db.execute(delete(DataCoverage).where(DataCoverage.owner_id == "u1")); db.commit()
    task = start(client, headers, STORY_B)
    assert task["status"] == "failed" and task["proposal"] is None

@pytest.mark.parametrize("change,code", [("coverage", "DATA_INCOMPLETE"),("small_refund", "STALE_RESOURCE")])
def test_confirm_rechecks_coverage_and_resource_versions(demo, change, code):
    client, engine = demo
    headers = login(client)
    task = start(client, headers, STORY_B)
    with Session(engine) as db:
        if change == "coverage":
            db.execute(delete(DataCoverage).where(DataCoverage.owner_id == "u1"))
        else:
            post_refund(db, owner_id="u1", original_id="t_food_1000", amount_minor=100,
                        business_at=datetime.fromisoformat("2026-09-24T12:00:00+08:00"))
        db.commit()
    response = confirm(client, headers, task)
    assert response.status_code == 409 and response.json()["error_code"] == code
    with Session(engine) as db:
        assert count(db, TransferOrder) == 0

def test_multiple_transactions_require_explicit_selection(demo):
    client, engine = demo
    headers = login(client)
    with Session(engine) as db:
        extra = post_entry(db, owner_id="u1", account_id="a1s", kind="purchase", direction="debit",
                           amount_minor=9900, card_id="c1b",
                           business_at=datetime.fromisoformat("2026-09-23T22:00:00+08:00"))
        extra_id = extra.id
        db.commit()
    task = start(client, headers, STORY_A)
    assert task["status"] == "gathering_info" and task["proposal"] is None
    assert len(task["candidates"]) == 2
    task = say(client, headers, task, "选择交易 " + extra_id)
    assert task["status"] == "completed" and task["steps"][1]["status"] == "skipped"
    assert task["steps"][2]["result"]["account_id"] == "a1s"
    with Session(engine) as db:
        assert count(db, CardActionOrder) == 0

def test_failure_to_freeze_does_not_hide_balance_result(demo):
    client, engine = demo
    headers = login(client)
    with Session(engine) as db:
        db.get(Account,"a1c").status = "disabled"; db.commit()
    task = start(client, headers, STORY_A)
    assert task["status"] == "partially_completed"
    assert task["steps"][1]["status"] == "failed"
    assert "未能准备" in task["messages"][-1]["content"]
    assert task["steps"][2]["status"] == "succeeded"

def test_committed_write_survives_failed_read_and_restart(demo):
    client, engine = demo
    headers = login(client)
    task = start(client, headers, STORY_A)
    response = confirm(client, headers, task)
    assert response.status_code == 200
    def read_error(*_args):
        raise RuntimeError("injected read failure")
    with Session(engine) as db:
        persisted = db.get(AgentTask,task["id"])
        step = db.scalar(select(AgentStep).where(AgentStep.task_id == persisted.id, AgentStep.ordinal == 2))
        assert step.status == "succeeded"  # Written atomically with the business result.
        recover(db, persisted, balance_reader=read_error)
    task = get(client, task)
    assert task["status"] == "partially_completed"
    assert task["proposal"]["receipt"]["message"] == "卡片已冻结"
    recovered = client.post(f"/api/tasks/{task['id']}/resume", headers=headers).json()["data"]
    assert recovered["status"] == "completed"
    with Session(engine) as db:
        assert count(db, CardActionOrder) == count(db, BusinessResult) == 1

def test_condition_change_replaces_old_proposal(demo):
    client, engine = demo
    headers = login(client)
    task = start(client, headers, STORY_B)
    old = task["proposal"]["id"]
    task = say(client, headers, task, "阈值改为1000元")
    assert task["plan_version"] == 2 and task["status"] == "awaiting_confirmation"
    assert task["proposal"]["id"] != old
    assert task["proposal"]["summary"]["condition"]["threshold_minor"] == 100000
    with Session(engine) as db:
        assert db.get(Proposal, old).status == "invalidated"

def test_condition_check_and_commit_exclude_competing_writer(demo):
    from sqlalchemy.exc import OperationalError
    from app.domain import operations
    client, engine = demo
    headers = login(client)
    task = start(client, headers, STORY_B)
    pid = task["proposal"]["id"]
    sid = client.cookies.get("finagent_demo_session")
    credential = token(client, headers, pid)
    entered, release = Event(), Event()
    evaluate = operations.evaluate_condition
    def guarded(*args):
        result = evaluate(*args)
        entered.set()
        assert release.wait(10)
        return result
    def submit():
        with Session(engine) as db:
            return confirm_proposal(db, session_id=sid, proposal_id=pid, confirmation_token=credential)
    with patch.object(operations, "evaluate_condition", guarded), ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(submit)
        try:
            assert entered.wait(10)
            with engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA busy_timeout=0")
                with pytest.raises(OperationalError):
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
        finally:
            release.set()
        assert future.result()["amount_minor"] == 10000
    with Session(engine) as db:
        assert count(db, TransferOrder) == 1

def test_plan_limits_and_reference_validation():
    for invalid in ([{"ordinal":1,"tool_name":"confirm"}],
                    [{"ordinal":1,"tool_name":"list_accounts","depends_on":[2]}],
                    [{"ordinal":1,"tool_name":"prepare_transfer"},{"ordinal":2,"tool_name":"prepare_card_action"}],
                    [{"ordinal":i,"tool_name":"list_accounts"} for i in range(1,14)]):
        with pytest.raises(DomainError):
            validate_plan(invalid)

def test_transaction_binding_changed_before_confirmation(demo):
    client, engine = demo
    headers = login(client)
    task = start(client, headers, STORY_A)
    with Session(engine) as db:
        db.get(Transaction,"t_unknown_now").status = "void"; db.commit()
    response = confirm(client, headers, task)
    assert response.status_code == 409 and response.json()["error_code"] == "TRANSACTION_CHANGED"
    task = get(client, task)
    assert task["status"] == "partially_completed" and task["steps"][1]["status"] == "failed"
    with Session(engine) as db:
        assert db.get(Card,"c1a").status == "active"


@pytest.mark.parametrize("first", ["confirm", "cancel"])
def test_task_cancel_and_confirmation_are_serialized(demo, first):
    from app.domain import operations
    from app.data.models import DemoSession
    client, engine = demo
    headers = login(client)
    task = start(client, headers, STORY_A)
    pid, tid = task["proposal"]["id"], task["id"]
    sid = client.cookies.get("finagent_demo_session")
    credential = token(client, headers, pid)
    entered, release = Event(), Event()
    end = operations._end
    def guarded(db, proposal, status, *args):
        if status == ("executed" if first == "confirm" else "cancelled"):
            entered.set()
            assert release.wait(10)
        return end(db, proposal, status, *args)
    def submit(action):
        with Session(engine) as db:
            if action == "cancel":
                cancel_task(db, db.get(DemoSession,sid), db.get(AgentTask,tid))
                return "cancelled"
            try:
                confirm_proposal(db, session_id=sid, proposal_id=pid, confirmation_token=credential)
                return "executed"
            except DomainError as error:
                return error.code
    with patch.object(operations,"_end",guarded), patch("app.agent.crossflow._end",guarded), ThreadPoolExecutor(max_workers=2) as pool:
        leading = pool.submit(submit,first)
        try:
            assert entered.wait(10)
            following = pool.submit(submit,"cancel" if first == "confirm" else "confirm")
        finally:
            release.set()
        leading.result(); following.result()
    result = get(client,task)
    with Session(engine) as db:
        assert count(db,CardActionOrder) == (1 if first == "confirm" else 0)
    assert result["status"] == ("partially_completed" if first == "confirm" else "cancelled")
    assert result["steps"][2]["status"] == "cancelled"
    assert bool(result["proposal"]["receipt"]) == (first == "confirm")


def test_crash_after_preparation_recovers_atomic_link_without_model(demo):
    client, engine = demo
    headers = login(client)
    with patch("app.agent.crossflow._apply_prepared", side_effect=RuntimeError("simulated disconnect")):
        with pytest.raises(RuntimeError):
            start(client, headers, STORY_B)
    with Session(engine) as db:
        task = db.scalar(select(AgentTask))
        assert task.proposal_id
        tid = task.id
    with patch("app.agent.crossflow.model_check", side_effect=AssertionError("recovery called model")):
        result = client.get(f"/api/tasks/{tid}").json()["data"]
    assert result["status"] == "awaiting_confirmation"
    with Session(engine) as db:
        assert count(db,Proposal) == 1 and count(db,TransferOrder) == 0


def test_terminal_single_task_cannot_be_repurposed_as_crossflow(demo):
    client, engine = demo
    headers = login(client)
    task = start(client, headers, "本月餐饮消费多少")
    response = client.post(f"/api/tasks/{task['id']}/messages", headers=headers,json={"message":STORY_B})
    assert response.status_code == 409
    assert get(client,task)["intent"] == "spending"


def test_evaluation_partition_and_schema():
    from app.agent.eval_cases import load_cases
    assert len(load_cases("dev")) == 30
    assert len(load_cases("holdout")) == 10
