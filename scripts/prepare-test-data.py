"""Create a portable annual test baseline from shipped CSVs; never reset runtime data."""
import argparse,json,os,sqlite3,subprocess,sys
from pathlib import Path
from contextlib import closing
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1]
def main():
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--target',type=Path,default=ROOT/'var/TEST-ONLY/f01-20261017/baseline.sqlite3')
 args=parser.parse_args();target=args.target.resolve();allowed=(ROOT/'var/TEST-ONLY').resolve()
 if not target.is_relative_to(allowed):raise SystemExit('Refusing target outside project var/TEST-ONLY')
 source=ROOT/'datasets/demo_financial_v2';expected=json.loads((source/'manifest.json').read_text(encoding='utf-8'))
 def validate(path):
  with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
   assert db.execute('select seed_version from demo_settings').fetchone()[0]=='demo-financial-v2'
   assert db.execute('select count(*) from transactions').fetchone()[0]==expected['cash_transaction_rows']
   assert db.execute('select count(*) from users').fetchone()[0]==expected['user_count']
   assert json.loads(db.execute('select manifest_json from financial_dataset_imports').fetchone()[0])==expected
  return dict(path=str(target),transactions=expected['cash_transaction_rows'],users=expected['user_count'])
 if target.exists():
  print(json.dumps(dict(state='reused',**validate(target)),ensure_ascii=False));return
 target.parent.mkdir(parents=True,exist_ok=True);staging=target.parent/('prepare-'+uuid4().hex);staging.mkdir();prepared=staging/'baseline.sqlite3';sqlite3.connect(prepared).close()
 env=dict(os.environ,PYTHONPATH=str(ROOT/'backend'),PYTHONIOENCODING='utf-8',PYTHONDONTWRITEBYTECODE='1',FINAGENT_DB_PATH=str(prepared),FINAGENT_MODE='mock',FINAGENT_SCHEDULER_ENABLED='0',FINAGENT_SUBSCRIPTIONS_ENABLED='0',FINAGENT_BIRTHDAYS_ENABLED='0')
 with (staging/'prepare.log').open('w',encoding='utf-8') as log:
  for args in [('-m','app.data.cli','migrate'),(str(ROOT/'scripts/import-financial-dataset.py'),'--source',str(source)),('-m','app.data.cli','check')]:
   result=subprocess.run([sys.executable,'-B',*args],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=180)
   if result.returncode:raise SystemExit('Test baseline preparation failed; isolated log: '+str(staging/'prepare.log'))
 validate(prepared)
 if target.exists():raise SystemExit('Target appeared concurrently; staged data retained, nothing replaced')
 prepared.rename(target);print(json.dumps(dict(state='created',**validate(target)),ensure_ascii=False))
if __name__=='__main__':main()
