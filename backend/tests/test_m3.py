from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.data.db import get_db, make_engine
from app.data.models import (Account, AuditEvent, Base, BusinessResult, Card, CardActionOrder,
                             DemoSession, Proposal, Task, Transaction, TransferOrder)
from app.data.seed import check_ledger, reset_demo
from app.domain.errors import DomainError
from app.domain.ledger import post_entry
from app.domain.operations import cancel_proposal, confirm_proposal, issue_confirmation_token
from app.main import app

TRANSFER = {"source_account_id": "a1c", "target": {"type": "own_account", "account_id": "a1s"},
            "amount_minor": 10_000}


@pytest.fixture
def demo(tmp_path, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "ROOT", tmp_path)
    engine = make_engine(tmp_path / "m3.sqlite3")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        reset_demo(db)

    def override():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = override
    with TestClient(app) as client:
        yield client, engine
    app.dependency_overrides.clear()
    engine.dispose()


def login(client: TestClient, user: str = "u1") -> tuple[str, dict]:
    assert client.post("/api/demo/session", json={"user_id": user}).status_code == 200
    session_id = client.cookies.get("finagent_demo_session")
    csrf = client.get("/api/demo/csrf")
    assert csrf.status_code == 200 and csrf.headers["cache-control"] == "no-store"
    return session_id, {"Origin": "http://testserver", "X-FinAgent-CSRF": csrf.json()["data"]["csrf_token"]}


def prepare(client: TestClient, headers: dict, payload: dict | None = None) -> dict:
    response = client.post("/api/transfers/prepare", headers=headers, json=payload or TRANSFER)
    assert response.status_code == 200, response.json()
    return response.json()["data"]["proposal"]


def token(client: TestClient, headers: dict, proposal_id: str) -> str:
    response = client.post(f"/api/proposals/{proposal_id}/confirmation-token", headers=headers)
    assert response.status_code == 200, response.json()
    assert response.headers["cache-control"] == "no-store"
    return response.json()["data"]["confirmation_token"]


def confirm(client: TestClient, headers: dict, proposal_id: str, value: str):
    return client.post(f"/api/proposals/{proposal_id}/confirm", headers=headers,
                       json={"confirmation_token": value})


def count(db: Session, model) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


def test_internal_transfer_is_atomic_and_idempotent(demo):
    client, engine = demo
    _session_id, headers = login(client)
    before = client.get("/api/accounts").json()["data"]
    proposal = prepare(client, headers)
    assert proposal["summary"]["amount_minor"] == 10_000
    assert "confirmation_token" not in str(proposal)
    assert client.get("/api/accounts").json()["data"] == before
    reused = client.post("/api/transfers/prepare", headers=headers, json=TRANSFER).json()["data"]
    assert reused["reused"] is True and reused["proposal"]["id"] == proposal["id"]
    credential = token(client, headers, proposal["id"])
    response = confirm(client, headers, proposal["id"], credential)
    assert response.status_code == 200
    receipt = response.json()["data"]
    assert receipt["message"] == "模拟转账成功" and len(receipt["transaction_ids"]) == 2
    assert receipt["source_balance_minor"] == 990_000
    assert receipt["target_balance_minor"] == 2_010_000
    assert confirm(client, headers, proposal["id"], credential).json()["data"] == receipt
    assert client.get(f"/api/proposals/{proposal['id']}").json()["data"]["receipt"] == receipt
    task = client.get(f"/api/tasks/{proposal['task_id']}").json()["data"]
    assert task["status"] == "executed" and any(event["event"] == "executed" for event in task["events"])
    with Session(engine) as db:
        assert db.get(Account, "a1c").balance_minor == 990_000
        assert db.get(Account, "a1s").balance_minor == 2_010_000
        assert count(db, TransferOrder) == count(db, BusinessResult) == 1
        legs = db.scalars(select(Transaction).where(Transaction.business_id == proposal["id"])).all()
        assert len(legs) == 2 and {leg.direction for leg in legs} == {"debit", "credit"}
        assert check_ledger(db) == []


def test_external_transfer_only_debits_source_and_limits(demo):
    client, engine = demo
    _session_id, headers = login(client)
    payload = {"source_account_id": "a1s", "target": {"type": "saved_recipient", "recipient_id": "r1"},
               "amount_minor": 500_000, "note": "演示"}
    for _ in range(2):
        proposal = prepare(client, headers, payload)
        with Session(engine) as db:
            from test_security import mfa
            mfa(db, proposal["id"], sid=_session_id)
        receipt = confirm(client, headers, proposal["id"], token(client, headers, proposal["id"])).json()["data"]
        assert receipt["recipient_id"] == "r1" and receipt["target_account_id"] is None
        assert len(receipt["transaction_ids"]) == 1
    denied = client.post("/api/transfers/prepare", headers=headers,
                         json={**payload, "amount_minor": 1})
    assert denied.status_code == 400 and denied.json()["error_code"] == "LIMIT_EXCEEDED"
    with Session(engine) as db:
        assert db.get(Account, "a1s").balance_minor == 1_000_000
        assert db.get(Account, "a2c").balance_minor == 680_000
        assert count(db, TransferOrder) == 2 and check_ledger(db) == []


def test_card_freeze_unfreeze_and_new_purchase_restriction(demo):
    client, engine = demo
    _session_id, headers = login(client)
    for action, expected in (("freeze", "frozen"), ("unfreeze", "active")):
        response = client.post("/api/cards/c1a/actions/prepare", headers=headers, json={"action": action})
        assert response.status_code == 200
        proposal = response.json()["data"]["proposal"]
        if action == "unfreeze":
            with Session(engine) as db:
                from test_security import mfa
                mfa(db, proposal["id"], sid=_session_id)
        result = confirm(client, headers, proposal["id"], token(client, headers, proposal["id"]))
        assert result.json()["data"]["card_status"] == expected
        assert client.post("/api/cards/c1a/actions/prepare", headers=headers,
                           json={"action": action}).json()["data"]["already_in_state"] is True
        if action == "freeze":
            with Session(engine) as db:
                with pytest.raises(DomainError) as error:
                    post_entry(db, owner_id="u1", account_id="a1c", kind="purchase", direction="debit",
                               amount_minor=100, business_at=datetime.fromisoformat("2026-09-24T12:00:00+08:00"),
                               card_id="c1a")
                assert error.value.code == "INVALID_CARD_STATE"
    with Session(engine) as db:
        assert db.get(Card, "c1a").status == "active"
        assert db.get(Account, "a1c").balance_minor == 1_000_000
        assert count(db, CardActionOrder) == 2 and check_ledger(db) == []


def test_csrf_forgery_extra_fields_and_cross_user_visibility(demo):
    client, engine = demo
    _session_id, headers = login(client)
    assert client.post("/api/transfers/prepare", json=TRANSFER).status_code == 403
    assert client.post("/api/transfers/prepare", headers={**headers, "Origin": "https://attacker.test"},
                       json=TRANSFER).status_code == 403
    assert client.post("/api/transfers/prepare", headers=headers,
                       json={**TRANSFER, "user_id": "u2"}).status_code == 422
    assert client.post("/api/transfers/prepare", headers=headers,
                       json={**TRANSFER, "target": {"type": "own_account", "account_id": "a2c"}}).status_code == 404
    proposal = prepare(client, headers)
    credential = token(client, headers, proposal["id"])
    assert confirm(client, headers, proposal["id"], "forged").json()["error_code"] == "CONFIRMATION_INVALID"
    assert client.post(f"/api/proposals/{proposal['id']}/confirm", headers=headers,
                       json={"confirmation_token": credential, "amount_minor": 1}).status_code == 422
    with TestClient(app) as other:
        _other_id, other_headers = login(other, "u2")
        assert other.get(f"/api/proposals/{proposal['id']}").status_code == 404
        assert confirm(other, other_headers, proposal["id"], credential).status_code == 404
    assert confirm(client, headers, proposal["id"], credential).status_code == 200
    with Session(engine) as db:
        assert count(db, TransferOrder) == 1 and check_ledger(db) == []


def test_cancel_expire_and_parameter_change(demo):
    client, engine = demo
    session_id, headers = login(client)
    proposal = prepare(client, headers)
    conflict = client.post("/api/transfers/prepare", headers=headers,
                           json={**TRANSFER, "amount_minor": 20_000})
    assert conflict.status_code == 409 and conflict.json()["error_code"] == "ACTIVE_WRITE_TASK_EXISTS"
    changed = client.post("/api/transfers/prepare", headers=headers,
                          json={**TRANSFER, "amount_minor": 20_000, "task_id": proposal["task_id"]})
    assert changed.status_code == 200
    changed_proposal = changed.json()["data"]["proposal"]
    assert changed_proposal["id"] != proposal["id"]
    assert client.get(f"/api/proposals/{proposal['id']}").json()["data"]["status"] == "invalidated"
    credential = token(client, headers, changed_proposal["id"])
    cancelled = client.post(f"/api/proposals/{changed_proposal['id']}/cancel", headers=headers)
    assert cancelled.json()["data"]["status"] == "cancelled"
    assert confirm(client, headers, changed_proposal["id"], credential).status_code == 409
    fresh = prepare(client, headers)
    after_expiry = datetime.fromisoformat(fresh["expires_at_utc"]) + timedelta(seconds=1)
    with Session(engine) as db:
        with pytest.raises(DomainError) as error:
            issue_confirmation_token(db, session_id=session_id, proposal_id=fresh["id"], now=after_expiry)
        assert error.value.code == "PROPOSAL_EXPIRED"
    assert client.get(f"/api/proposals/{fresh['id']}").json()["data"]["status"] == "expired"
    with Session(engine) as db:
        assert db.get(Account, "a1c").balance_minor == 1_000_000
        assert count(db, TransferOrder) == 0
        assert count(db, Task) == 3


def test_resource_version_change_invalidates_old_proposal(demo):
    client, engine = demo
    _session_id, headers = login(client)
    old = prepare(client, headers)
    credential = token(client, headers, old["id"])
    with TestClient(app) as second:
        _sid2, h2 = login(second)
        proposal = prepare(second, h2, {**TRANSFER, "amount_minor": 100})
        assert confirm(second, h2, proposal["id"], token(second, h2, proposal["id"])).status_code == 200
    denied = confirm(client, headers, old["id"], credential)
    assert denied.status_code == 409 and denied.json()["error_code"] == "STALE_RESOURCE"
    with Session(engine) as db:
        assert db.get(Proposal, old["id"]).status == "invalidated"
        assert count(db, TransferOrder) == 1 and check_ledger(db) == []


def test_session_switch_and_reset_invalidate_proposals(demo):
    client, engine = demo
    old_session, headers = login(client)
    old = prepare(client, headers)
    assert client.post("/api/demo/session", json={"user_id": "u2"}).status_code == 200
    assert client.get(f"/api/proposals/{old['id']}").status_code == 404
    with Session(engine) as db:
        assert db.get(Proposal, old["id"]).status == "invalidated"
        assert db.get(DemoSession, old_session) is None
    assert client.post("/api/demo/session", json={"user_id": "u1"}).status_code == 200
    headers = {"Origin": "http://testserver", "X-FinAgent-CSRF": client.get("/api/demo/csrf").json()["data"]["csrf_token"]}
    fresh = prepare(client, headers)
    with Session(engine) as db:
        reset_demo(db)
    assert client.get(f"/api/proposals/{fresh['id']}").status_code == 401


def test_precommit_failure_rolls_back_then_retries_and_recovers(demo):
    client, engine = demo
    session_id, headers = login(client)
    proposal = prepare(client, headers)
    credential = token(client, headers, proposal["id"])

    def fail():
        raise RuntimeError("test injected before commit")

    with Session(engine) as db:
        with pytest.raises(RuntimeError):
            confirm_proposal(db, session_id=session_id, proposal_id=proposal["id"],
                             confirmation_token=credential, before_commit=fail)
    with Session(engine) as db:
        assert db.get(Proposal, proposal["id"]).status == "pending_confirmation"
        assert db.get(Account, "a1c").balance_minor == 1_000_000
        assert count(db, TransferOrder) == count(db, BusinessResult) == 0
        assert db.scalar(select(AuditEvent).where(AuditEvent.proposal_id == proposal["id"],
                                                   AuditEvent.event == "execution_error")) is not None
    receipt = confirm(client, headers, proposal["id"], credential).json()["data"]
    # Simulate a lost HTTP response and a fresh process reading durable state.
    with Session(engine) as db:
        stored = db.scalar(select(BusinessResult).where(BusinessResult.proposal_id == proposal["id"]))
        assert stored is not None
    assert client.get("/api/tasks/current").json()["data"]["proposal"]["receipt"] == receipt
    assert confirm(client, headers, proposal["id"], credential).json()["data"] == receipt
    with Session(engine) as db:
        assert count(db, TransferOrder) == 1 and check_ledger(db) == []


def test_concurrent_same_proposal_and_cancel_confirm_race(demo):
    client, engine = demo
    session_id, headers = login(client)
    proposal = prepare(client, headers)
    credential = token(client, headers, proposal["id"])
    barrier = Barrier(2)

    def attempt_confirm():
        with Session(engine) as db:
            barrier.wait()
            return confirm_proposal(db, session_id=session_id, proposal_id=proposal["id"],
                                    confirmation_token=credential)

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(attempt_confirm)
        two = pool.submit(attempt_confirm)
        assert one.result() == two.result()
    with Session(engine) as db:
        assert count(db, TransferOrder) == 1 and check_ledger(db) == []

    second = prepare(client, headers)
    second_token = token(client, headers, second["id"])
    barrier = Barrier(2)

    def race_cancel():
        with Session(engine) as db:
            barrier.wait()
            return cancel_proposal(db, session_id=session_id, proposal_id=second["id"])

    def race_confirm():
        with Session(engine) as db:
            barrier.wait()
            try:
                return confirm_proposal(db, session_id=session_id, proposal_id=second["id"],
                                        confirmation_token=second_token)
            except DomainError as error:
                return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(race_cancel)
        b = pool.submit(race_confirm)
        a.result(); b.result()
    with Session(engine) as db:
        status = db.get(Proposal, second["id"]).status
        assert status in {"cancelled", "executed"}
        assert count(db, TransferOrder) == (2 if status == "executed" else 1)
        assert check_ledger(db) == []


def test_competing_proposals_cannot_overdraw(demo):
    client, engine = demo
    _sid1, headers1 = login(client)
    with TestClient(app) as second:
        _sid2, headers2 = login(second)
        payload = {**TRANSFER, "amount_minor": 500_000}
        first = prepare(client, headers1, payload)
        other = prepare(second, headers2, payload)
        with Session(engine) as db:
            from test_security import mfa
            mfa(db, first["id"], sid=_sid1)
            mfa(db, other["id"], sid=_sid2)
        assert confirm(client, headers1, first["id"], token(client, headers1, first["id"])).status_code == 200
        denied = confirm(second, headers2, other["id"], token(second, headers2, other["id"]))
        assert denied.status_code == 409 and denied.json()["error_code"] == "STALE_RESOURCE"
    with Session(engine) as db:
        assert db.get(Account, "a1c").balance_minor == 500_000
        assert count(db, TransferOrder) == 1 and check_ledger(db) == []
