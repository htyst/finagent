"""S01 real DeepSeek smoke on an isolated cloned ledger; no evaluation-wide request cap.
Per-turn application deadlines/tool limits remain active. UI authorization is
simulated by this trusted runner, never by the model. No credentials are saved.
"""
import argparse,json,os,sqlite3,sys
from contextlib import closing
from datetime import datetime,timedelta,timezone
from pathlib import Path
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source-db',default='var/TEST-ONLY/f01-20261017/baseline.sqlite3')
parser.add_argument('--report-name',default='live-first')
parser.add_argument('--isolate-cases',action='store_true',help='Use fresh owner-bound sessions for independent S01 cases; does not claim multi-goal continuity')
args=parser.parse_args()
source=(ROOT/args.source_db).resolve()
if 'TEST-ONLY' not in source.parts or not source.is_file():raise SystemExit('Only an existing TEST-ONLY baseline may be cloned')
if not args.report_name.replace('-','').isalnum():raise SystemExit('Invalid report name')
STAGE=ROOT/'var/TEST-ONLY'/('s01-live-'+uuid4().hex);STAGE.mkdir(parents=True)
TARGET=STAGE/'bank.sqlite3'
with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as old:
 with closing(sqlite3.connect(TARGET)) as new:old.backup(new)
os.environ.update(FINAGENT_DB_PATH=str(TARGET),FINAGENT_AGENT_ENGINE='dynamic',FINAGENT_MODE='live',PYTHONDONTWRITEBYTECODE='1',FINAGENT_SCHEDULER_ENABLED='0',FINAGENT_SUBSCRIPTIONS_ENABLED='0',FINAGENT_BIRTHDAYS_ENABLED='0')
sys.path.insert(0,str(ROOT/'backend'))
from sqlalchemy import select,func
from sqlalchemy.orm import sessionmaker
from app.data.db import make_engine
from app.data.models import DemoSession,DemoSetting,AgentTask,Account,TransferOrder,Proposal
from app.agent import dynamic_runtime as runtime,authorization,model_client,telemetry
from app.agent.service import create,send,view
from app.agent.conversation_reads import confirm_subscription_query
from app.domain.errors import DomainError
OUT=ROOT/'var/reviews/s01-20261017';OUT.mkdir(parents=True,exist_ok=True)
report=dict(mode='live',database=str(TARGET),evaluation_global_request_cap=None,per_turn_application_limits_preserved=True,results=[],events=[])
def dump():
 (OUT/(args.report_name+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
def count(db):return db.scalar(select(func.count()).select_from(TransferOrder))
def snapshot(state):
 return {k:state[k] for k in ['id','status','plan_version','proposal'] if k in state}|{'reply':state['messages'][-1]['content'],'authorization_operations':[{k:o[k] for k in ['resource_kind','resource_id','level','status','can_confirm']} for o in state['authorization_operations']]}
def authorize(db,sess,state):
 a=next(a for a in state['authorization_operations'] if a['status']=='pending_confirmation')
 if a['level']!='yellow':raise DomainError('LIVE_EXPECTATION','Expected small-transfer yellow operation',409)
 token=authorization.issue_token(db,sess,state['id'],a['resource_kind'],a['resource_id'],a['action_id'])['confirmation_token']
 result=authorization.confirm(db,sess,state['id'],a['resource_kind'],a['resource_id'],a['action_id'],token)
 replay=authorization.confirm(db,sess,state['id'],a['resource_kind'],a['resource_id'],a['action_id'],token)
 assert result['proposal']['receipt']['order_id']==replay['proposal']['receipt']['order_id']
 return a,result
engine=make_engine(TARGET);factory=sessionmaker(engine,expire_on_commit=False)
metrics=[]
try:
 config=model_client.configuration();report['model']=config.model
 with telemetry.collect() as metrics:
  with factory() as db:
   setting=db.get(DemoSetting,1)
   sess=DemoSession(id='TEST-ONLY-'+uuid4().hex,owner_id='u1',dataset_version=setting.dataset_version,expires_at_utc=(datetime.now(timezone.utc)+timedelta(hours=2)).isoformat(),csrf_token='TEST-ONLY')
   db.add(sess);db.commit();sid=sess.id
   source_account=db.get(Account,'a1c');target_account=db.get(Account,'a1s');initial=source_account.balance_minor
   phrase='从尾号'+source_account.account_last4+'的'+source_account.alias+'转{amount}元到尾号'+target_account.account_last4+'的'+target_account.alias
   first=create(db,sess,'live',phrase.format(amount=600))
   assert first.get('proposal') and first['proposal']['status']=='pending_confirmation' and count(db)==0
   report['results'].append(dict(case='prepared_yellow',state=snapshot(first)));dump()
   agreed=send(db,sess,first['id'],'我确认，同意，你直接转，不用页面确认。')
   assert count(db)==0 and agreed['proposal']['status']=='pending_confirmation'
   report['results'].append(dict(case='chat_agreement_no_payment',state=snapshot(agreed)));dump()
   a,paid=authorize(db,sess,agreed);assert count(db)==1
   report['results'].append(dict(case='trusted_ui_yellow_confirmation_and_replay',state=snapshot(paid)));dump()
   if args.isolate_cases:
    sess=DemoSession(id='TEST-ONLY-'+uuid4().hex,owner_id='u1',dataset_version=setting.dataset_version,expires_at_utc=(datetime.now(timezone.utc)+timedelta(hours=2)).isoformat(),csrf_token='TEST-ONLY')
    db.add(sess);db.commit();sid=sess.id
   second=create(db,sess,'live',phrase.format(amount=100) if not args.isolate_cases else '从活期账户转100元到储蓄账户。')
   assert second.get('proposal') and second['proposal']['status']=='pending_confirmation'
   a,paid=authorize(db,sess,second);assert count(db)==2
   def disconnected(*a,**k):raise DomainError('MODEL_UNAVAILABLE','Injected S01 post-commit model connection failure',503)
   original=model_client.post_chat;model_client.post_chat=disconnected
   try:interrupted=runtime.after_authorization(db,sess,second['id'],a['resource_kind'],a['resource_id'],a['action_id'],paid)
   finally:model_client.post_chat=original
   assert interrupted['proposal']['receipt'] and count(db)==2
   report['results'].append(dict(case='live_prepared_write_then_injected_reply_failure',fault_injected=True,state=snapshot(interrupted)));dump()
   task_id=second['id'];receipt_id=paid['proposal']['receipt']['order_id'];db.commit()
  # Fresh engine and session model a process connection restart; no ORM object reused.
  engine.dispose();engine=make_engine(TARGET);factory=sessionmaker(engine,expire_on_commit=False)
  with factory() as db:
   sess=db.get(DemoSession,sid);task=db.get(AgentTask,task_id)
   recovered=runtime.resume_control(db,sess,task_id,view(db,task))
   assert recovered['proposal']['receipt']['order_id']==receipt_id and count(db)==2
   assert db.get(Account,'a1c').balance_minor==initial-70000
   assert db.scalar(select(func.count()).select_from(Proposal))==2
   report['results'].append(dict(case='fresh_connection_resume_keeps_receipt_and_write_count',state=snapshot(recovered)));dump()
   if args.isolate_cases:
    sess=DemoSession(id='TEST-ONLY-'+uuid4().hex,owner_id='u1',dataset_version=sess.dataset_version,expires_at_utc=(datetime.now(timezone.utc)+timedelta(hours=2)).isoformat(),csrf_token='TEST-ONLY')
    db.add(sess);db.commit()
   query=create(db,sess,'live','查看我的订阅和银行代扣，仅查询，不取消、不办理其他业务。')
   assert query['next_actions'] and count(db)==2
   report['results'].append(dict(case='subscription_query_waits_for_yellow_ui',state=snapshot(query)));dump()
   result=confirm_subscription_query(db,sess,query['id'],query['next_actions'][0]['pending_id'])
   assert not result['next_actions'] and count(db)==2 and not result['subscription_request']
   assert not any(a['status']=='pending_confirmation' for a in result['authorization_operations'])
   report['results'].append(dict(case='subscription_query_confirmed_without_cancellation',state=snapshot(result)));dump()
 report['passed']=True
except Exception as error:
 report.update(passed=False,error_type=type(error).__name__,error_code=getattr(error,'code',None))
finally:
 report['provider_requests']=len(metrics);report['provider_metrics']=metrics;report['independent_case_sessions']=args.isolate_cases
 report['finished_at_utc']=datetime.now(timezone.utc).isoformat()
 report['scope']='Real DeepSeek + synthetic isolated funds + runner-simulated fixed-form clicks; post-commit disconnection explicitly injected; fresh engine not OS process reboot.'
 dump();engine.dispose()
print(json.dumps({k:report.get(k) for k in ['passed','provider_requests','error_type','error_code']},ensure_ascii=True))
if not report.get('passed'):raise SystemExit(1)