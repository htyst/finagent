"""Bounded real-provider checks on a copied synthetic database; never confirm money."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from uuid import uuid4

# Assistant replies contain bullets and Chinese text; a legacy Windows console
# (cp936) would otherwise abort this harness mid-run with UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8')
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--cases', type=Path, required=True)
parser.add_argument('--label', default='run')
parser.add_argument('--max-requests', type=int, default=24)
parser.add_argument('--max-prompt-tokens', type=int, default=800000)
args = parser.parse_args()
RUN = ROOT/'var/TEST-ONLY'/('complex-live-'+args.label+'-'+uuid4().hex)
RUN.mkdir(parents=True)
BASE = ROOT/'var/TEST-ONLY/f01-20261017/baseline.sqlite3'
with closing(sqlite3.connect(BASE.as_uri()+'?mode=ro', uri=True)) as a, closing(sqlite3.connect(RUN/'bank.sqlite3')) as b:
    a.backup(b)
os.environ.update(FINAGENT_DB_PATH=str(RUN/'bank.sqlite3'), FINAGENT_MODE='live',
    FINAGENT_AGENT_ENGINE='dynamic', FINAGENT_SCHEDULER_ENABLED='0',
    FINAGENT_SUBSCRIPTIONS_ENABLED='0', FINAGENT_BIRTHDAYS_ENABLED='0', FINAGENT_WORKFLOWS_ENABLED='0')
sys.path.insert(0, str(ROOT/'backend'))
from fastapi.testclient import TestClient
from app.main import app
from app.agent import model_client, dynamic_runtime as rt, authorization
from app.data.db import SessionLocal
from app.data.models import AgentTask
from app.domain import operations, wealth, card_management, subscriptions, birthdays, scheduled_transfers, collections, servicing
from app.domain.errors import DomainError

def forbidden(*a, **kw):
    raise DomainError('EVAL_FINANCIAL_EXECUTION_DISABLED', '隔离评测不签发授权或提交金融操作。', 403)

for module, names in [(authorization,['issue_token','confirm']), (operations,['issue_confirmation_token','confirm_proposal']),
    (wealth,['issue_token','confirm_order']), (card_management,['issue_token','confirm']),
    (subscriptions,['issue_token','confirm']), (birthdays,['issue_token','confirm']),
    (scheduled_transfers,['issue_schedule_confirmation_token','confirm_schedule']),
    (collections,['issue_token','confirm_collection']), (servicing,['issue_token','confirm'])]:
    for name in names:
        if hasattr(module,name): setattr(module,name,forbidden)

NETWORK=[]
CURRENT={}
original=model_client.measured_post
def measured(http,url,**kw):
    tokens=sum((n.get('usage') or {}).get('prompt_tokens',0) for n in NETWORK)
    if len(NETWORK)>=args.max_requests or tokens>=args.max_prompt_tokens:
        raise DomainError('EVAL_COST_LIMIT', '本次真实评测达到调用或输入预算，停止新增请求。', 503)
    started=time.monotonic()
    response=original(http,url,**kw)
    row=dict(CURRENT,http_status=response.status_code,seconds=round(time.monotonic()-started,3))
    if response.status_code==200:
        data=response.json(); message=data.get('choices',[{}])[0].get('message',{})
        row.update(usage=data.get('usage'),content=message.get('content'),tool_calls=message.get('tool_calls'))
    NETWORK.append(row)
    with (RUN/'public-model-output.jsonl').open('a',encoding='utf-8') as f: f.write(json.dumps(row,ensure_ascii=False)+'\n')
    return response
model_client.measured_post=measured

PROTECTED=['accounts','cards','transactions','transfer_orders','financial_events','subscription_authorizations','subscription_charges','birthday_fund_events']
def snapshot():
    with sqlite3.connect(RUN/'bank.sqlite3') as db:
        tables={r[0] for r in db.execute("select name from sqlite_master where type='table'")}
        return {t:hashlib.sha256(repr(db.execute('select * from '+t+' order by rowid').fetchall()).encode()).hexdigest() for t in PROTECTED if t in tables}

source={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'backend/app').rglob('*.py')}
report={'mode':'real-provider+isolated-database; no financial confirmation','run':str(RUN),'cases':[],
    'source_fingerprints':source,'financial_execution_disabled':True}
before=snapshot()
def save():
    report.update(requests=len(NETWORK),prompt_tokens=sum((n.get('usage') or {}).get('prompt_tokens',0) for n in NETWORK),
        completion_tokens=sum((n.get('usage') or {}).get('completion_tokens',0) for n in NETWORK),
        financial_tables_unchanged=before==snapshot(),source_unchanged=all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in source.items()))
    (RUN/'results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')

print(json.dumps({'output':str(RUN)}),flush=True)
for case in json.loads(args.cases.read_text(encoding='utf-8-sig')):
    if len(NETWORK)>=args.max_requests or any(n['http_status']==402 for n in NETWORK): break
    row={'id':case['id'],'turns':[]};report['cases'].append(row)
    with TestClient(app,raise_server_exceptions=False) as client:
        assert client.post('/api/demo/session',json={'user_id':case.get('owner','u1')}).status_code==200
        headers={'Origin':'http://testserver','X-FinAgent-CSRF':client.get('/api/demo/csrf').json()['data']['csrf_token']}
        tid=None
        for number,text in enumerate(case['turns'],1):
            CURRENT.update(case=case['id'],turn=number)
            response=client.post('/api/tasks' if tid is None else f'/api/tasks/{tid}/messages',headers=headers,json={'message':text})
            result=response.json().get('data') or {};tid=result.get('id',tid)
            stages=[result]
            for _ in range(2):
                consent=next((a for a in result.get('next_actions',[]) if a['kind']=='confirm_subscription_query'),None)
                if not consent:break
                response=client.post(f'/api/tasks/{tid}/subscription-query-confirmation',headers=headers,json={'pending_id':consent['pending_id']})
                result=response.json().get('data') or {};stages.append(result)
            with SessionLocal() as db:
                runtime=rt.get(db.get(AgentTask,tid)) if tid else {}
            reply=next((m['content'] for m in reversed(result.get('messages',[])) if m['role']=='assistant'),'')
            entry=dict(user=text,http_status=response.status_code,reply=reply,result=result,runtime=runtime,stages=stages)
            row['turns'].append(entry)
            save()
            print(json.dumps({'case':case['id'],'turn':number,'status':result.get('status'),
                'graph_status':result.get('execution_graph',{}).get('status'),'reply':reply[:180],
                'errors':[n.get('error_code') for n in (runtime or {}).get('nodes',[]) if n.get('error_code') and not n.get('invalidated')],
                'requests':len(NETWORK),'prompt_tokens':report['prompt_tokens']},ensure_ascii=False),flush=True)
            if len(NETWORK)>=args.max_requests or any(n['http_status']==402 for n in NETWORK):break
    save()
save()
assert report['financial_tables_unchanged']
print(json.dumps({k:report[k] for k in ('run','requests','prompt_tokens','completion_tokens','financial_tables_unchanged','source_unchanged')}),flush=True)
