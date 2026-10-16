"""补齐证据式源码清单中最高价值缺口的回归用例（G01/G02/G03/G04/G07/G10/G11/G15）。

本文件只断言当前真实行为；发现文档与实现漂移时，测试钉住实现并在测试文档串中记录漂移。
"""
from __future__ import annotations

import ast
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import ROOT
from app.data.cli import project_database
from app.data.db import get_db, make_engine
from app.data.models import (Account, AuditEvent, Base, BusinessResult, DemoSession, Proposal, Task,
                             Transaction, TransferOrder)
from app.data.seed import check_ledger, reset_demo
from app.domain.errors import DomainError
from app.domain.ledger import daily_transfer_outflow, post_entry
from app.domain.operations import get_current_task
from app.main import app

TRANSFER = {"source_account_id": "a1c", "target": {"type": "own_account", "account_id": "a1s"},
            "amount_minor": 10_000}
SAVINGS_TRANSFER = {"source_account_id": "a1s", "target": {"type": "own_account", "account_id": "a1c"},
                    "amount_minor": 200_000}
ALLOWED_ORIGIN = "http://testserver"


@pytest.fixture
def demo(tmp_path):
    engine = make_engine(tmp_path / "gap.sqlite3")
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
    return session_id, {"Origin": ALLOWED_ORIGIN, "X-FinAgent-CSRF": csrf.json()["data"]["csrf_token"]}


def prepare(client: TestClient, headers: dict, payload: dict | None = None) -> dict:
    response = client.post("/api/transfers/prepare", headers=headers, json=payload or TRANSFER)
    assert response.status_code == 200, response.json()
    return response.json()["data"]["proposal"]


def token(client: TestClient, headers: dict, proposal_id: str) -> str:
    response = client.post(f"/api/proposals/{proposal_id}/confirmation-token", headers=headers)
    assert response.status_code == 200, response.json()
    return response.json()["data"]["confirmation_token"]


def confirm(client: TestClient, headers: dict, proposal_id: str, value: str):
    return client.post(f"/api/proposals/{proposal_id}/confirm", headers=headers,
                       json={"confirmation_token": value})


def count(db: Session, model) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


def instant(text: str) -> datetime:
    return datetime.fromisoformat(text)


def test_health_and_demo_session_restore(demo):
    """G01: GET /api/health 返回 service/phase；GET /api/demo/session 未选用户 401、选后返回同一用户。"""
    client, _engine = demo
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["data"] == {"service": "finagent", "phase": "M6"}

    anonymous = client.get("/api/demo/session")
    assert anonymous.status_code == 401
    assert anonymous.json()["error_code"] == "SESSION_EXPIRED"

    users = {row["id"]: row["display_name"] for row in client.get("/api/demo/users").json()["data"]}
    assert client.post("/api/demo/session", json={"user_id": "u2"}).status_code == 200
    restored = client.get("/api/demo/session")
    assert restored.status_code == 200
    assert restored.json()["data"]["user"] == {"id": "u2", "display_name": users["u2"]}


def test_expired_demo_session_blocks_reads_and_writes(demo):
    """G04: 把 expires_at_utc 改到过去后，读接口与写接口都必须 401 SESSION_EXPIRED（8 小时有效期分支）。"""
    client, engine = demo
    assert client.post("/api/demo/session", json={"user_id": "u1"}).status_code == 200
    session_id = client.cookies.get("finagent_demo_session")
    csrf = client.get("/api/demo/csrf")
    headers = {"Origin": ALLOWED_ORIGIN, "X-FinAgent-CSRF": csrf.json()["data"]["csrf_token"]}
    assert client.get("/api/accounts").status_code == 200

    with Session(engine) as db:
        expires_at = datetime.fromisoformat(db.get(DemoSession, session_id).expires_at_utc)
    remaining = expires_at - datetime.now(timezone.utc)
    assert timedelta(hours=7, minutes=59) <= remaining <= timedelta(hours=8, minutes=1)

    proposal = prepare(client, headers)
    credential = token(client, headers, proposal["id"])

    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds")
    with Session(engine) as db:
        db.get(DemoSession, session_id).expires_at_utc = past
        db.commit()

    denied_read = client.get("/api/accounts")
    assert denied_read.status_code == 401 and denied_read.json()["error_code"] == "SESSION_EXPIRED"
    assert client.get("/api/demo/session").status_code == 401
    denied_write = client.post("/api/transfers/prepare", headers=headers, json=TRANSFER)
    assert denied_write.status_code == 401 and denied_write.json()["error_code"] == "SESSION_EXPIRED"
    denied_confirm = confirm(client, headers, proposal["id"], credential)
    assert denied_confirm.status_code == 401 and denied_confirm.json()["error_code"] == "SESSION_EXPIRED"
    with Session(engine) as db:
        with pytest.raises(DomainError) as error:
            get_current_task(db, session_id=session_id)
        assert error.value.code == "SESSION_EXPIRED" and error.value.http_status == 401
        assert count(db, TransferOrder) == count(db, BusinessResult) == 0

    # 只有到期时间这一个原因：把到期时间移回未来后同一 cookie 立即恢复可用。
    future = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(timespec="seconds")
    with Session(engine) as db:
        db.get(DemoSession, session_id).expires_at_utc = future
        db.commit()
    assert client.get("/api/accounts").status_code == 200


def test_cancelled_proposal_confirm_returns_invalid_proposal_state(demo):
    """G02: docs/API.md 列出的 PROPOSAL_CANCELLED 不存在；取消后确认真实返回 409 INVALID_PROPOSAL_STATE。"""
    client, engine = demo
    _session_id, headers = login(client)
    proposal = prepare(client, headers)
    credential = token(client, headers, proposal["id"])

    cancelled = client.post(f"/api/proposals/{proposal['id']}/cancel", headers=headers)
    assert cancelled.status_code == 200 and cancelled.json()["data"]["status"] == "cancelled"

    denied = confirm(client, headers, proposal["id"], credential)
    assert denied.status_code == 409
    assert denied.json()["error_code"] == "INVALID_PROPOSAL_STATE"
    assert denied.json()["error_code"] != "PROPOSAL_CANCELLED"
    assert client.post(f"/api/proposals/{proposal['id']}/confirmation-token",
                       headers=headers).json()["error_code"] == "INVALID_PROPOSAL_STATE"
    assert client.get(f"/api/proposals/{proposal['id']}").json()["data"]["status"] == "cancelled"
    with Session(engine) as db:
        assert db.get(Proposal, proposal["id"]).status == "cancelled"
        assert count(db, TransferOrder) == count(db, BusinessResult) == 0
        assert db.get(Account, "a1c").balance_minor == 1_000_000


def test_api_md_lists_error_code_no_backend_module_emits():
    """G02 漂移钉：docs/API.md 列出 PROPOSAL_CANCELLED，但 backend/app 下没有任何模块产生该码。"""
    api_md = (ROOT / "docs" / "API.md").read_text(encoding="utf-8")
    assert "PROPOSAL_CANCELLED" in api_md
    sources = sorted((ROOT / "backend" / "app").rglob("*.py"))
    assert sources
    for path in sources:
        assert "PROPOSAL_CANCELLED" not in path.read_text(encoding="utf-8"), path


def test_prepare_rejects_insufficient_funds_without_writes(demo):
    """G03: 准备阶段余额不足返回 INSUFFICIENT_FUNDS，且不写余额、流水、任务或操作单。"""
    client, engine = demo
    _session_id, headers = login(client)
    with Session(engine) as db:
        # 真实账本分录把 a1c 压到 5,000 分，保持 期初 + 净额 = 余额 不变。
        post_entry(db, owner_id="u1", account_id="a1c", kind="purchase", direction="debit",
                   amount_minor=995_000, business_at=instant("2026-09-24T04:00:00+00:00"),
                   category="购物", merchant="G03 余额压降")
        db.commit()
    with Session(engine) as db:
        assert db.get(Account, "a1c").balance_minor == 5_000
        assert check_ledger(db) == []
        before = (count(db, Transaction), count(db, Proposal), count(db, Task), count(db, TransferOrder))

    denied = client.post("/api/transfers/prepare", headers=headers, json=TRANSFER)
    assert denied.status_code == 400
    assert denied.json()["status"] == "error" and denied.json()["error_code"] == "INSUFFICIENT_FUNDS"

    with Session(engine) as db:
        assert (count(db, Transaction), count(db, Proposal), count(db, Task), count(db, TransferOrder)) == before
        assert db.get(Account, "a1c").balance_minor == 5_000
        assert db.get(Account, "a1s").balance_minor == 2_000_000
        assert check_ledger(db) == []
        # 领域层 post_entry 的同余额分支同样拒绝且不落行。
        with pytest.raises(DomainError) as error:
            post_entry(db, owner_id="u1", account_id="a1c", kind="purchase", direction="debit",
                       amount_minor=10_000, business_at=instant("2026-09-24T04:00:00+00:00"))
        assert error.value.code == "INSUFFICIENT_FUNDS"
        db.rollback()
        assert count(db, Transaction) == before[0]


def test_confirm_rejects_insufficient_funds_without_business_writes(demo):
    """G03: 确认阶段余额不足返回 INSUFFICIENT_FUNDS，操作单转 failed，且不扣款、不写流水与订单。"""
    client, engine = demo
    _session_id, headers = login(client)
    proposal = prepare(client, headers)
    credential = token(client, headers, proposal["id"])

    with Session(engine) as db:
        # 只压降余额、不动 version，确保失败来自余额校验而不是快照校验（STALE_RESOURCE）。
        db.get(Account, "a1c").balance_minor = 5_000
        db.commit()
        before_transactions = count(db, Transaction)

    denied = confirm(client, headers, proposal["id"], credential)
    # operations.confirm_proposal 里该 DomainError 未显式指定状态码，走 errors.DomainError 默认 http_status=400
    # （不是同类状态错误 PROPOSAL_EXPIRED / INVALID_PROPOSAL_STATE 的 409）。
    assert denied.status_code == 400
    assert denied.json()["error_code"] == "INSUFFICIENT_FUNDS"

    with Session(engine) as db:
        assert db.get(Proposal, proposal["id"]).status == "failed"
        assert db.get(Account, "a1c").balance_minor == 5_000
        assert db.get(Account, "a1s").balance_minor == 2_000_000
        assert count(db, Transaction) == before_transactions
        assert count(db, TransferOrder) == count(db, BusinessResult) == 0
        assert db.scalar(select(func.count()).select_from(Transaction)
                         .where(Transaction.business_id == proposal["id"])) == 0
        event = db.scalar(select(AuditEvent).where(AuditEvent.proposal_id == proposal["id"],
                                                   AuditEvent.event == "failed"))
        assert event is not None and event.detail_code == "INSUFFICIENT_FUNDS"


def test_http_layer_reports_proposal_expired_after_token_window(demo):
    """G07: 真实时钟 5 分钟窗口过后，confirmation-token 与 confirm 两个路由都返回 409 PROPOSAL_EXPIRED。"""
    client, engine = demo
    _session_id, headers = login(client)
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()

    first = prepare(client, headers)
    with Session(engine) as db:
        db.get(Proposal, first["id"]).expires_at_utc = past
        db.commit()
    denied_token = client.post(f"/api/proposals/{first['id']}/confirmation-token", headers=headers)
    assert denied_token.status_code == 409 and denied_token.json()["error_code"] == "PROPOSAL_EXPIRED"
    assert client.get(f"/api/proposals/{first['id']}").json()["data"]["status"] == "expired"
    again = client.post(f"/api/proposals/{first['id']}/confirmation-token", headers=headers)
    assert again.status_code == 409 and again.json()["error_code"] == "INVALID_PROPOSAL_STATE"

    second = prepare(client, headers)
    credential = token(client, headers, second["id"])
    with Session(engine) as db:
        db.get(Proposal, second["id"]).expires_at_utc = past
        db.commit()
    denied_confirm = confirm(client, headers, second["id"], credential)
    assert denied_confirm.status_code == 409 and denied_confirm.json()["error_code"] == "PROPOSAL_EXPIRED"
    assert client.get(f"/api/proposals/{second['id']}").json()["data"]["status"] == "expired"
    with Session(engine) as db:
        assert db.get(Proposal, second["id"]).status == "expired"
        assert db.get(Account, "a1c").balance_minor == 1_000_000
        assert count(db, TransferOrder) == count(db, BusinessResult) == 0


def test_daily_transfer_cap_uses_shanghai_midnight_window(demo):
    """G10: 每日额度按上海日切左闭右开 [local_midnight, +1 day) 统计；次日 00:00:00 归入新的一天。"""
    client, engine = demo
    _session_id, headers = login(client)
    with Session(engine) as db:
        for index, (moment, amount) in enumerate([
            ("2026-09-23T16:00:00+00:00", 400_000),  # 上海 2026-09-24 00:00:00 -> 当天窗口左端，含
            ("2026-09-24T15:59:59+00:00", 400_000),  # 上海 2026-09-24 23:59:59 -> 当天窗口内
            ("2026-09-24T16:00:00+00:00", 200_000),  # 上海 2026-09-25 00:00:00 -> 右端不含，算次日
            ("2026-09-23T15:59:59+00:00", 100_000),  # 上海 2026-09-23 23:59:59 -> 早于左端，不算当天
        ]):
            post_entry(db, owner_id="u1", account_id="a1s", kind="external_transfer", direction="debit",
                       amount_minor=amount, business_at=instant(moment),
                       business_id=f"g10_boundary_{index}", merchant="G10 边界")
        db.commit()
        assert db.get(Account, "a1s").balance_minor == 900_000
        assert daily_transfer_outflow(db, "a1s", date(2026, 9, 24)) == 800_000
        assert daily_transfer_outflow(db, "a1s", date(2026, 9, 25)) == 200_000
        assert daily_transfer_outflow(db, "a1s", date(2026, 9, 23)) == 100_000
        assert check_ledger(db) == []

    denied = client.post("/api/transfers/prepare", headers=headers,
                         json={**SAVINGS_TRANSFER, "amount_minor": 200_001})
    assert denied.status_code == 400 and denied.json()["error_code"] == "LIMIT_EXCEEDED"
    allowed = client.post("/api/transfers/prepare", headers=headers, json=SAVINGS_TRANSFER)
    assert allowed.status_code == 200, allowed.json()
    assert allowed.json()["data"]["proposal"]["summary"]["amount_minor"] == 200_000
    with Session(engine) as db:
        # 800000 + 200000 正好等于每日上限（money.py 用 > 判断），且准备不写业务流水。
        assert daily_transfer_outflow(db, "a1s", date(2026, 9, 24)) == 800_000
        assert db.get(Account, "a1s").balance_minor == 900_000
        assert count(db, TransferOrder) == 0


def test_spending_evidence_offset_pages_do_not_overlap(demo):
    """G11: 餐饮类别 + 自定义区间下 evidence_offset>0 翻页：页不重叠，并集等于不分页证据集。"""
    client, _engine = demo
    login(client)
    filters = {"category": "餐饮", "start": "2026-09-01", "end": "2026-09-30", "comparison_mode": "none"}
    data = client.get("/api/spending", params={**filters, "evidence_limit": 100}).json()["data"]
    assert data["comparison"] is None
    full = data["current"]
    assert full["evidence"]["total"] == 3
    full_ids = [item["transaction_id"] for item in full["evidence"]["items"]]
    assert full_ids == ["t_refund_50", "t_food_250", "t_food_1000"]

    pages: list[list[dict]] = []
    for offset in range(3):
        page = client.get("/api/spending", params={**filters, "evidence_limit": 1,
                                                  "evidence_offset": offset}).json()["data"]["current"]
        assert page["evidence"]["offset"] == offset and page["evidence"]["limit"] == 1
        assert page["evidence"]["total"] == 3
        assert page["totals"] == full["totals"]
        pages.append(page["evidence"]["items"])

    assert [len(items) for items in pages] == [1, 1, 1]
    paged_ids = [item["transaction_id"] for items in pages for item in items]
    assert paged_ids == full_ids
    assert len(set(paged_ids)) == 3
    assert sum(item["signed_minor"] for items in pages for item in items) == 120_000
    assert sum(item["signed_minor"] for item in full["evidence"]["items"]) == 120_000

    beyond = client.get("/api/spending", params={**filters, "evidence_limit": 1,
                                                "evidence_offset": 3}).json()["data"]["current"]
    assert beyond["evidence"]["items"] == [] and beyond["evidence"]["total"] == 3

    # 自由文本带来的未知类别只过滤出空集，不报错也不回退为全部类别。
    unknown = client.get("/api/spending", params={"category": "教育"}).json()["data"]
    assert unknown["current"]["totals"] == {"purchase_minor": 0, "refund_minor": 0, "net_minor": 0,
                                            "purchase_count": 0, "refund_count": 0, "is_complete": True}
    assert unknown["current"]["evidence"]["total"] == 0
    assert unknown["comparison"]["percent_change"] is None
    assert unknown["comparison"]["percent_change_reason"] == "BASELINE_NONPOSITIVE"
    listed = client.get("/api/transactions", params={"category": "教育"}).json()["data"]
    assert listed["total"] == 0 and listed["items"] == []


def test_upgrade_regression_uses_project_var_temp_dir(monkeypatch, tmp_path):
    """G15: test_review 的迁移用例把临时目录建在项目 var/ 内（与 API.md“测试只用临时路径”不一致），
    因为 app.data.cli.project_database() 只接受项目 var/ 下的数据库路径。"""
    source = (ROOT / "backend" / "tests" / "test_review.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "test_upgrade_cli_preserves_old_database_and_is_repeatable")
    calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and node.func.attr == "TemporaryDirectory"]
    assert len(calls) == 1
    dir_argument = next(keyword.value for keyword in calls[0].keywords if keyword.arg == "dir")
    assert ast.get_source_segment(source, dir_argument) == 'ROOT / "var"'

    var_dir = (ROOT / "var").resolve()
    assert var_dir.is_relative_to(ROOT.resolve())

    # 注意：不能拿 tempfile.gettempdir() 当“项目外”的代表 —— 测试运行器会把 TEMP 指向
    # 本次报告目录（可能就在项目内），那时“系统临时目录”反而落在 var/ 里。
    # 用项目根的同级目录构造一个一定在项目之外的路径，才能稳定验证这条例外。
    outside_dir = ROOT.resolve().parent / "g15-outside-probe"
    assert not (outside_dir / "outside.sqlite3").is_relative_to(ROOT.resolve())
    monkeypatch.setenv("FINAGENT_DB_PATH", str(outside_dir / "outside.sqlite3"))
    with pytest.raises(SystemExit):
        project_database()
    inside = var_dir / "g15-probe" / "inside.sqlite3"
    monkeypatch.setenv("FINAGENT_DB_PATH", str(inside))
    assert project_database() == inside.resolve()
