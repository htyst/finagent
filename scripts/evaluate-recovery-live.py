"""Small real DeepSeek multi-turn and post-commit recovery evaluation, no global cap."""
import hashlib,json,os,sqlite3,sys,time
from contextlib import closing
from datetime import datetime,timedelta,timezone
from pathlib import Path
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/os.getenv('FINAGENT_REVIEW_DIR','var/reviews/remaining-20261018/final');OUT.mkdir(parents=True,exist_ok=True)
RUN=ROOT/'var/TEST-ONLY'/('recovery-live-'+uuid4().hex);RUN.mkdir(parents=True);target=RUN/'bank.sqlite3';source=ROOT/'var/TEST-ONLY/f01-20261017/baseline.sqlite3'
with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(target)) as dest:src.backup(dest)
os.environ.update(FINAGENT_DB_PATH=str(target),FINAGENT_AGENT_ENGINE='dynamic',FINAGENT_MODE='live',FINAGENT_SCHEDULER_ENABLED='0',FINAGENT_SUBSCRIPTIONS_ENABLED='0',FINAGENT_BIRTHDAYS_ENABLED='0')
sys.path.insert(0,str(ROOT/'backend'))
from sqlalchemy import select,func
from sqlalchemy.orm import sessionmaker
from app.data.db import make_engine
from app.data.models import DemoSetting,DemoSession,AgentTask,TransferOrder
from app.agent import model_client,telemetry,dynamic_runtime as rt,dynamic_protocol as protocol,authorization
from app.agent.service import create,send,view
from app.agent.privacy import model_payload
from app.domain.errors import DomainError
engine=make_engine(target);factory=sessionmaker(engine,expire_on_commit=False)
manifest=json.loads((OUT/'backend-tested-source.json').read_text(encoding='utf-8'))
if any(hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=sha for name,sha in manifest.items()):raise SystemExit('Source differs from regression')
report=dict(mode='real_DeepSeek+isolated_simulated_bank',global_api_cap=None,application_per_turn_limits=rt.config(),source_version=hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest(),model=model_client.configuration().model,turns=[],recovery=None)
def dump():(OUT/'live-recovery.json').write_text(json.dumps(model_payload(report),ensure_ascii=False,indent=2),encoding='utf-8')
def new_session(db):
 setting=db.get(DemoSetting,1);s=DemoSession(id='TEST-ONLY-'+uuid4().hex,owner_id='u1',dataset_version=setting.dataset_version,expires_at_utc=(datetime.now(timezone.utc)+timedelta(hours=2)).isoformat(),csrf_token='TEST-ONLY');db.add(s);db.commit();return s
texts=['想去旅行，总预算两万元，请结合我的现金和收支分析可行性，不能挪用应急金，只咨询不办理。','刚才旅行预算两万改成八千元，出行时间尚未确定，请只按新预算分析，不办理。','先查一下我的可用余额，原来的旅行分析待办先保留，不要转账。','取消旅行分析目标，保留余额查询的结果；不办理资金操作。']
try:
 with factory() as db:
  s=new_session(db);tid=None;goal_id=None
  for index,text in enumerate(texts):
   started=time.perf_counter()
   with telemetry.collect() as metrics:
    result=create(db,s,'live',text) if tid is None else send(db,s,tid,text)
   tid=result['id'];g=result['execution_graph'];r=rt.get(db.get(AgentTask,tid));checks={'no_write':db.scalar(select(func.count()).select_from(TransferOrder))==0,'no_prepared_fund_operation':not any(n['policy']['effect']=='prepare' for n in g['nodes'])}
   if index==0:
    goal_id=next((x['id'] for x in g['goals'] if x['kind']=='analysis'),g['goals'][0]['id']);checks['queried_basis']=any(n['name']=='financial_planning_basis' and n['status']=='completed' for n in g['nodes'])
   if index==1:
    checks['same_goal_retained']=any(x['id']==goal_id for x in g['goals']);checks['revision_recorded']=any(n['name']=='revise_goals' and n['status']=='completed' for n in g['nodes']);checks['new_budget_constraint']=any(any(c['value'] in {'八千','八千元','8000','8000元'} for c in x.get('constraints',[])) for x in g['goals'])
    checks['old_budget_calculations_not_active']=not any(n['name']=='calculate' and not n.get('invalidated') and any(i.get('value') in {'20000','20000.00'} for i in n['arguments'].get('calculation',{}).get('inputs',[])) for n in r['nodes'])
   if index==2:checks['old_goal_retained']=any(x['id']==goal_id for x in g['goals']);checks['balance_evidence']=any(n['name'] in {'list_accounts','financial_planning_basis'} and n['status']=='completed' and not n.get('invalidated') for n in g['nodes']) and any(f['key']=='available_liquid' or f['tool']=='list_accounts' for f in rt.current_evidence(r)['facts'])
   if index==3:checks['only_travel_cancelled']=any(x['id']==goal_id and x['status']=='cancelled' for x in g['goals']);checks['read_result_retained']=any(n['name'] in {'list_accounts','financial_planning_basis'} and n['status']=='completed' and not n.get('invalidated') for n in g['nodes']) and any(f['key']=='available_liquid' or f['tool']=='list_accounts' for f in rt.current_evidence(r)['facts'])
   item=dict(turn=index+1,user=text,checks=checks,passed=all(checks.values()),graph=g,reply=result['messages'][-1]['content'],api_requests=len(metrics),metrics=metrics,seconds=round(time.perf_counter()-started,3));report['turns'].append(item);dump();print(json.dumps(dict(turn=index+1,passed=item['passed'],api_requests=len(metrics))),flush=True)
 # Separate explicit operation. Only dedicated server authorization executes it.
 with factory() as db:
  s=new_session(db);sid=s.id
  with telemetry.collect() as metrics:result=create(db,s,'live','从活期账户转100元到储蓄账户，确认后再查询余额。')
  op=next((x for x in result['authorization_operations'] if x['status']=='pending_confirmation'),None)
  if not op:report['recovery']=dict(passed=False,stage='no_fixed_form',graph=result['execution_graph'],api_requests=len(metrics));dump()
  else:
   token=authorization.issue_token(db,s,result['id'],op['resource_kind'],op['resource_id'],op['action_id'])['confirmation_token'];paid=authorization.confirm(db,s,result['id'],op['resource_kind'],op['resource_id'],op['action_id'],token)
   original=protocol.step
   def fail(*a):raise DomainError('MODEL_UNAVAILABLE','验收注入回复阶段断网，真实回执保留',503)
   protocol.step=fail
   try:rt.after_authorization(db,s,result['id'],op['resource_kind'],op['resource_id'],op['action_id'],paid)
   finally:protocol.step=original
   tid=result['id'];db.rollback()
   with factory() as fresh,telemetry.collect() as recovery_metrics:
    sess=fresh.get(DemoSession,sid);task=fresh.get(AgentTask,tid);result=rt.resume_control(fresh,sess,tid,view(fresh,task));writes=fresh.scalar(select(func.count()).select_from(TransferOrder))
    checks=dict(exactly_one_write=writes==1,receipt_survives=bool(result['proposal']['receipt']),no_duplicate_prepare=sum(n['policy']['effect']=='prepare' and n['status']=='completed' for n in result['execution_graph']['nodes'])==1)
    report['recovery']=dict(passed=all(checks.values()),checks=checks,mode='real_model_before_and_after; injected_model_failure_after_commit; new_DB_session_restart',graph=result['execution_graph'],reply=result['messages'][-1]['content'],api_requests=len(metrics)+len(recovery_metrics),metrics=[*metrics,*recovery_metrics]);dump()
 report['passed']=all(x['passed'] for x in report['turns']) and bool((report['recovery'] or {}).get('passed'));report['api_requests']=sum(t['api_requests'] for t in report['turns'])+(report['recovery'] or {}).get('api_requests',0);dump()
except Exception as error:
 report['error_type']=type(error).__name__;report['error_code']=getattr(error,'code',None);report['passed']=False;dump();raise
finally:engine.dispose()
