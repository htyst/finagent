"""Inspect the Git index without printing secret values or reading local .env."""
from pathlib import PurePosixPath
import hashlib
import json
import re
import subprocess

entries = subprocess.check_output(['git', 'ls-files', '--stage', '-z']).decode().split('\0')
errors = []
# Match credential prefixes at an identifier boundary, not inside task-/risk- names.
patterns = [rb'(?<![A-Za-z0-9_])sk-[A-Za-z0-9_-]{24,}', rb'(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9]{25,}', rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----']
# Exact invalid synthetic literals in pre-existing tests and their failure report.
# A changed value or a copy into any other file still fails the scan.
synthetic_hashes = {
    'backend/tests/test_deepseek.py': {'429297a5bf224447c5d6ca2a53c939742f257acd645a6f802b29b620cbae5c6f'},
    'docs/evidence/dialogue-first.xml': {'429297a5bf224447c5d6ca2a53c939742f257acd645a6f802b29b620cbae5c6f'},
    'backend/tests/test_unified_dialogue.py': {'59373c3f9400454324e7861b2a8070aad9170cd47f732a2afcd8ab9c6cdb94ab'},
}
safe = []
for entry in filter(None, entries):
    metadata, path = entry.split('\t', 1)
    parts = PurePosixPath(path).parts
    name = parts[-1]
    if (any(p in {'var', '.venv', 'node_modules', '__pycache__'} or p.startswith('.venv') for p in parts)
            or name.startswith('.env') and name != '.env.example'
            or name.endswith(('.key', '.pem', '.p12', '.pfx', '.sqlite3', '.sqlite3-wal', '.sqlite3-shm', '.exe'))):
        errors.append({'file': path, 'rule': 'private-or-runtime-file'})
        continue
    safe.append((metadata.split()[1], path))
# One Git subprocess for all blobs; never print blob contents or scan private env.
raw = subprocess.check_output(['git', 'cat-file', '--batch'], input=('\n'.join(oid for oid, _ in safe) + '\n').encode()) if safe else b''
offset = 0
for oid, path in safe:
    end = raw.index(b'\n', offset)
    header = raw[offset:end].split()
    if len(header) != 3 or header[1] != b'blob':
        raise SystemExit('Unexpected Git object type')
    size = int(header[2])
    content = raw[end + 1:end + 1 + size]
    offset = end + 2 + size
    if any(hashlib.sha256(match.group()).hexdigest() not in synthetic_hashes.get(path, set())
           for pattern in patterns for match in re.finditer(pattern, content)):
        errors.append({'file': path, 'rule': 'credential-pattern'})
print(json.dumps({'check': 'git-index-pattern-scan', 'files': len(safe), 'errors': errors,
                  'limits': 'Pattern scan only, not a proof that arbitrary secrets are absent.'}, ensure_ascii=False))
raise SystemExit(bool(errors))
