"""Recheck the SAME actual provider result by authoritative facts, never change a bad business result."""
import hashlib,json,os,shutil,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/os.getenv('FINAGENT_REVIEW_DIR','var/reviews/remaining-20261018/accepted');p=OUT/'live.json';d=json.loads(p.read_text(encoding='utf-8'));target=Path(d['database']).resolve()
if 'TEST-ONLY' not in target.parts:raise SystemExit('Only isolated evaluation database')
os.environ['FINAGENT_DB_PATH']=str(target);sys.path.insert(0,str(ROOT/'backend'))
from sqlalchemy.orm import sessionmaker
from app.data.db import make_engine
from evaluation_support import balance_evidence
manifest=json.loads((OUT/'backend-tested-source.json').read_text(encoding='utf-8'))
if any(hashlib.sha256((ROOT/n).read_bytes()).hexdigest()!=h for n,h in manifest.items()):raise SystemExit('Source differs from provider evidence')
original=OUT/'live-before-balance-predicate-correction.json'
if not original.exists():shutil.copy2(p,original)
engine=make_engine(target)
with sessionmaker(engine)() as db:
 for item in d['results']:
  if item['case']=='balance':
   valid=balance_evidence(db,item['graph']['task_id'],item['graph']);assert valid,'Actual balance facts failed authoritative ledger match'
   item['checks']['actual_balance_evidence']=valid;item['passed']=all(item['checks'].values())
engine.dispose();d['passed']=all(x['passed'] for x in d['results']);d['evaluation_correction']='Tool name restriction replaced with owned current available-balance facts matched exactly to domain ledger; SAME real provider result, original report retained; no new API call, no rescore of missing/incorrect data.'
p.write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(dict(passed=d['passed'],actual_API_requests=d['api_requests'],balance_rechecked=True)))
