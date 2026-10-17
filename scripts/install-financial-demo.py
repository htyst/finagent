"""Prepare, verify and atomically install the annual synthetic demo dataset.

Existing demo sessions/tasks are replaced only after a complete SQLite backup.
Run while the local dev services are stopped. No credentials are read or copied.
"""
from __future__ import annotations
import argparse,json,os,socket,sqlite3,subprocess,sys
from datetime import datetime
from contextlib import closing
from pathlib import Path
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=ROOT/'datasets/demo_financial_v2')
    args=parser.parse_args()
    for port in (8000,5173):
        with socket.socket() as connection:
            connection.settimeout(.3)
            if connection.connect_ex(('127.0.0.1',port))==0:
                raise SystemExit(f'Port {port} is in use. Stop FinAgent services before importing.')
    target=(ROOT/'var/finagent.sqlite3').resolve()
    if target.parent!=(ROOT/'var').resolve():raise SystemExit('Invalid database path')
    staging=ROOT/'var/staging'/uuid4().hex
    staging.mkdir(parents=True)
    prepared=staging/'financial.sqlite3'
    sqlite3.connect(prepared).close()
    env={**os.environ,'PYTHONPATH':str(ROOT/'backend'),'FINAGENT_DB_PATH':str(prepared),
         'PYTHONIOENCODING':'utf-8','PYTHONDONTWRITEBYTECODE':'1','FINAGENT_MODE':'mock'}
    def run(*arguments):subprocess.run([sys.executable,'-B',*arguments],cwd=ROOT,env=env,check=True)
    run('-m','app.data.cli','migrate')
    run(str(ROOT/'scripts/import-financial-dataset.py'),'--source',str(args.source.resolve()))
    run('-m','app.data.cli','check')
    report=json.loads((staging/'financial-import-result.json').read_text(encoding='utf-8'))
    backup=ROOT/'var/backups'/('before-financial-install-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid4().hex[:6])
    backup.mkdir(parents=True)
    if target.exists():
        with closing(sqlite3.connect(target.as_uri()+'?mode=ro',uri=True)) as source:
            with closing(sqlite3.connect(backup/'finagent.sqlite3')) as dest:source.backup(dest)
    # Refuse to replace a database with an active or uncheckpointed journal.
    for suffix in ('-wal','-journal'):
        journal=Path(str(target)+suffix)
        if journal.exists() and journal.stat().st_size:
            raise SystemExit('Database journal present; stop services and checkpoint before installation. Backup retained: '+str(backup))
    os.replace(prepared,target)
    report.update(database=str(target),previous_database_backup=str(backup/'finagent.sqlite3'),state='installed')
    (ROOT/'var/financial-import-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
