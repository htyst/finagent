import os
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.data.models import Base, DemoSession, DemoSetting
from app.data import security_models  # noqa: F401
from app.data.db import make_engine
from app.data.seed import reset_demo, check_ledger

# Every bank fixture creates a full database (~38 MB), so an unbounded directory per
# test reached tens of GB and filled the disk mid-suite. Runs are now scoped and only
# the newest few are retained; raise FINAGENT_TEST_KEEP_RUNS to keep more history.
_RUN_ROOT = Path(__file__).resolve().parents[2] / 'var' / 'TEST-ONLY' / 'pytest'
_RUN_ID = uuid4().hex


def _keep_runs() -> int:
    try:
        return max(1, int(os.getenv('FINAGENT_TEST_KEEP_RUNS', '1')))
    except ValueError:
        return 1


def _prune_previous_runs(keep: int) -> None:
    try:
        runs = sorted((path for path in _RUN_ROOT.iterdir() if path.is_dir()),
                      key=lambda path: path.stat().st_mtime, reverse=True)
    except (FileNotFoundError, PermissionError, OSError):
        return
    for stale in runs[keep:]:
        shutil.rmtree(stale, ignore_errors=True)


def pytest_sessionstart(session):
    _prune_previous_runs(_keep_runs())


def pytest_sessionfinish(session, exitstatus):
    # A green run has nothing worth keeping; a failing run keeps its artifacts.
    if exitstatus == 0:
        shutil.rmtree(_RUN_ROOT / _RUN_ID, ignore_errors=True)


@pytest.fixture
def tmp_path():
    """Retain isolated test artifacts without pytest's Windows chmod/ACL path.

    The managed workspace permits normal directory creation but not pytest's
    private-permission temp directories. No production paths are touched.
    """
    path = _RUN_ROOT / _RUN_ID / uuid4().hex
    path.mkdir(parents=True)
    return path


@pytest.fixture
def bank(tmp_path, monkeypatch):
    # Historical behavior contracts retain their legacy semantic substitutes.
    # Dynamic-engine tests explicitly select the production-default engine.
    monkeypatch.setenv('FINAGENT_AGENT_ENGINE','legacy')
    import app.config
    monkeypatch.setattr(app.config, 'ROOT', tmp_path)
    engine = make_engine(tmp_path / 'TEST-ONLY' / 'test.sqlite3')
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        reset_demo(db, date(2026, 9, 24))
        setting = db.get(DemoSetting, 1)
        for sid, owner in [('s1', 'u1'), ('s2', 'u1'), ('other', 'u2')]:
            db.add(DemoSession(id=sid, owner_id=owner, dataset_version=setting.dataset_version,
                expires_at_utc=(datetime.now(timezone.utc) + timedelta(days=2)).isoformat(), csrf_token='test-csrf'))
        db.commit()
        yield db, factory
        assert check_ledger(db) == []
    engine.dispose()
