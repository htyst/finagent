"""Fresh install and incremental upgrade must preserve financial rows."""
from datetime import date
from pathlib import Path
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session
from app.data.db import make_engine
from app.data.models import Account
from app.data.seed import reset_demo, check_ledger


def test_upgrade_from_0012_preserves_balances(tmp_path, monkeypatch):
    path = tmp_path / 'migration-TEST-ONLY.sqlite3'
    monkeypatch.setenv('FINAGENT_DB_PATH', str(path))
    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend / 'alembic.ini'))
    config.set_main_option('script_location', str(backend / 'migrations'))
    command.upgrade(config, '0012_birthday_workflows')
    engine = make_engine(path)
    with Session(engine) as db:
        reset_demo(db, date(2026, 9, 24))
        balances = dict(db.execute(select(Account.id, Account.balance_minor)).all())
    # The historical initial migration references today's metadata. Reproduce
    # an actual 0012 database by removing ONLY the four new, empty test tables.
    from app.data.models import Base
    with engine.begin() as connection:
        for name in ('task_controls', 'security_authorizations', 'security_challenges', 'security_states'):
            Base.metadata.tables[name].drop(connection, checkfirst=True)
    command.upgrade(config, 'head')
    command.upgrade(config, 'head')
    assert {'security_states', 'security_challenges', 'security_authorizations', 'task_controls'} <= set(inspect(engine).get_table_names())
    with Session(engine) as db:
        assert dict(db.execute(select(Account.id, Account.balance_minor)).all()) == balances
        assert check_ledger(db) == []
    engine.dispose()
