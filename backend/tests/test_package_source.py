"""Check downloadable archives keep required data and exclude local runtime files."""
import importlib.util
import json
from pathlib import Path
import sys
import zipfile

import pytest


@pytest.fixture
def package(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[2] / 'scripts' / 'package-source.py'
    spec = importlib.util.spec_from_file_location('finagent_package_source', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / 'project'
    monkeypatch.setattr(module, 'ROOT', root)
    files = module.REQUIRED | module.LAUNCHERS | {
        'datasets/demo_financial_v2/transactions.csv', 'Dockerfile.cloud',
        'scripts/cloud-start.py', '.github/workflows/ci.yml', '.env',
        'var/finagent.sqlite3', 'frontend/node_modules/local.txt',
        'frontend/dist/index.html', 'Other.exe', 'scripts/FinAgent.exe',
    }
    for name in files:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'TEST-ONLY')
    return module, root


def test_windows_archive_includes_launchers_and_data_without_local_runtime(package, monkeypatch, capsys):
    module, root = package
    monkeypatch.setattr(sys, 'argv', ['package-source.py', '--include-launchers', '--name', 'windows.zip'])
    module.main()
    report = json.loads(capsys.readouterr().out)
    assert report['verified'] is True
    assert report['windows_launchers_included'] is True
    with zipfile.ZipFile(root / 'var/artifacts/windows.zip') as archive:
        names = set(archive.namelist())
        for name in module.REQUIRED | module.LAUNCHERS | {
            'datasets/demo_financial_v2/transactions.csv', 'Dockerfile.cloud',
            'scripts/cloud-start.py', '.github/workflows/ci.yml',
        }:
            assert 'finagent/' + name in names
        for name in ('.env', 'var/finagent.sqlite3', 'frontend/node_modules/local.txt',
                     'frontend/dist/index.html', 'Other.exe', 'scripts/FinAgent.exe'):
            assert 'finagent/' + name not in names
        manifest = json.loads(archive.read('SOURCE-MANIFEST.json'))
        assert set(manifest) == {name.removeprefix('finagent/') for name in names if name != 'SOURCE-MANIFEST.json'}


def test_requested_windows_archive_rejects_a_missing_launcher(package, monkeypatch):
    module, root = package
    (root / 'FinAgent-Live.exe').unlink()
    monkeypatch.setattr(sys, 'argv', ['package-source.py', '--include-launchers'])
    with pytest.raises(SystemExit, match='FinAgent-Live.exe'):
        module.main()


def test_default_source_archive_keeps_data_and_excludes_binaries(package, monkeypatch):
    module, root = package
    monkeypatch.setattr(sys, 'argv', ['package-source.py', '--name', 'source.zip'])
    module.main()
    with zipfile.ZipFile(root / 'var/artifacts/source.zip') as archive:
        names = set(archive.namelist())
        assert 'finagent/datasets/demo_financial_v2/transactions.csv' in names
        assert not any(name.lower().endswith('.exe') for name in names)
