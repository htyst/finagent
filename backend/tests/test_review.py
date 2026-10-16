"""Post-M7 checks for real input and non-destructive upgrade regressions."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.config import ROOT
from app.data.cli import migrate
from app.data.db import get_db, make_engine
from app.data.models import Base, DemoSetting
from app.data.seed import reset_demo
from app.main import app

@pytest.fixture
def client(tmp_path):
    engine = make_engine(tmp_path / "review.sqlite3")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        reset_demo(db)
    def override():
        with Session(engine) as db:
            yield db
    app.dependency_overrides[get_db] = override
    try:
        with TestClient(app, raise_server_exceptions=False) as http:
            assert http.post("/api/demo/session", json={"user_id":"u1"}).status_code == 200
            yield http
    finally:
        app.dependency_overrides.clear()
        engine.dispose()

@pytest.mark.parametrize("endpoint", [
    "/api/transactions?end=9999-12-31", "/api/transactions?start=0001-01-01",
    "/api/spending?start=0001-01-01&end=0001-01-02&comparison_mode=none",
])
def test_date_overflow_returns_actionable_error(client, endpoint):
    response = client.get(endpoint)
    assert response.status_code == 400, response.text
    assert response.json()["error_code"] == "INVALID_INPUT"
    assert response.json()["message"]
    assert client.get("/api/accounts").status_code == 200

def test_request_validation_uses_contract_without_echoing_values(client):
    for response in [client.get("/api/transactions?limit=not-a-number"),
                     client.post("/api/demo/session", json={"user_id":"u1", "secret":"private-test-value"})]:
        assert response.status_code == 422
        data = response.json()
        assert data.get("error_code") == "INVALID_INPUT", data
        assert data["status"] == "error" and data["message"] and data["request_id"]
        assert "private-test-value" not in response.text

def test_upgrade_cli_preserves_old_database_and_is_repeatable(monkeypatch):
    (ROOT / "var").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="review-upgrade-", dir=ROOT / "var") as folder:
        path = Path(folder) / "legacy.sqlite3"
        monkeypatch.setenv("FINAGENT_DB_PATH", str(path))
        migrate()
        engine = make_engine(path)
        try:
            with Session(engine) as db:
                version = reset_demo(db)
                balances = db.execute(text("SELECT id, balance_minor FROM accounts ORDER BY id")).all()
            config = Config(str(ROOT / "backend/alembic.ini"))
            config.set_main_option("script_location", str(ROOT / "backend/migrations"))
            command.downgrade(config, "0002_m3_operations")
            for _ in range(2):
                result = subprocess.run([sys.executable, "-m", "app.data.cli", "migrate"],
                                        cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "backend")},
                                        capture_output=True, text=True, encoding="utf-8", errors="replace")
                assert result.returncode == 0, result.stderr
            with Session(engine) as db:
                assert db.get(DemoSetting, 1).dataset_version == version
                assert db.execute(text("SELECT id, balance_minor FROM accounts ORDER BY id")).all() == balances
                assert db.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0004_m5_plans"
                assert db.execute(text("SELECT count(*) FROM agent_tasks")).scalar() == 0
        finally:
            engine.dispose()
