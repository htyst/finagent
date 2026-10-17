"""Verify the container selects the cloud gateway before starting Uvicorn."""
from pathlib import Path
import runpy
import sys

import pytest

import app.config

ENTRYPOINT = Path(__file__).resolve().parents[2] / 'scripts' / 'container-start.py'


def launch(monkeypatch, database):
    calls = []
    monkeypatch.setattr(app.config, 'database_path', lambda: database)
    monkeypatch.setattr('subprocess.run', lambda command, **kwargs: calls.append(('database', command)))
    monkeypatch.setattr('os.execv', lambda program, command: calls.append(('server', command)))
    runpy.run_path(str(ENTRYPOINT), run_name='__main__')
    return calls


def test_local_container_keeps_default_app_and_port(tmp_path, monkeypatch):
    monkeypatch.delenv('FINAGENT_APP_ENTRY', raising=False)
    monkeypatch.delenv('PORT', raising=False)
    calls = launch(monkeypatch, tmp_path / 'bank.sqlite3')
    assert calls[0] == ('database', [sys.executable, '-m', 'app.data.cli', 'reset'])
    assert calls[1] == ('server', [sys.executable, '-m', 'uvicorn', 'app.main:app',
                                  '--host', '0.0.0.0', '--port', '8000'])


def test_cloud_container_uses_factory_port_and_migrates_existing_data(tmp_path, monkeypatch):
    monkeypatch.setenv('FINAGENT_APP_ENTRY', 'app.cloud:app_factory')
    monkeypatch.setenv('PORT', '18080')
    database = tmp_path / 'bank.sqlite3'
    database.write_bytes(b'TEST-ONLY existing data')
    calls = launch(monkeypatch, database)
    assert calls[0] == ('database', [sys.executable, '-m', 'app.data.cli', 'migrate'])
    assert calls[1] == ('server', [sys.executable, '-m', 'uvicorn', 'app.cloud:app_factory',
                                  '--host', '0.0.0.0', '--port', '18080', '--factory'])
    assert database.read_bytes() == b'TEST-ONLY existing data'


@pytest.mark.parametrize('port', ['0', '65536', 'invalid'])
def test_invalid_port_stops_before_database_changes(tmp_path, monkeypatch, port):
    monkeypatch.setenv('PORT', port)
    monkeypatch.delenv('FINAGENT_APP_ENTRY', raising=False)
    monkeypatch.setattr(app.config, 'database_path', lambda: tmp_path / 'bank.sqlite3')
    def unexpected_run(*args, **kwargs):
        pytest.fail('Database command must not run for invalid configuration')
    monkeypatch.setattr('subprocess.run', unexpected_run)
    with pytest.raises(SystemExit, match='PORT must be between'):
        runpy.run_path(str(ENTRYPOINT), run_name='__main__')


def test_unsupported_entry_stops_before_database_changes(tmp_path, monkeypatch):
    monkeypatch.setenv('PORT', '8000')
    monkeypatch.setenv('FINAGENT_APP_ENTRY', 'untrusted.module:app')
    monkeypatch.setattr(app.config, 'database_path', lambda: tmp_path / 'bank.sqlite3')
    def unexpected_run(*args, **kwargs):
        pytest.fail('Database command must not run for invalid configuration')
    monkeypatch.setattr('subprocess.run', unexpected_run)
    with pytest.raises(SystemExit, match='Unsupported application entry'):
        runpy.run_path(str(ENTRYPOINT), run_name='__main__')
