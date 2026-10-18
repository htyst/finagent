"""Small real DeepSeek G01 evaluation. Isolated synthetic funds, no global API cap."""
import json,os,sqlite3,sys,time
from contextlib import closing
from pathlib import Path
from datetime import datetime,timedelta,timezone
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/os.getenv('FINAGENT_REVIEW_DIR','var/reviews/g01-phase1-20261017');OUT.mkdir(parents=True,exist_ok=True)
STAGE=ROOT/'var/TEST-ONLY'/('g01-live-'+uuid4().hex);STAGE.mkdir(parents=True)
source=ROOT/'var/TEST-ONLY/f01-20261017/baseline.sqlite3';target=STAGE/'bank.sqlite3'
with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as src:
 with closing(sqlite3.connect(target)) as dst:src.backup(dst)
os.environ.update(FINAGENT_DB_PATH=str(target),FINAGENT_AGENT_ENGINE='dynamic',FINAGENT_MODE='live',PYTHONDONTWRITEBYTECODE='1',FINAGENT_SCHEDULER_ENABLED='0',FINAGENT_SUBSCRIPTIONS_ENABLED='0',FINAGENT_BIRTHDAYS_ENABLED='0')
sys.path.insert(0,str(ROOT/'backend'))
from sqlalchemy import select,func
from sqlalchemy.orm import sessionmaker
from app.data.db import make_engine
from app.data.models import DemoSession,DemoSetting,TransferOrder
from app.agent import model_client,telemetry,registry
from app.agent.service import create
from app.domain.errors import DomainError
from evaluation_support import balance_evidence
report=dict(mode='real_deepseek',database=str(target),global_api_cap=None,per_turn_application_limits_preserved=True,results=[])
# Bind actual provider evidence to the same immutable source manifest as tests.
import hashlib
manifest_path=OUT/'backend-tested-source.json'
if manifest_path.exists():
 manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
 if any(hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=sha for name,sha in manifest.items()):raise SystemExit('Source changed before Live evaluation')
 report['source_version']=hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest()
def dump():(OUT/'live.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
CASES=[('greeting','你好。'),('balance','查一下我的可用余额。'),('missing_budget','想旅行，请结合我的现金和收支判断可行性。我还没确定旅行预算，只分析，不办理。'),('multiple_partial','我有两个独立目标：先查本人余额；再分析旅行是否可行，目前没确定旅行花费。请分别保留进度，只分析不办理。'),('tool_failure','查一下我的可用余额。'),('waiting_authorization','从活期账户转100元到储蓄账户。')]
engine=make_engine(target);factory=sessionmaker(engine,expire_on_commit=False)
try:
 report['model']=model_client.configuration().model
 for name,text in CASES:
  metrics=[];started=time.monotonic();original=registry.execute
  try:
   if name=='tool_failure':
    def broken(*a,**kw):
     if kw['name'] not in {'search_knowledge','read_tool_catalog'}:raise DomainError('G01_INJECTED_READ_FAILURE','隔离验收注入读取失败，请说明不能获得余额，不执行其他业务。',503)
     return original(*a,**kw)
    registry.execute=broken
   with factory() as db,telemetry.collect() as metrics:
    setting=db.get(DemoSetting,1);sess=DemoSession(id='TEST-ONLY-'+uuid4().hex,owner_id='u1',dataset_version=setting.dataset_version,expires_at_utc=(datetime.now(timezone.utc)+timedelta(hours=2)).isoformat(),csrf_token='TEST-ONLY');db.add(sess);db.commit()
    before=db.scalar(select(func.count()).select_from(TransferOrder))
    result=create(db,sess,'live',text);g=result['execution_graph'];statuses=[x['status'] for x in g['goals']]
    no_write=db.scalar(select(func.count()).select_from(TransferOrder))==before
    checks=dict(no_unauthorized_write=no_write,has_graph=bool(g))
    if name=='greeting':checks.update(no_forced_business_plan=all(n['policy']['effect']=='answer' for n in g['nodes']),answered=g['status']=='completed')
    elif name=='balance':checks.update(actual_balance_evidence=balance_evidence(db,result['id'],g),complete=g['status']=='completed')
    elif name=='missing_budget':checks.update(waiting_input='waiting_input' in statuses,actual_basis=any(n['name']=='financial_planning_basis' and n['status']=='completed' for n in g['nodes']))
    elif name=='multiple_partial':checks.update(multiple_goals=len(g['goals'])>=2,partial='completed' in statuses and 'waiting_input' in statuses)
    elif name=='tool_failure':checks.update(failed='failed' in statuses,injected_failure_observed=any(n.get('error_code')=='G01_INJECTED_READ_FAILURE' for n in g['nodes']))
    else:checks.update(waiting_authorization='waiting_authorization' in statuses,real_pending_form=bool(result.get('proposal') and result['proposal']['status']=='pending_confirmation'))
    item=dict(case=name,user=text,checks=checks,passed=all(checks.values()),graph=g,reply=result['messages'][-1]['content'],fault_injected=name=='tool_failure')
  except Exception as error:
   item=dict(case=name,passed=False,error_type=type(error).__name__,error_code=getattr(error,'code',None))
  finally:registry.execute=original
  item.update(seconds=round(time.monotonic()-started,3),api_requests=len(metrics),metrics=metrics);report['results'].append(item);dump();print(json.dumps({k:item[k] for k in ['case','passed','api_requests','seconds']},ensure_ascii=False),flush=True)
 report['passed']=all(i['passed'] for i in report['results']);report['api_requests']=sum(i['api_requests'] for i in report['results']);dump()
finally:engine.dispose()