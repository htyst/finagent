"""SQLite online backup, run by the instance owner; never exposed as an API."""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from app.config import database_path

source = database_path()
if not source.is_file():
    raise SystemExit('Database does not exist')
directory = source.parent / 'backups'
directory.mkdir(parents=True, exist_ok=True)
stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
for item in [source, source.parent / 'cloud-budget.sqlite3']:
    if not item.is_file():
        continue
    target = directory / f'{item.stem}-{stamp}.sqlite3'
    with sqlite3.connect(item.as_uri() + '?mode=ro', uri=True) as before, sqlite3.connect(target) as after:
        before.backup(after)
    target.chmod(0o600)
    print(target)
