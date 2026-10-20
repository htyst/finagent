"""Fresh checkout data completeness, safe publication and repeat startup regression."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import runpy
import sqlite3
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = runpy.run_path(str(ROOT / 'scripts/initialize-demo.py'))
initialize = BOOTSTRAP['initialize_database']
validate = BOOTSTRAP['validate_database']


def environment(database):
    return dict(os.environ, PYTHONPATH=str(ROOT / 'backend'), FINAGENT_DB_PATH=str(database),
                FINAGENT_MODE='mock', PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1',
                FINAGENT_SCHEDULER_ENABLED='0', FINAGENT_SUBSCRIPTIONS_ENABLED='0',
                FINAGENT_BIRTHDAYS_ENABLED='0', FINAGENT_WORKFLOWS_ENABLED='0')


def run(database, *args):
    return subprocess.run([sys.executable, '-B', *args], cwd=ROOT, env=environment(database),
                          check=True, capture_output=True, text=True, encoding='utf-8', timeout=180)


def snapshot(database):
    with closing(sqlite3.connect(database)) as db:
        return {name: hashlib.sha256(repr(db.execute('select * from '+name+' order by 1').fetchall()).encode()).hexdigest()
                for name in ('users', 'accounts', 'transactions', 'transfer_orders', 'proposals', 'demo_settings')}


def test_fresh_checkout_has_complete_annual_bank_and_all_users_in_api(tmp_path):
    database = tmp_path / 'fresh.sqlite3'
    result = initialize(database)
    assert result['state'] == 'annual_demo_initialized'
    assert {k: result[k] for k in ('users', 'accounts', 'cards', 'transactions', 'csv_files')} == {
        'users': 20, 'accounts': 56, 'cards': 40, 'transactions': 25471, 'csv_files': 23}
    assert result['archive_rows'] > 25471
    assert result['migration'] == '0019_workflow_orchestration'
    # This is the real frontend user-list API, not a count from the input CSV.
    run(database, '-c', "from fastapi.testclient import TestClient; from app.main import app; "
        "from app.data.db import engine; "
        "client=TestClient(app); response=client.get('/api/demo/users'); "
        "assert response.status_code==200; rows=response.json()['data']; "
        "assert len(rows)==20; assert len({r['id'] for r in rows})==20; engine.dispose()")
    before = snapshot(database)
    assert initialize(database)['state'] == 'existing_database_preserved'
    assert snapshot(database) == before
    assert validate(database)['transactions'] == 25471


def test_existing_three_user_bank_and_real_transfer_receipt_survive_restart(tmp_path):
    database = tmp_path / 'existing.sqlite3'
    sqlite3.connect(database).close()
    run(database, '-m', 'app.data.cli', 'reset')  # Explicitly create the old disposable fixture.
    run(database, '-c', "from datetime import datetime,timedelta,timezone; from sqlalchemy.orm import Session; "
        "from app.data.db import engine; from app.data.models import DemoSetting,DemoSession; "
        "from app.domain.operations import prepare_transfer,issue_confirmation_token,confirm_proposal; "
        "db=Session(engine); setting=db.get(DemoSetting,1); "
        "db.add(DemoSession(id='TEST-ONLY-startup',owner_id='u1',dataset_version=setting.dataset_version,"
        "expires_at_utc=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),csrf_token='TEST-ONLY'));db.commit(); "
        "p=prepare_transfer(db,session_id='TEST-ONLY-startup',source_account_id='a1c',"
        "target={'type':'own_account','account_id':'a1s'},amount_minor=100)['proposal']['id']; "
        "token=issue_confirmation_token(db,session_id='TEST-ONLY-startup',proposal_id=p)['confirmation_token']; "
        "r=confirm_proposal(db,session_id='TEST-ONLY-startup',proposal_id=p,confirmation_token=token); "
        "assert r['status']=='executed';db.close();engine.dispose()")
    before = snapshot(database)
    with closing(sqlite3.connect(database)) as db:
        assert db.execute('select count(*) from transfer_orders').fetchone()[0] == 1
    for _ in range(2):
        assert initialize(database) == {'state': 'existing_database_preserved', 'users': 3}
        assert snapshot(database) == before


def test_missing_dataset_does_not_fall_back_to_three_users(tmp_path):
    database = tmp_path / 'missing.sqlite3'
    with pytest.raises(SystemExit, match='Annual dataset missing'):
        initialize(database, source=tmp_path / 'missing-dataset')
    assert not database.exists()


def test_simultaneous_first_start_refuses_existing_initialization_lock(tmp_path):
    database = tmp_path / 'concurrent.sqlite3'
    lock = database.with_name(database.name + '.initialize.lock')
    lock.write_text('TEST-ONLY another initializer', encoding='ascii')
    with pytest.raises(SystemExit, match='already in progress'):
        initialize(database)
    assert not database.exists()
    assert lock.read_text('ascii') == 'TEST-ONLY another initializer'


def test_import_failure_leaves_no_partial_runtime_bank(tmp_path, monkeypatch):
    database = tmp_path / 'failure.sqlite3'
    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(subprocess, 'run', fail)
    with pytest.raises(SystemExit, match='Annual initialization failed'):
        initialize(database)
    assert not database.exists()
    assert not database.with_name(database.name + '.initialize.lock').exists()
    assert list(tmp_path.glob('initialize-*/initialize.log'))


def test_racing_bank_is_never_overwritten_by_prepared_annual_data(tmp_path, monkeypatch):
    database = tmp_path / 'race.sqlite3'
    def prepare(command, **kwargs):
        prepared = Path(kwargs['env']['FINAGENT_DB_PATH'])
        prepared.write_bytes(b'TEST-ONLY prepared annual bank')
    monkeypatch.setattr(subprocess, 'run', prepare)
    monkeypatch.setitem(initialize.__globals__, 'validate_database', lambda *args: {})
    original_link = os.link
    def race(source, destination):
        Path(destination).write_bytes(b'TEST-ONLY bank created by another process')
        return original_link(source, destination)
    monkeypatch.setattr(os, 'link', race)
    with pytest.raises(SystemExit, match='Annual initialization failed'):
        initialize(database)
    assert database.read_bytes() == b'TEST-ONLY bank created by another process'


def test_database_outside_project_var_is_refused():
    with pytest.raises(SystemExit, match='outside project var'):
        initialize(ROOT / 'TEST-ONLY-forbidden.sqlite3')


def test_images_ship_current_annual_dataset_and_import_tools():
    for name in ('Dockerfile.backend', 'Dockerfile.cloud'):
        text = (ROOT / name).read_text('utf-8')
        assert 'COPY datasets/demo_financial_v2' in text
        for script in ('initialize-demo.py', 'import-financial-dataset.py', 'install-financial-demo.py'):
            assert script in text
    desktop = (ROOT / 'scripts/dev.ps1').read_text('utf-8-sig')
    assert "Join-Path $PSScriptRoot 'initialize-demo.py'" in desktop
    assert 'app.data.cli reset' not in desktop


def test_tampered_csv_cannot_publish_a_runtime_bank(tmp_path):
    import shutil
    source = tmp_path / 'tampered-source'
    shutil.copytree(ROOT / 'datasets/demo_financial_v2', source)
    with (source / 'users.csv').open('ab') as stream:
        stream.write(b'\nTEST-ONLY corruption')
    database = tmp_path / 'tampered.sqlite3'
    with pytest.raises(SystemExit, match='Annual initialization failed'):
        initialize(database, source=source)
    assert not database.exists()
    assert not database.with_name(database.name + '.initialize.lock').exists()
