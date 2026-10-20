"""First-run annual demo initialization. Never replace an existing runtime bank."""
from __future__ import annotations

import argparse
import csv
import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'datasets/demo_financial_v2'


def validate_database(database: Path, source: Path = SOURCE) -> dict:
    """Read-only, exact first-run checks against the shipped manifest and archive."""
    expected = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
    csv_manifest = json.loads((source / 'csv_file_manifest.json').read_text(encoding='utf-8'))
    for item in csv_manifest['files']:
        path = (source / item['file']).resolve()
        if path.parent != source.resolve() or hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
            raise RuntimeError('Annual CSV missing or hash mismatch')
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        counts = {name: db.execute('select count(*) from ' + name).fetchone()[0]
                  for name in ('users', 'accounts', 'cards', 'transactions')}
        required = {'users': expected['user_count'], 'accounts': expected['account_count'],
                    'cards': expected['debit_card_count'], 'transactions': expected['cash_transaction_rows']}
        if counts != required:
            raise RuntimeError('Annual data counts differ from manifest: ' + json.dumps(counts))
        if db.execute('select seed_version from demo_settings').fetchone()[0] != expected['dataset_version']:
            raise RuntimeError('Unexpected seed version')
        archived = json.loads(db.execute('select manifest_json from financial_dataset_imports').fetchone()[0])
        if archived != expected:
            raise RuntimeError('Imported manifest does not match shipped dataset')
        # Verify every CSV's lossless archive count, not just the visible user list.
        archive_rows = 0
        for item in csv_manifest['files']:
            with (source / item['file']).open(encoding='utf-8-sig', newline='') as stream:
                count = sum(1 for _ in csv.DictReader(stream))
            actual = db.execute('select count(*) from financial_dataset_rows where table_name=?',
                                (Path(item['file']).stem,)).fetchone()[0]
            if actual != count:
                raise RuntimeError('Incomplete archive: ' + item['file'])
            archive_rows += count
        migration = db.execute('select version_num from alembic_version').fetchone()[0]
    return dict(seed_version=expected['dataset_version'], **counts, csv_files=len(csv_manifest['files']),
                archive_rows=archive_rows, migration=migration)


def initialize_database(database: Path, source: Path = SOURCE) -> dict:
    database = database.resolve()
    if not database.is_relative_to((ROOT / 'var').resolve()):
        raise SystemExit('Refusing database outside project var/')
    env = dict(os.environ, PYTHONPATH=str(ROOT / 'backend'), FINAGENT_DB_PATH=str(database),
               PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1')
    if database.exists():
        subprocess.run([sys.executable, '-B', '-m', 'app.data.cli', 'migrate'],
                       cwd=ROOT, env=env, check=True, timeout=180)
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
            users = db.execute('select count(*) from users').fetchone()[0]
        if users == 3:
            print('Existing three-user bank retained. To install annual data, stop services and use '
                  'scripts/install-financial-demo.py; it backs up before explicit replacement.', file=sys.stderr)
        return dict(state='existing_database_preserved', users=users)
    # Fail closed before creating a bank if the release omitted the dataset.
    if not (source / 'manifest.json').is_file() or not (source / 'csv_file_manifest.json').is_file():
        raise SystemExit('Annual dataset missing; obtain the complete FinAgent-03 source. No three-user fallback.')
    database.parent.mkdir(parents=True, exist_ok=True)
    lock = database.with_name(database.name + '.initialize.lock')
    try:
        gate = lock.open('x', encoding='ascii')
    except FileExistsError:
        raise SystemExit('Demo initialization already in progress. Do not start a second instance; '
                         'after a crash inspect the staged data and lock before retrying.') from None
    staging = None
    try:
        gate.close()
        if database.exists():
            raise SystemExit('Database appeared during initialization; nothing replaced. Restart to migrate it.')
        staging = database.parent / ('initialize-' + uuid4().hex)
        staging.mkdir()
        prepared = staging / 'annual.sqlite3'
        sqlite3.connect(prepared).close()
        child_env = dict(env, FINAGENT_DB_PATH=str(prepared), FINAGENT_MODE='mock')
        commands = [('-m', 'app.data.cli', 'migrate'),
                    (str(ROOT / 'scripts/import-financial-dataset.py'), '--source', str(source.resolve())),
                    ('-m', 'app.data.cli', 'check')]
        with (staging / 'initialize.log').open('w', encoding='utf-8') as log:
            for args in commands:
                subprocess.run([sys.executable, '-B', *args], cwd=ROOT, env=child_env, check=True,
                               stdout=log, stderr=subprocess.STDOUT, timeout=180)
        summary = validate_database(prepared, source)
        # Same-volume hard-link publication is atomic and refuses an existing target.
        # A racing process can never have its bank replaced by os.replace/rename.
        os.link(prepared, database)
        if staging.resolve().parent != database.parent or not staging.name.startswith('initialize-'):
            raise RuntimeError('Unexpected staging path')
        shutil.rmtree(staging)
        return dict(state='annual_demo_initialized', **summary)
    except Exception as error:
        raise SystemExit('Annual initialization failed; no existing bank replaced. Staged diagnostics: '
                         + str(staging) + '; ' + type(error).__name__) from error
    finally:
        lock.unlink()


def main():
    from app.config import database_path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=database_path())
    parser.add_argument('--check-only', action='store_true', help='Read-only exact fresh-bank verification')
    args = parser.parse_args()
    result = (dict(state='verified', **validate_database(args.database)) if args.check_only
              else initialize_database(args.database))
    print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    main()
