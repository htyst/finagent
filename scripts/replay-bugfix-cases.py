"""Replay recorded public model outputs against the current app, without a provider.

This is a deterministic replay, not a fresh live-model evaluation. It never
confirms a financial operation. A copied synthetic database is required.
"""
import argparse,hashlib,json,os,sqlite3,sys
from contextlib import closing
from pathlib import Path
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser();parser.add_argument('--evidence',type=Path,required=True);parser.add_argument('--baseline',type=Path,required=True)
args=parser.parse_args();out=ROOT/'var/TEST-ONLY'/('recorded-replay-'+uuid4().hex);out.mkdir(parents=True)
with closing(sqlite3.connect(args.baseline.resolve().as_uri()+'?mode=ro',uri=True)) as a,closing(sqlite3.connect(out/'bank.sqlite3')) as b:a.backup(b)
os.environ.update(FINAGENT_DB_PATH=str(out/'bank.sqlite3'),FINAGENT_MODE='live',FINAGENT_AGENT_ENGINE='dynamic',FINAGENT_SCHEDULER_ENABLED='0',FINAGENT_SUBSCRIPTIONS_ENABLED='0',FINAGENT_BIRTHDAYS_ENABLED='0',FINAGENT_WORKFLOWS_ENABLED='0')
sys.path.insert(0,str(ROOT/'backend'))
from fastapi.testclient import TestClient
from app.main import app
from app.agent import dynamic_protocol as protocol,dynamic_runtime as rt,model_client
from app.data.db import SessionLocal
from app.data.models import AgentTask
from app.domain.errors import DomainError

def no_network(*a,**kw):raise AssertionError('Replay must not access a provider')
model_client.measured_post=no_network
records=[]
for f in args.evidence.glob('*-shard-*/provider-responses.jsonl'):
    records.extend(json.loads(line) for line in f.read_text(encoding='utf-8').splitlines())
cases={}
for name in ['cases.json','repeats.json']:
    cases.update({c['id']:c for c in json.loads((args.evidence/name).read_text(encoding='utf-8'))})
pending=[]
def step(*a):
    if not pending:raise DomainError('MODEL_REPLAY_EXHAUSTED','离线回放已结束，不能代表新模型已重新规划。',503)
    row=pending.pop(0);result={'role':'assistant','content':row.get('content')}
    if row.get('tool_calls'):result['tool_calls']=row['tool_calls']
    return result
protocol.step=step

def balances():
    with sqlite3.connect(out/'bank.sqlite3') as db:
        return hashlib.sha256(repr([db.execute('select * from '+t+' order by id').fetchall() for t in ['accounts','cards','transfer_orders','transactions']]).encode()).hexdigest()

before=balances();results=[]
for cid in ['R02','D04','E09','F03','E03']:
    case=cases[cid];stages=[]
    with TestClient(app) as client:
        client.post('/api/demo/session',json={'user_id':case.get('owner','u1')})
        headers={'Origin':'http://testserver','X-FinAgent-CSRF':client.get('/api/demo/csrf').json()['data']['csrf_token']};tid=None
        for turn,message in enumerate(case['turns'],1):
            if cid=='F03' and turn>1:break # Replay the interrupted first stage only.
            pending[:]=sorted([r for r in records if r['case']==cid and r['turn']==turn and r.get('http_status')==200],key=lambda r:r['at'])
            response=client.post('/api/tasks' if tid is None else f'/api/tasks/{tid}/messages',headers=headers,json={'message':message})
            assert response.status_code==200;result=response.json()['data'];tid=result['id']
            consent=next((n for n in result.get('next_actions',[]) if n['kind']=='confirm_subscription_query'),None)
            if consent:
                response=client.post(f'/api/tasks/{tid}/subscription-query-confirmation',headers=headers,json={'pending_id':consent['pending_id']})
                assert response.status_code==200;result=response.json()['data']
            stages.append(result)
        with SessionLocal() as db:runtime=rt.get(db.get(AgentTask,tid))
    graph=stages[-1]['execution_graph']
    passed=(stages[-1].get('proposal',{}).get('status')=='pending_confirmation' if cid=='R02' else
        any(n.get('error_code')=='ANALYSIS_JUDGMENT' for n in runtime['nodes']) if cid=='D04' else
        graph['status']=='cancelled' if cid=='E09' else
        any(g.get('user_cancelled') and '旅行' in g['text'] for g in graph['goals']) and
        '旅行成本' not in stages[-1]['messages'][-1]['content'] and '目标测算' not in stages[-1]['messages'][-1]['content'] if cid=='E03' else graph['status']!='completed')
    results.append(dict(case=cid,passed=passed,graph_status=graph['status'],stages=stages,runtime=runtime))
report=dict(mode='offline recorded public model output replay; NOT fresh real-model testing',passed=all(r['passed'] for r in results),financial_tables_unchanged=before==balances(),cases=results)
(out/'results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(dict(output=str(out),passed=report['passed'],financial_tables_unchanged=report['financial_tables_unchanged'],checks=[dict(case=r['case'],passed=r['passed'],graph_status=r['graph_status']) for r in results]),ensure_ascii=False))
raise SystemExit(0 if report['passed'] and report['financial_tables_unchanged'] else 1)
