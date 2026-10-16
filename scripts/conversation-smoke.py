"""Live regression on a dedicated synthetic DB under var/conversation-review. Run from repo root."""
import json, os
from pathlib import Path
os.environ['FINAGENT_MODE'] = 'live'
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from app.main import app
from app.data.db import make_engine, get_db
from app.data.models import Base, TransferOrder
from app.data.seed import reset_demo
from app.agent.telemetry import collect

path = Path('var/conversation-review/live.sqlite3').resolve()
engine = make_engine(path)
Base.metadata.create_all(engine)
with Session(engine) as db:
    reset_demo(db)
def override():
    with Session(engine) as db:
        yield db
app.dependency_overrides[get_db] = override
report = []
with TestClient(app) as client:
    client.post('/api/demo/session', json={'user_id':'u1'})
    token = client.get('/api/demo/csrf').json()['data']['csrf_token']
    headers = {'Origin':'http://testserver','X-FinAgent-CSRF':token}
    task = None
    cases = [
        ('介绍一下你的功能', None),
        ('查询', None),
        ('我想去日本玩，你在资金消费方面有什么建议', None),
        ('两个人，玩7天，总预算一万元，包含机票住宿', None),
        ('改成5天呢', None),
        ('这个月吃饭花了多少', 'summarize_spending'),
        ('那上个月呢', 'summarize_spending'),
        ('我还有多少钱可以用', 'list_accounts'),
        ('从活期给张三打一百块', None),
        ('3821那个', 'prepare_transfer'),
        ('这笔要转多少钱？', None),
        ('改成两百块', 'prepare_transfer'),
        ('确认', None),
        ('算了', None),
    ]
    for message, expected_tool in cases:
        active = task and task['status'] not in ['completed','cancelled','expired','failed','partially_completed']
        endpoint = f"/api/tasks/{task['id']}/messages" if active else '/api/tasks'
        with collect() as metrics:
            response = client.post(endpoint, headers=headers, json={'message':message})
        assert response.status_code == 200, response.text
        task = response.json()['data']
        reply = task['messages'][-1]['content']
        record = {'input':message,'reply':reply,'status':task['status'],'tools':[s['tool_name'] for s in task['steps']], 'metrics':metrics}
        if expected_tool:
            assert expected_tool in record['tools'], record
        if message in ['介绍一下你的功能','查询','我想去日本玩，你在资金消费方面有什么建议','两个人，玩7天，总预算一万元，包含机票住宿']:
            assert not task['steps'] and not task['proposal'], record
        if message == '两个人，玩7天，总预算一万元，包含机票住宿':
            assert '214.29' in reply and '合计 10,000.00' in reply, record
        if message == '改成5天呢':
            assert '300.00' in reply and '2 人、5 天' in reply, record
        if message == '那上个月呢':
            assert task['steps'][0]['result']['current']['period']['start_date'] == '2026-08-01', record
            assert '800.00' in reply, record
        if message == '从活期给张三打一百块':
            assert not task['proposal'] and '3821' in reply and '9156' in reply, record
        if message in ['3821那个','这笔要转多少钱？']:
            assert task['proposal']['summary']['amount_minor'] == 10000, record
        if message == '改成两百块':
            assert task['proposal']['summary']['amount_minor'] == 20000, record
        print(json.dumps(record,ensure_ascii=False), flush=True)
        report.append(record)
        Path('var/conversation-review/live-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    with Session(engine) as db:
        assert db.query(TransferOrder).count() == 0
app.dependency_overrides.clear()
engine.dispose()
print('LIVE CONVERSATION CHECK PASSED', flush=True)
