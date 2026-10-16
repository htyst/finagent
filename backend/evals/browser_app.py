"""TEST-ONLY real-provider browser server, with a hard global HTTP budget.

Not imported/registered by the normal application. No alternate authorization.
"""
import os
from pathlib import Path
from threading import Lock
from app.main import app
from app.agent import model_client
from app.domain.errors import DomainError

database=Path(os.environ.get('FINAGENT_DB_PATH','')).resolve()
if 'TEST-ONLY' not in database.parts or os.environ.get('FINAGENT_MODE') != 'live':
    raise RuntimeError('This evaluation entry point requires an isolated Live TEST-ONLY database')
limit=int(os.environ.get('FINAGENT_E2E_API_BUDGET','30'))
if not 1 <= limit <= 60: raise RuntimeError('Invalid evaluation budget')
lock=Lock(); records=[]
original=model_client.measured_post


def measured(http,url,**kwargs):
    with lock:
        if len(records)>=limit:raise DomainError('EVALUATION_BUDGET_EXCEEDED','浏览器真实API调用预算已用完',503)
        item={'http_status':None,'usage':None};records.append(item)
    response=original(http,url,**kwargs)
    item['http_status']=response.status_code
    if response.status_code==200:
        item['usage']={k:v for k,v in response.json().get('usage',{}).items()
                       if k in {'prompt_tokens','completion_tokens','total_tokens'} and type(v) is int}
    return response


model_client.measured_post=measured


@app.get('/api/evaluation/metrics')
def metrics():
    return {'mode':'real_deepseek_browser','api_budget':limit,'http_attempts':len(records),
            'usage_records':sum(bool(r['usage']) for r in records),
            'tokens':{k:sum((r['usage'] or {}).get(k,0) for r in records) for k in ('prompt_tokens','completion_tokens','total_tokens')}}
