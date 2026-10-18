"""Real domain writer/worker contention on independent seeded TEST-ONLY databases."""
import concurrent.futures,hashlib,json,os,sys,time,threading
from datetime import date,datetime,timedelta,timezone
from pathlib import Path
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/os.getenv('FINAGENT_REVIEW_DIR','var/reviews/remaining-20261018');RUN=ROOT/'var/TEST-ONLY'/('d01-writers-'+uuid4().hex);RUN.mkdir(parents=True)
os.environ['FINAGENT_DB_PATH']=str(RUN/'unused.sqlite3');sys.path.insert(0,str(ROOT/'backend'))
from sqlalchemy import select,func,event
from sqlalchemy.orm import sessionmaker
from app.data.models import Base,DemoSession,DemoSetting,TransferOrder
from app.data.seed import reset_demo,check_ledger
from app.data.db import make_engine
from app.domain import operations as op,scheduled_transfers as st
from app.domain.errors import DomainError
import app.config
app.config.ROOT=RUN
manifest_path=OUT/'backend-tested-source.json'
manifest=json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else {}
if manifest and any(hashlib.sha256((ROOT/n).read_bytes()).hexdigest()!=h for n,h in manifest.items()):raise SystemExit('Source differs from regression')
report=dict(mode='real_domain+simulated_funds+no_model',source_version=hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest(),runs=[])
for case in ('same_order_replay','daily_600_500_race','background_and_frontend'):
 for repetition in range(2):
  engine=make_engine(RUN/f'{case}-{repetition}.sqlite3');Base.metadata.create_all(engine);factory=sessionmaker(engine,expire_on_commit=False);lock_times=[]
  @event.listens_for(engine,'before_cursor_execute')
  def before(conn,cursor,stmt,params,ctx,many):
   if stmt.strip().upper().startswith('BEGIN IMMEDIATE'):ctx.d01_begin=time.perf_counter()
  @event.listens_for(engine,'after_cursor_execute')
  def after(conn,cursor,stmt,params,ctx,many):
   if hasattr(ctx,'d01_begin'):lock_times.append((time.perf_counter()-ctx.d01_begin)*1000)
  with factory() as db:
   reset_demo(db,date(2026,9,24));setting=db.get(DemoSetting,1)
   for sid in ('s1','s2'):db.add(DemoSession(id=sid,owner_id='u1',dataset_version=setting.dataset_version,expires_at_utc=(datetime.now(timezone.utc)+timedelta(hours=2)).isoformat(),csrf_token='TEST-ONLY'))
   db.commit()
   def prepare(amount,sid='s1',source='a1c'):
    result=op.prepare_transfer(db,session_id=sid,source_account_id=source,target={'type':'own_account','account_id':'a1s' if source=='a1c' else 'a1c'},amount_minor=amount)['proposal'];pid=result['id'];token=op.issue_confirmation_token(db,session_id=sid,proposal_id=pid)['confirmation_token']
    return lambda conn:op.confirm_proposal(conn,session_id=sid,proposal_id=pid,confirmation_token=token)
   if case=='same_order_replay':
    a=prepare(10000);functions=[a,a];expected=1
   elif case=='daily_600_500_race':functions=[prepare(60000),prepare(50000,'s2','a1s')];expected=1
   else:
    due=datetime.now(timezone.utc)+timedelta(minutes=10)
    record=st.prepare_schedule(db,session_id='s1',source_account_id='a1c',target={'type':'own_account','account_id':'a1s'},amount_minor=10000,execute_at_utc=due.isoformat())['schedule']
    token=st.issue_schedule_confirmation_token(db,session_id='s1',schedule_id=record['id'])['confirmation_token'];st.confirm_schedule(db,session_id='s1',schedule_id=record['id'],confirmation_token=token)
    a=prepare(10000,'s2');functions=[lambda conn:st._execute_due(conn,record['id'],due),lambda conn:st._execute_due(conn,record['id'],due),a];expected=2
  barrier=threading.Barrier(len(functions))
  def worker(fn):
   with factory() as db:
    barrier.wait(timeout=10);started=time.perf_counter()
    try:
     result=fn(db);return dict(status=result['status'],receipt_id=result.get('order_id') or (result.get('receipt') or {}).get('order_id'),ms=(time.perf_counter()-started)*1000)
    except DomainError as error:return dict(error_code=error.code,ms=(time.perf_counter()-started)*1000)
  started=time.perf_counter()
  with concurrent.futures.ThreadPoolExecutor(max_workers=len(functions)) as pool:results=list(pool.map(worker,functions))
  with factory() as db:
   count=db.scalar(select(func.count()).select_from(TransferOrder));ledger_errors=check_ledger(db)
  assert count==expected and not ledger_errors,(case,count,results,ledger_errors)
  if case=='daily_600_500_race':assert sum(r.get('error_code')=='MFA_REQUIRED' for r in results)==1
  errors=[r['error_code'] for r in results if r.get('error_code')];unexpected=[e for e in errors if e!='MFA_REQUIRED']
  item=dict(case=case,repetition=repetition,concurrency=len(functions),elapsed_ms=(time.perf_counter()-started)*1000,results=results,actual_writes=count,expected_writes=expected,begin_immediate_latency_ms=lock_times,lock_wait_measurement='BEGIN IMMEDIATE elapsed includes acquisition and driver overhead',expected_policy_rejections=len(errors)-len(unexpected),unexpected_errors=len(unexpected),ledger_correct=not ledger_errors)
  report['runs'].append(item);(OUT/'d01-writers.json').write_text(json.dumps(report,indent=2),encoding='utf-8');print(json.dumps({k:item[k] for k in ('case','repetition','actual_writes','unexpected_errors')}),flush=True);engine.dispose()
report['passed']=all(not i['unexpected_errors'] and i['ledger_correct'] for i in report['runs']);(OUT/'d01-writers.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
