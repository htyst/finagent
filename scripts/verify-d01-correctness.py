"""Extend measured scale data with independent coverage-gap checks and same-input repair comparison."""
import ast,hashlib,json,os,sqlite3,sys
from contextlib import closing
from datetime import datetime,timezone
from pathlib import Path
from sqlalchemy.orm import sessionmaker
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/os.getenv('FINAGENT_REVIEW_DIR','var/reviews/remaining-20261018/final-v6');sys.path.insert(0,str(ROOT/'backend'))
os.environ.setdefault('FINAGENT_DB_PATH',str(ROOT/'var/TEST-ONLY/d01-unused.sqlite3'))
from app.data.db import make_engine
from app.domain.spending import summarize_spending
from app.agent import dynamic_runtime as rt,analysis_workflow as facts
base=json.loads((OUT/'d01.json').read_text(encoding='utf-8'));checks=[]
for measured in base['scales']:
 if measured['status']!='measured':continue
 path=Path(base['run'])/f"scale-{measured['scale']}.sqlite3"
 if 'TEST-ONLY' not in path.resolve().parts:raise SystemExit('Only TEST-ONLY datasets')
 engine=make_engine(path);factory=sessionmaker(engine,expire_on_commit=False)
 with factory() as db:
  for start,end in [('2025-08-01','2026-09-24'),('2026-09-01','2026-09-30')]:
   actual=summarize_spending(db,owner_id='u1',start=start,end=end,evidence_limit=1)
   lo=datetime.fromisoformat(start+'T00:00:00+08:00').astimezone(timezone.utc).isoformat();hi=datetime.fromisoformat(end+'T23:59:59.999999+08:00').astimezone(timezone.utc).isoformat();buy=refund=0
   with closing(sqlite3.connect(path)) as raw:
    for amount,kind in raw.execute("SELECT amount_minor,type FROM transactions WHERE owner_id='u1' AND status='posted' AND posted_at_utc>=? AND posted_at_utc<=? AND type IN ('purchase','refund')",(lo,hi)):
     if kind=='purchase':buy+=amount
     else:refund+=amount
    for amount,kind in raw.execute("SELECT amount_minor,event_type FROM financial_events WHERE owner_id='u1' AND posted_at_utc>=? AND posted_at_utc<=? AND event_type IN ('credit_purchase','subscription_charge','credit_refund')",(lo,hi)):
     if kind=='credit_refund':refund+=amount
     else:buy+=amount
   correct=actual['current']['totals']['net_minor']==buy-refund;incomplete=actual['current']['totals']['is_complete'] is False
   assert correct and incomplete,'coverage gap must not be zero/complete'
   r=dict(id='D01',nodes=[],evidence=[],facts=[]);node=dict(id='s1',name='summarize_spending',arguments={'start':start,'end':end})
   obs=rt.observation(r,node,actual)
   checks.append(dict(scale=measured['scale'],period=[start,end],observed_net_minor=buy-refund,correct_to_cent=correct,unknown_period_not_complete=incomplete,server_evidence_total=actual['current']['evidence']['total'],model_observation_bytes=len(json.dumps(obs,ensure_ascii=False).encode()),evidence_rows_returned=len(actual['current']['evidence']['items'])))
 engine.dispose()
# Reproduce the observed 16-revision failure using exactly the same persisted
# synthetic runtime. The original function comes from the preserved source, no
# old application/normal DB is executed or reset.
comparison=None
failed=ROOT/'var/TEST-ONLY/pytest/1cfb7c0d5d1946a9a135268140852184/TEST-ONLY/test.sqlite3'
previous=ROOT/'var/reviews/remaining-20261018/before/backend/app/agent/dynamic_runtime.py'
if failed.exists() and previous.exists():
 tree=ast.parse(previous.read_text(encoding='utf-8'));fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='current_evidence');ns={};exec(compile(ast.Module(body=[fn],type_ignores=[]),str(previous),'exec'),ns)
 with closing(sqlite3.connect(failed.as_uri()+'?mode=ro',uri=True)) as db:r=json.loads(db.execute('select context_json from agent_tasks').fetchone()[0])['dynamic_runtime']
 def check(active):
  refs=[e['source_ref'] for e in active['evidence'] if not e.get('error')]
  try:facts.Answer(answer_template='仍需核对后续条件。',evidence_refs=refs);valid=True
  except ValueError:valid=False
  return dict(evidence_count=len(refs),answer_schema_accepts=valid,financial_fact_count=len(active['facts']))
 before=check(ns['current_evidence'](r));after=check(rt.current_evidence(r));assert not before['answer_schema_accepts'] and after['answer_schema_accepts'] and before['financial_fact_count']==after['financial_fact_count']
 comparison=dict(same_input_sha256=hashlib.sha256(json.dumps(r,sort_keys=True).encode()).hexdigest(),before=before,after=after,scope='same persisted synthetic input; exact provenance/schema comparison, not latency or LLM quality average')
report=dict(passed=all(x['correct_to_cent'] and x['unknown_period_not_complete'] for x in checks),mode='independent integer sums + actual domain aggregates',coverage_checks=checks,same_input_repair=comparison)
(OUT/'d01-correctness.json').write_text(json.dumps(report,indent=2),encoding='utf-8');print(json.dumps(dict(passed=report['passed'],coverage_cases=len(checks),same_input_repair=comparison)),flush=True)
