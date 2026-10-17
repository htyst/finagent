"""Regression checks for the Git-index scanner, using disposable repositories."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

SCANNER = Path(__file__).resolve().parents[2] / 'scripts' / 'scan-git.py'


def scan_index(directory, path, content, *, working_content=None):
    directory.mkdir()
    subprocess.run(['git', 'init', '--quiet', str(directory)], check=True)
    target = directory / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding='utf-8')
    subprocess.run(['git', '-C', str(directory), 'add', '--', path], check=True)
    if working_content is not None:
        target.write_text(working_content, encoding='utf-8')
    result = subprocess.run([sys.executable, str(SCANNER)], cwd=directory,
                            text=True, capture_output=True, check=False)
    return result, json.loads(result.stdout)


def test_task_and_risk_identifiers_are_not_credentials(tmp_path):
    identifiers = ['task-authorization-browser-integration-test',
                   'risk-authorization-browser-integration-test']
    result, report = scan_index(tmp_path / 'identifiers', 'test.js', repr(identifiers))
    assert result.returncode == 0
    assert report['errors'] == []


@pytest.mark.parametrize('prefix', ['sk-', 'ghp_'])
def test_standalone_credentials_are_blocked_without_echoing_values(tmp_path, prefix):
    token = prefix + 'A' * 32
    result, report = scan_index(tmp_path / 'credential', 'config.txt', 'token="' + token + '"')
    assert result.returncode == 1
    assert report['errors'] == [{'file': 'config.txt', 'rule': 'credential-pattern'}]
    assert token not in result.stdout + result.stderr


def test_private_key_header_is_blocked(tmp_path):
    header = '-----BEGIN ' + 'PRIVATE KEY-----'
    result, report = scan_index(tmp_path / 'private-key', 'note.txt', header)
    assert result.returncode == 1
    assert report['errors'][0]['rule'] == 'credential-pattern'


@pytest.mark.parametrize('path', ['.env', 'var/runtime.txt'])
def test_private_and_runtime_files_are_still_blocked(tmp_path, path):
    result, report = scan_index(tmp_path / 'private-path', path, 'TEST-ONLY')
    assert result.returncode == 1
    assert report['errors'] == [{'file': path, 'rule': 'private-or-runtime-file'}]


@pytest.mark.parametrize('path', ['FinAgent.exe', 'FinAgent-Mock.exe', 'FinAgent-Live.exe'])
def test_named_root_launchers_can_be_published(tmp_path, path):
    result, report = scan_index(tmp_path / 'launcher', path, 'MZ\0TEST-ONLY')
    assert result.returncode == 0
    assert report['errors'] == []


@pytest.mark.parametrize('path', ['Other.exe', 'Other.EXE', 'tools/FinAgent.exe', 'var/FinAgent.exe'])
def test_other_executables_are_still_blocked(tmp_path, path):
    result, report = scan_index(tmp_path / 'executable', path, 'MZ\0TEST-ONLY')
    assert result.returncode == 1
    assert report['errors'] == [{'file': path, 'rule': 'private-or-runtime-file'}]


def test_named_launcher_does_not_bypass_credential_scanning(tmp_path):
    token = 'ghp_' + 'C' * 32
    result, report = scan_index(tmp_path / 'launcher-token', 'FinAgent.exe', 'MZ\0' + token)
    assert result.returncode == 1
    assert report['errors'] == [{'file': 'FinAgent.exe', 'rule': 'credential-pattern'}]


def test_scanner_uses_staged_content_not_working_copy(tmp_path):
    token = 'sk-' + 'B' * 32
    result, report = scan_index(tmp_path / 'index', 'config.txt', token,
                                working_content='safe working copy')
    assert result.returncode == 1
    assert report['errors'][0]['rule'] == 'credential-pattern'
