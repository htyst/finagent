from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.db import get_db, make_engine
from app.data.models import Account, Base, Transaction
from app.data.seed import reset_demo
from app.domain.clock import Clock, current_month_range, last_night_range, previous_corresponding_range, selected_range
from app.domain.errors import DomainError
from app.domain.ledger import (daily_transfer_outflow, ledger_errors, owned_account, post_entry,
                               post_external_transfer, post_internal_transfer, post_refund)
from app.domain.money import require_cny, validate_minor, validate_transfer_amount, yuan_to_minor
from app.domain.spending import summarize_spending
from app.main import app

SHANGHAI = ZoneInfo("Asia/Shanghai")


def at(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 12, tzinfo=SHANGHAI)


@pytest.fixture
def demo(tmp_path):
    engine = make_engine(tmp_path / "m2.sqlite3")
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


def assert_code(exc: DomainError, code: str):
    assert exc.code == code


def test_money_and_two_clocks():
    assert yuan_to_minor("500.00") == 50_000
    assert yuan_to_minor(Decimal("0.01")) == 1
    assert validate_minor(1) == 1
    for value in (True, 1.0, "1", 0, -1, 2**63):
        with pytest.raises(DomainError):
            validate_minor(value)
    for value in ("1.234", "-1", "1e2", 1.0, True, "92233720368547759"):
        with pytest.raises(DomainError):
            yuan_to_minor(value)
    with pytest.raises(DomainError) as error:
        require_cny("USD")
    assert_code(error.value, "UNSUPPORTED_CURRENCY")
    with pytest.raises(DomainError) as error:
        validate_transfer_amount(500_001)
    assert_code(error.value, "LIMIT_EXCEEDED")

    frozen = Clock(date(2026, 9, 24), now=lambda: datetime(2026, 10, 2, 4, tzinfo=timezone.utc))
    assert frozen.business_now().date() == date(2026, 9, 24)
    assert frozen.system_now().date() == date(2026, 10, 2)
    assert last_night_range(date(2026, 9, 24)) == ("2026-09-23T10:00:00+00:00", "2026-09-23T22:00:00+00:00")
    assert current_month_range(date(2026, 3, 31)).end_exclusive == date(2026, 4, 1)
    assert previous_corresponding_range(date(2026, 3, 31)).end_exclusive == date(2026, 3, 1)
    with pytest.raises(DomainError):
        selected_range("2026-02-30", "2026-03-01")


def test_seed_ledger_refund_pair_limits_and_ownership(demo):
    _client, engine = demo
    with Session(engine) as db:
        assert ledger_errors(db) == []
        assert daily_transfer_outflow(db, "a1c", date(2026, 9, 11)) == 50_000
        assert daily_transfer_outflow(db, "a1c", date(2026, 9, 24)) == 0
        with pytest.raises(DomainError) as error:
            owned_account(db, "u2", "a1c")
        assert_code(error.value, "RESOURCE_NOT_FOUND")
        debit, credit = post_internal_transfer(db, owner_id="u1", source_id="a1c", target_id="a1s",
                                               amount_minor=10_000, business_at=at(2026, 9, 24), business_id="test_pair")
        assert debit.amount_minor == credit.amount_minor == 10_000
        assert debit.business_id == credit.business_id == "test_pair"
        assert db.get(Account, "a1c").balance_minor == 990_000
        assert db.get(Account, "a1s").balance_minor == 2_010_000
        assert daily_transfer_outflow(db, "a1c", date(2026, 9, 24)) == 10_000
        assert ledger_errors(db) == []
        refund = post_refund(db, owner_id="u1", original_id="t_food_1000", amount_minor=80_000,
                             business_at=at(2026, 9, 24))
        assert refund.category == "餐饮" and refund.original_transaction_id == "t_food_1000"
        assert db.get(Account, "a1c").balance_minor == 1_070_000
        with pytest.raises(DomainError) as error:
            post_refund(db, owner_id="u1", original_id="t_food_1000", amount_minor=20_000,
                        business_at=at(2026, 9, 24))
        assert_code(error.value, "REFUND_LIMIT_EXCEEDED")
        assert db.get(Account, "a1c").balance_minor == 1_070_000
        assert ledger_errors(db) == []
        db.commit()


def test_pending_void_frozen_card_and_external_transfer(demo):
    _client, engine = demo
    with Session(engine) as db:
        before = db.get(Account, "a1c").balance_minor
        post_entry(db, owner_id="u1", account_id="a1c", kind="purchase", direction="debit",
                   amount_minor=2_000, business_at=at(2026, 9, 24), status="pending", card_id="c1a")
        post_entry(db, owner_id="u1", account_id="a1c", kind="purchase", direction="debit",
                   amount_minor=2_000, business_at=at(2026, 9, 24), status="void", card_id="c1a")
        assert db.get(Account, "a1c").balance_minor == before
        with pytest.raises(DomainError) as error:
            post_entry(db, owner_id="u3", account_id="a3c", kind="purchase", direction="debit",
                       amount_minor=100, business_at=at(2026, 9, 24), card_id="c3a")
        assert_code(error.value, "INVALID_CARD_STATE")
        post_refund(db, owner_id="u3", original_id="t_u3_transport", amount_minor=1_200,
                    business_at=at(2026, 9, 24))
        assert db.get(Account, "a3c").balance_minor == 761_200
        external = post_external_transfer(db, owner_id="u1", source_id="a1c", recipient_id="r1",
                                          amount_minor=50_000, business_at=at(2026, 9, 24), business_id="test_ext")
        assert external.type == "external_transfer"
        assert db.get(Account, "a1c").balance_minor == 950_000
        assert ledger_errors(db) == []
        db.commit()


def test_spending_hand_calculated_refunds_and_evidence_pagination(demo):
    client, engine = demo
    client.post("/api/demo/session", json={"user_id": "u1"})
    data = client.get("/api/spending?category=餐饮&evidence_limit=1").json()["data"]
    now = data["current"]
    assert now["coverage"]["status"] == "complete"
    assert now["totals"] == {"purchase_minor": 125_000, "refund_minor": 5_000, "net_minor": 120_000,
                              "purchase_count": 2, "refund_count": 1, "is_complete": True}
    assert now["evidence"]["total"] == 3
    assert len(now["evidence"]["items"]) == 1
    assert data["comparison"]["totals"]["net_minor"] == 80_000
    assert data["comparison"]["delta_minor"] == 40_000
    assert data["comparison"]["percent_change"] == "50.0"
    assert data["rules_version"] == "spending-v2-cash-and-credit"
    all_evidence = client.get("/api/spending?category=餐饮&evidence_limit=10").json()["data"]["current"]["evidence"]["items"]
    assert {item["transaction_id"] for item in all_evidence} == {"t_food_1000", "t_food_250", "t_refund_50"}
    assert sum(item["signed_minor"] for item in all_evidence) == 120_000
    with Session(engine) as db:
        assert db.get(Transaction, "t_internal_out").id not in {item["transaction_id"] for item in all_evidence}


def test_spending_zero_incomplete_uncategorized_and_boundaries(demo):
    client, engine = demo
    client.post("/api/demo/session", json={"user_id": "u1"})
    zero = client.get("/api/spending?account_id=a1s&category=餐饮").json()["data"]
    assert zero["current"]["coverage"]["status"] == "complete"
    assert zero["current"]["totals"]["net_minor"] == 0
    assert zero["comparison"]["percent_change"] is None
    assert zero["comparison"]["percent_change_reason"] == "BASELINE_NONPOSITIVE"
    uncategorized = client.get("/api/spending?category=未分类&comparison_mode=none").json()["data"]
    assert uncategorized["current"]["totals"]["net_minor"] == 3_200
    boundary = client.get("/api/spending?start=2026-09-08&end=2026-09-08&category=餐饮").json()["data"]
    assert boundary["current"]["totals"]["net_minor"] == 100_000
    assert boundary["current"]["evidence"]["total"] == 1
    assert boundary["comparison"] is None
    cross_month = client.get("/api/spending?start=2026-08-20&end=2026-09-08&category=餐饮").json()["data"]
    assert cross_month["current"]["totals"]["net_minor"] == 100_000
    assert client.get("/api/spending?account_id=a2c").status_code == 404
    assert client.get("/api/spending?start=2026-09-25&end=2026-09-24").json()["error_code"] == "INVALID_INPUT"
    with Session(engine) as db:
        incomplete = summarize_spending(db, owner_id="u3", account_id="a3s")
        assert incomplete["current"]["coverage"]["status"] == "complete"
        assert incomplete["comparison"]["coverage"]["status"] == "incomplete"
        assert incomplete["comparison"]["delta_minor"] is None
        assert incomplete["comparison"]["percent_change_reason"] == "DATA_INCOMPLETE"
        outside = summarize_spending(db, owner_id="u1", start="2026-05-01", end="2026-05-31")
        assert outside["current"]["coverage"]["status"] == "incomplete"
        assert outside["current"]["totals"]["is_complete"] is False


def test_negative_baseline_and_refund_period(demo):
    _client, engine = demo
    with Session(engine) as db:
        original = post_entry(db, owner_id="u1", account_id="a1c", kind="purchase", direction="debit",
                              amount_minor=100_000, business_at=at(2026, 6, 15), category="餐饮", card_id="c1a")
        post_refund(db, owner_id="u1", original_id=original.id, amount_minor=95_000,
                    business_at=at(2026, 8, 15))
        result = summarize_spending(db, owner_id="u1", category="餐饮")
        assert result["current"]["totals"]["net_minor"] == 120_000
        assert result["comparison"]["totals"]["net_minor"] == -15_000
        assert result["comparison"]["delta_minor"] == 135_000
        assert result["comparison"]["percent_change"] is None
        assert result["comparison"]["percent_change_reason"] == "BASELINE_NONPOSITIVE"
        assert ledger_errors(db) == []

def test_daily_limit_and_internal_pair_integrity(demo):
    _client, engine = demo
    with Session(engine) as db:
        for sequence in (1, 2):
            post_internal_transfer(db, owner_id="u1", source_id="a1s", target_id="a1c",
                                   amount_minor=500_000, business_at=at(2026, 9, 24),
                                   business_id=f"limit_pair_{sequence}")
        assert daily_transfer_outflow(db, "a1s", date(2026, 9, 24)) == 1_000_000
        before = db.get(Account, "a1s").balance_minor
        with pytest.raises(DomainError) as error:
            post_internal_transfer(db, owner_id="u1", source_id="a1s", target_id="a1c",
                                   amount_minor=1, business_at=at(2026, 9, 24))
        assert_code(error.value, "LIMIT_EXCEEDED")
        assert db.get(Account, "a1s").balance_minor == before
        assert ledger_errors(db) == []
        post_entry(db, owner_id="u1", account_id="a1c", kind="internal_transfer", direction="debit",
                   amount_minor=100, business_at=at(2026, 9, 24), business_id="orphan")
        assert "internal_pair:orphan" in ledger_errors(db)


def test_current_negative_net_and_half_up_percentage(demo):
    _client, engine = demo
    with Session(engine) as db:
        from app.data.card_models import CardControl
        db.get(CardControl, "c1a").limit_minor = 200_000
        original = post_entry(db, owner_id="u1", account_id="a1c", kind="purchase", direction="debit",
                              amount_minor=200_000, business_at=at(2026, 8, 5), category="餐饮", card_id="c1a")
        post_refund(db, owner_id="u1", original_id=original.id, amount_minor=200_000,
                    business_at=at(2026, 9, 20))
        result = summarize_spending(db, owner_id="u1", category="餐饮")
        assert result["current"]["totals"]["net_minor"] == -80_000
        assert result["comparison"]["totals"]["net_minor"] == 280_000
        assert result["comparison"]["delta_minor"] == -360_000
        assert result["comparison"]["percent_change"] == "-128.6"
        assert ledger_errors(db) == []

def test_full_month_and_custom_comparison_ranges(demo):
    _client, engine = demo
    with Session(engine) as db:
        post_entry(db, owner_id="u1", account_id="a1c", kind="purchase", direction="debit",
                   amount_minor=3_000, business_at=at(2026, 8, 28), category="餐饮", card_id="c1a")
        default = summarize_spending(db, owner_id="u1", category="餐饮")
        assert default["comparison"]["totals"]["net_minor"] == 80_000
        full = summarize_spending(db, owner_id="u1", category="餐饮", comparison_mode="previous_full_month")
        assert full["comparison"]["period"]["end_exclusive_date"] == "2026-09-01"
        assert full["comparison"]["totals"]["net_minor"] == 83_000
        assert full["comparison"]["delta_minor"] == 37_000
        assert full["comparison"]["percent_change"] == "44.6"
        custom = summarize_spending(db, owner_id="u1", category="餐饮",
                                    start="2026-09-01", end="2026-09-24",
                                    comparison_mode="custom", compare_start="2026-08-01", compare_end="2026-08-24")
        assert custom["comparison"]["totals"]["net_minor"] == 80_000
        with pytest.raises(DomainError) as error:
            summarize_spending(db, owner_id="u1", start="2026-09-01", end="2026-09-24",
                               comparison_mode="previous_corresponding")
        assert_code(error.value, "INVALID_INPUT")


def test_ledger_detects_cross_user_card_and_credit_overflow(demo):
    _client, engine = demo
    with Session(engine) as db:
        with pytest.raises(DomainError) as error:
            post_entry(db, owner_id="u1", account_id="a1c", kind="income", direction="credit",
                       amount_minor=2**63 - 1, business_at=at(2026, 9, 24))
        assert_code(error.value, "INVALID_INPUT")
        with pytest.raises(DomainError) as error:
            post_internal_transfer(db, owner_id="u1", source_id="a1c", target_id="a1s",
                                   amount_minor=100, business_at=datetime(2026, 9, 24, 12))
        assert_code(error.value, "INVALID_INPUT")
        with pytest.raises(DomainError) as error:
            post_external_transfer(db, owner_id="u1", source_id="a1c", recipient_id="r1",
                                   amount_minor=100, business_at=datetime(2026, 9, 24, 12))
        assert_code(error.value, "INVALID_INPUT")
        db.get(Transaction, "t_food_250").card_id = "c2a"
        db.flush()
        assert "transaction_card:t_food_250" in ledger_errors(db)


@pytest.mark.parametrize("demo_date", [date(2026, 9, 1), date(2026, 9, 10), date(2026, 9, 17),
                                       date(2026, 9, 24), date(2026, 1, 1), date(2026, 2, 28),
                                       date(2024, 2, 29), date(2026, 3, 31)])
def test_seed_dates_and_visible_ledger(demo, demo_date):
    client, engine = demo
    cutoff = datetime.combine(demo_date + timedelta(days=1), datetime.min.time(), SHANGHAI)
    with Session(engine) as db:
        reset_demo(db, demo_date)
        rows = db.scalars(select(Transaction)).all()
        assert all(datetime.fromisoformat(row.posted_at_utc) < cutoff for row in rows)
        assert ledger_errors(db) == []
        by_id = {row.id: row for row in rows}
        for refund in (row for row in rows if row.type == "refund"):
            original = by_id[refund.original_transaction_id]
            assert original.posted_at_utc <= refund.posted_at_utc
        assert db.get(Transaction, "t_unknown_now").posted_at_utc == datetime.combine(
            demo_date - timedelta(days=1), datetime.min.time().replace(hour=21, minute=10), SHANGHAI
        ).astimezone(timezone.utc).isoformat(timespec="seconds")

    for owner in ("u1", "u2", "u3"):
        client.post("/api/demo/session", json={"user_id": owner})
        visible = client.get("/api/transactions?limit=100").json()["data"]["items"]
        with Session(engine) as db:
            for account in db.scalars(select(Account).where(Account.owner_id == owner)):
                delta = sum((row["amount_minor"] if row["direction"] == "credit" else -row["amount_minor"])
                            for row in visible if row["account_id"] == account.id and row["status"] == "posted")
                assert account.opening_balance_minor + delta == account.balance_minor

    if demo_date == date(2026, 9, 10):
        client.post("/api/demo/session", json={"user_id": "u1"})
        result = client.get("/api/spending?category=餐饮").json()["data"]
        assert result["current"]["totals"]["net_minor"] == 100_000
        assert {row["transaction_id"] for row in result["current"]["evidence"]["items"]} == {"t_food_1000"}


def test_transaction_categories_match_spending(demo):
    client, _engine = demo
    client.post("/api/demo/session", json={"user_id": "u1"})
    page = client.get("/api/transactions", params={"category": "未分类", "limit": 100}).json()["data"]
    purchases = [row for row in page["items"] if row["type"] == "purchase" and row["status"] == "posted"]
    assert {row["id"] for row in purchases} == {"t_uncategorized"}
    assert sum(row["amount_minor"] for row in purchases) == 3_200
    summary = client.get("/api/spending", params={"category": "未分类"}).json()["data"]
    assert summary["current"]["totals"]["net_minor"] == 3_200
    unfiltered = client.get("/api/transactions?limit=100").json()["data"]
    all_categories = client.get("/api/transactions", params={"category": "全部", "limit": 100}).json()["data"]
    assert all_categories == unfiltered
    client.post("/api/demo/session", json={"user_id": "u2"})
    other_user = client.get("/api/transactions", params={"category": "未分类"}).json()["data"]
    assert {row["id"] for row in other_user["items"]} == {"t_u2_salary"}
