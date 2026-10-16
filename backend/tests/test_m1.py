from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.db import get_db, make_engine
from app.data.models import Account, Base, DemoSetting, Transaction
from app.data.seed import check_ledger, reset_demo
from app.main import app


@pytest.fixture
def demo(tmp_path):
    engine = make_engine(tmp_path / "test.sqlite3")
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


def test_seed_is_reproducible_and_balanced(demo):
    client, engine = demo
    with Session(engine) as db:
        first_version = db.get(DemoSetting, 1).dataset_version
        assert check_ledger(db) == []
        assert db.get(Account, "a1c").balance_minor == 1_000_000
        assert db.get(Account, "a1s").balance_minor == 2_000_000
        assert db.get(Transaction, "t_unknown_now").card_id == "c1a"
        first = [(a.id, a.balance_minor, a.opening_balance_minor) for a in db.scalars(select(Account).order_by(Account.id))]
        second_version = reset_demo(db, date(2026, 9, 24))
        second = [(a.id, a.balance_minor, a.opening_balance_minor) for a in db.scalars(select(Account).order_by(Account.id))]
        assert first == second
        assert first_version != second_version
        assert check_ledger(db) == []
    assert client.get("/api/demo/info").json()["data"]["demo_date"] == "2026-09-24"


def test_session_owner_scopes_accounts_cards_and_paginated_transactions(demo):
    client, _engine = demo
    assert client.get("/api/accounts").status_code == 401
    users = client.get("/api/demo/users").json()["data"]
    assert len(users) == 3
    assert client.post("/api/demo/session", json={"user_id": "u1"}).status_code == 200
    accounts = client.get("/api/accounts").json()["data"]
    assert {a["id"] for a in accounts} == {"a1c", "a1s"}
    assert client.get("/api/cards").json()["data"][0]["card_mask"] == "**1234"
    page = client.get("/api/transactions?limit=2&offset=0").json()["data"]
    assert len(page["items"]) == 2 and page["total"] > 2
    assert all(t["account_id"] in {"a1c", "a1s"} for t in page["items"])
    assert client.get("/api/transactions?account_id=a2c").status_code == 404
    assert client.get("/api/transactions?card_id=c2a").status_code == 404
    assert [r["id"] for r in client.get("/api/recipients?q=张三").json()["data"]] == ["r1", "r2"]

    old_cookie = client.cookies.get("finagent_demo_session")
    assert client.post("/api/demo/session", json={"user_id": "u2"}).status_code == 200
    assert {a["id"] for a in client.get("/api/accounts").json()["data"]} == {"a2c", "a2s"}
    assert all(t["account_id"].startswith("a2") for t in client.get("/api/transactions").json()["data"]["items"])
    with TestClient(app) as stale:
        stale.cookies.set("finagent_demo_session", old_cookie)
        assert stale.get("/api/accounts").status_code == 401


def test_reset_invalidates_session_and_date_filter(demo):
    client, engine = demo
    client.post("/api/demo/session", json={"user_id": "u1"})
    day = client.get("/api/transactions?start=2026-09-23&end=2026-09-23").json()["data"]
    assert [t["id"] for t in day["items"]] == ["t_unknown_now"]
    assert client.get("/api/transactions?start=2026-09-24&end=2026-09-23").status_code == 400
    with Session(engine) as db:
        reset_demo(db)
    assert client.get("/api/accounts").status_code == 401


def test_early_demo_date_contains_no_future_posted_business(demo):
    client, engine = demo
    with Session(engine) as db:
        reset_demo(db, date(2026, 9, 10))
        posted = db.scalars(select(Transaction).where(Transaction.status == "posted")).all()
        assert posted
        assert all(item.posted_at_utc < "2026-09-10T16:00:00+00:00" for item in posted)
        for missing in ("t_food_250", "t_refund_50", "t_internal_out", "t_internal_in"):
            assert db.get(Transaction, missing) is None
        for account in db.scalars(select(Account)):
            net = sum(
                item.amount_minor if item.direction == "credit" else -item.amount_minor
                for item in posted if item.account_id == account.id
            )
            assert account.opening_balance_minor + net == account.balance_minor
        assert check_ledger(db) == []
    client.post("/api/demo/session", json={"user_id": "u1"})
    rows = client.get("/api/transactions").json()["data"]["items"]
    assert all(row["posted_at_utc"] < "2026-09-10T16:00:00+00:00" for row in rows if row["status"] == "posted")


def test_uncategorized_transaction_filter_matches_display_and_spending(demo):
    client, _engine = demo
    client.post("/api/demo/session", json={"user_id": "u1"})
    transactions = client.get("/api/transactions", params={"category": "未分类"}).json()["data"]
    assert transactions["total"] == 5
    assert "t_uncategorized" in {item["id"] for item in transactions["items"]}
    assert all(item["category"] == "未分类" for item in transactions["items"])
    spending = client.get("/api/spending", params={"category": "未分类"}).json()["data"]
    assert spending["current"]["totals"]["net_minor"] == 3200
    assert [item["transaction_id"] for item in spending["current"]["evidence"]["items"]] == ["t_uncategorized"]
