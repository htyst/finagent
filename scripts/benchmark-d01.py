"""Isolated reproducible statistics/scale baseline; never opens runtime DB for writing."""
import argparse,concurrent.futures,ctypes,hashlib,json,os,platform,shutil,sqlite3,statistics,sys,time,tracemalloc
from contextlib import closing
from datetime import datetime,timezone
from pathlib import Path
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser();p.add_argument('--source',type=Path,default=ROOT/'var/TEST-ONLY/f01-20261017/baseline.sqlite3');p.add_argument('--out',type=Path,default=ROOT/'var/reviews/remaining-20261018/d01.json');p.add_argument('--max-mib',type=int,default=384);p.add_argument('--repeats',type=int,default=3);p.add_argument('--reuse-run',type=Path);args=p.parse_args()
source=args.source.resolve()
if 'TEST-ONLY' not in source.parts:raise SystemExit('Source must be an isolated TEST-ONLY baseline')
run=args.reuse_run.resolve() if args.reuse_run else ROOT/'var/TEST-ONLY'/('d01-'+uuid4().hex)
if 'TEST-ONLY' not in run.parts:raise SystemExit('Reused scale data must be TEST-ONLY')
run.mkdir(parents=True,exist_ok=True)
os.environ.update(FINAGENT_DB_PATH=str(run/'unused.sqlite3'),FINAGENT_SCHEDULER_ENABLED='0',FINAGENT_SUBSCRIPTIONS_ENABLED='0',FINAGENT_BIRTHDAYS_ENABLED='0',PYTHONDONTWRITEBYTECODE='1')
sys.path.insert(0,str(ROOT/'backend'))
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select,func,text,event
from app.data.db import make_engine
from app.domain.spending import summarize_spending
from app.data.models import DemoSetting,Transaction,TransferOrder

def memory():
 if os.name!='nt':return None
 class Counters(ctypes.Structure):
  _fields_=[('cb',ctypes.c_ulong),('PageFaultCount',ctypes.c_ulong),('PeakWorkingSetSize',ctypes.c_size_t),('WorkingSetSize',ctypes.c_size_t),('QuotaPeakPagedPoolUsage',ctypes.c_size_t),('QuotaPagedPoolUsage',ctypes.c_size_t),('QuotaPeakNonPagedPoolUsage',ctypes.c_size_t),('QuotaNonPagedPoolUsage',ctypes.c_size_t),('PagefileUsage',ctypes.c_size_t),('PeakPagefileUsage',ctypes.c_size_t)]
 c=Counters();c.cb=ctypes.sizeof(c)
 handle=ctypes.windll.kernel32.GetCurrentProcess();ctypes.windll.psapi.GetProcessMemoryInfo(handle,ctypes.byref(c),c.cb)
 return dict(working_set_bytes=c.WorkingSetSize,peak_working_set_bytes=c.PeakWorkingSetSize)


def available_memory():
 if os.name!='nt':return None
 class Status(ctypes.Structure):
  _fields_=[('length',ctypes.c_ulong),('load',ctypes.c_ulong),('total',ctypes.c_ulonglong),('available',ctypes.c_ulonglong),('page_total',ctypes.c_ulonglong),('page_available',ctypes.c_ulonglong),('virtual_total',ctypes.c_ulonglong),('virtual_available',ctypes.c_ulonglong),('extended',ctypes.c_ulonglong)]
 status=Status();status.length=ctypes.sizeof(status)
 if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):return None
 return dict(total_physical_bytes=status.total,available_physical_bytes=status.available)


def clone(target):
 with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(target)) as dst:src.backup(dst)

def independent(path,owner,start,end,card=None):
 # Separate Python integer sum, no shared ORM/fact_query/category/statistics code.
 a=datetime.fromisoformat(start+'T00:00:00+08:00').astimezone(timezone.utc).isoformat();b=datetime.fromisoformat(end+'T23:59:59.999999+08:00').astimezone(timezone.utc).isoformat()
 buy=refund=bc=rc=0
 with closing(sqlite3.connect(path)) as db:
  for row in db.execute('SELECT amount_minor,type,card_id FROM transactions WHERE owner_id=? AND status=? AND posted_at_utc>=? AND posted_at_utc<=? AND type IN (?,?)',(owner,'posted',a,b,'purchase','refund')):
   amount,kind,c=row
   if card and c!=card:continue
   if kind=='purchase':buy+=amount;bc+=1
   else:refund+=amount;rc+=1
  for amount,kind,c in db.execute('SELECT amount_minor,event_type,card_id FROM financial_events WHERE owner_id=? AND posted_at_utc>=? AND posted_at_utc<=? AND event_type IN (?,?,?)',(owner,a,b,'credit_purchase','subscription_charge','credit_refund')):
   if card and c!=card:continue
   if kind=='credit_refund':refund+=amount;rc+=1
   else:buy+=amount;bc+=1
 return dict(purchase_minor=buy,refund_minor=refund,net_minor=buy-refund,purchase_count=bc,refund_count=rc)

report=dict(started_utc=datetime.now(timezone.utc).isoformat(),source=str(source),source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),mode='no_model_API; real SQLite/domain aggregates',machine=dict(system=platform.platform(),processor=platform.processor(),logical_cpus=os.cpu_count(),python=platform.python_version()),sqlite_version=sqlite3.sqlite_version,run=str(run),resource_limit_mib=args.max_mib,scales=[],unmeasured=['external hosted database','production Internet traffic','100 concurrent users','real model 20-turn endurance (deterministic 24-turn separate test)'])
manifest={str(x.relative_to(ROOT)).replace('\\','/'):hashlib.sha256(x.read_bytes()).hexdigest() for x in sorted((ROOT/'backend/app').rglob('*.py'))};report['source_version']=hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest()
def dump():args.out.parent.mkdir(parents=True,exist_ok=True);args.out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
with closing(sqlite3.connect(source)) as db:
 base_count=db.execute('select count(*) from transactions').fetchone()[0];base_events=db.execute('select count(*) from financial_events').fetchone()[0]
 migration=db.execute('select version_num from alembic_version').fetchone()[0]
 try:ledger_bytes=db.execute("select sum(pgsize) from dbstat where name like '%transaction%' or name like '%financial_event%'").fetchone()[0]
 except sqlite3.Error:ledger_bytes=source.stat().st_size
report.update(migration=migration,baseline_transactions=base_count,baseline_credit_events=base_events,ledger_bytes=ledger_bytes,memory_assessment=available_memory(),timing_notes='Local synthetic workload while other verification may run; no production SLA or before/after speed claim')
for scale in (1,10,100):
 target=run/f'scale-{scale}.sqlite3';estimate=source.stat().st_size+(scale-1)*ledger_bytes
 item=dict(scale=scale,estimated_database_bytes=estimate,free_disk_bytes=shutil.disk_usage(run).free)
 mem=available_memory()
 if estimate>args.max_mib*1024**2 or estimate*3>shutil.disk_usage(run).free or scale==100 and (not mem or mem['available_physical_bytes']<768*1024**2):
  item.update(status='not_run_resource_guard',reason='projected database exceeds configured local resource allowance or disk reserve; increase --max-mib on an adequate machine')
  report['scales'].append(item);dump();print(json.dumps(item),flush=True);continue
 fresh=not target.exists()
 if fresh:clone(target)
 if scale>1 and fresh:
  with closing(sqlite3.connect(target)) as db:
   db.execute('PRAGMA foreign_keys=OFF') # synthetic analytical replicas only, not a runnable money ledger
   for table in ('transactions','financial_events'):
    cols=[r[1] for r in db.execute(f'pragma table_info({table})')]
    for i in range(1,scale):
     expr=[("? || "+c if c in {'id','business_id','original_transaction_id','cash_transaction_id'} else c) for c in cols]
     params=[f'd01-{i}-' for c in cols if c in {'id','business_id','original_transaction_id','cash_transaction_id'}]
     if i%10==0:
      mem=available_memory()
      if mem and mem['available_physical_bytes']<512*1024**2:raise SystemExit('Stopped expansion: less than 512 MiB physical memory free; partial isolated dataset retained')
     db.execute(f"INSERT INTO {table} ({','.join(cols)}) SELECT {','.join(expr)} FROM {table} WHERE id NOT LIKE 'd01-%'",params)
    db.commit()
 with closing(sqlite3.connect(target)) as raw:
  if raw.execute('select count(*) from transactions').fetchone()[0]!=base_count*scale:raise SystemExit('Partial or mismatched replica cannot be reused')
 engine=make_engine(target);factory=sessionmaker(engine,expire_on_commit=False)
 captured={}
 @event.listens_for(engine,'before_cursor_execute')
 def capture_query(connection,cursor,statement,parameters,context,executemany):
  if not executemany and len(captured)<12 and statement.lstrip().upper().startswith(('SELECT','WITH')) and any(table in statement.lower() for table in ('transactions','financial_events')):
   signature=hashlib.sha256(statement.encode()).hexdigest()
   captured.setdefault(signature,(statement,parameters))
 cases=[dict(owner='u1',start='2026-09-01',end='2026-09-24'),dict(owner='u1',start='2025-09-25',end='2026-09-24'),dict(owner='u1',start='2025-09-25',end='2026-09-24',card='cc1'),dict(owner='u14',start='2025-09-25',end='2026-09-24')]
 item.update(status='measured',transactions=base_count*scale,credit_events=base_events*scale,database_bytes=target.stat().st_size,cases=[])
 # Capture real SQL and warm all four declared scopes outside measured repeats.
 # Tracing hooks are removed before timing, avoiding observer overhead.
 for case in cases:
  with factory() as db:summarize_spending(db,owner_id=case['owner'],start=case['start'],end=case['end'],card_id=case.get('card'),evidence_limit=1)
 event.remove(engine,'before_cursor_execute',capture_query)
 item['measurement_protocol']='four untimed scope warmups; actual SQL capture removed before timed repeats'
 tracemalloc.start();before_mem=memory()
 for case in cases:
  expected=independent(target,case['owner'],case['start'],case['end'],case.get('card'));times=[]
  for repeat in range(args.repeats):
   with factory() as db:
    started=time.perf_counter();data=summarize_spending(db,owner_id=case['owner'],start=case['start'],end=case['end'],card_id=case.get('card'),evidence_limit=1)
    times.append((time.perf_counter()-started)*1000)
  actual=data['current']['totals'];checks={k:actual[k]==v for k,v in expected.items()}
  item['cases'].append(dict(scope=case,expected=expected,actual=actual,correct=all(checks.values()),latencies_ms=times,median_ms=statistics.median(times),returned_evidence=len(data['current']['evidence']['items']),coverage=data['current']['coverage']['status']))
  assert all(checks.values()),'Independent penny reconciliation failed'
 def read(owner):
  with factory() as db:
   started=time.perf_counter();result=summarize_spending(db,owner_id=owner,start='2025-09-25',end='2026-09-24',evidence_limit=1);return dict(owner=owner,ms=(time.perf_counter()-started)*1000,net=result['current']['totals']['net_minor'])
 for label,owners in [('multi_user_read',['u1','u2','u3','u14']),('same_user_read',['u1']*4)]:
  with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
   started=time.perf_counter();results=list(pool.map(read,owners));item[label]=dict(concurrency=4,elapsed_ms=(time.perf_counter()-started)*1000,error_count=0,results=results)
 with closing(sqlite3.connect(target)) as raw:
  item['query_plans']=[list(r) for r in raw.execute("EXPLAIN QUERY PLAN SELECT category,sum(amount_minor) FROM transactions WHERE owner_id='u1' AND posted_at_utc>='2025-09-24T16:00:00+00:00' AND posted_at_utc<'2026-09-24T16:00:00+00:00' AND type IN ('purchase','refund') GROUP BY category")]
  item['indexes']=[list(r) for r in raw.execute('pragma index_list(transactions)')]
  item['actual_query_plans']=[dict(sql_sha256=signature,sql=sql,synthetic_parameters=parameters,plan=[list(row) for row in raw.execute('EXPLAIN QUERY PLAN '+sql,parameters)]) for signature,(sql,parameters) in captured.items()]
  assert item['actual_query_plans'],'Actual domain SQL query plan was not captured'

 item.update(memory_before=before_mem,memory_after=memory(),python_allocation_peak_bytes=tracemalloc.get_traced_memory()[1],correct=all(c['correct'] for c in item['cases']),lock_wait='read-only workload; no writer lock requested')
 tracemalloc.stop();engine.dispose();report['scales'].append(item);dump();print(json.dumps(dict(scale=scale,status=item['status'],transactions=item['transactions'],database_bytes=item['database_bytes'],medians_ms=[c['median_ms'] for c in item['cases']])),flush=True)
report['passed']=all(s.get('correct',True) for s in report['scales']);report['optimization_decision']='No speculative index/cache added; existing owner/date indexes inspected. Exact query plans and timings are evidence, not a production SLA.';dump()
