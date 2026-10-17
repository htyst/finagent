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


@pytest.fixture
def tmp_path():
    """Retain isolated test artifacts without pytest's Windows chmod/ACL path.

    The managed workspace permits normal directory creation but not pytest's
    private-permission temp directories. No production paths are touched.
    """
    path = Path(__file__).resolve().parents[2] / 'var' / 'TEST-ONLY' / 'pytest' / uuid4().hex
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
