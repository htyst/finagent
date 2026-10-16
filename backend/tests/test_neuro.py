"""Engineering acceptance only: isolated SQLite, fake model, no real provider requests."""
from datetime import datetime,timedelta
from unittest.mock import patch
import importlib
import json
import pytest
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from test_m4 import demo,login,start,say
from app.clock import configure,get_clock,now,business_day
from app.planning.schema import PlanInvalid,validate_asset
from app.planning.provider import generate
from app.verifier import verify,register,RULES,record
from app.neuro_models import VerificationRecord,StandingOrder,StandingOccurrence
from app.data.models import Account,Proposal,BusinessResult,AuditEvent
from app import standing

@pytest.fixture
def neuro(demo,monkeypatch):
    configure('manual','2026-09-24')
    monkeypatch.setenv('FINAGENT_PLANNER','model')
    for name in ('app.main','app.agent.service','app.agent.crossflow','app.agent.language','app.domain.operations','app.domain.spending'):
        monkeypatch.setattr(importlib.import_module(name),'business_day',business_day)
    return demo
def live(client,headers,message,raw):
    with patch.dict('os.environ',{'FINAGENT_MODE':'live'}),patch('app.planning.provider.request_plan',return_value=raw):
        return start(client,headers,message)
def normal_plan(amount='100'):
    return {'steps':[{'id':'read','tool':'list_accounts'},
        {'id':'write','tool':'prepare_transfer','depends_on':['read'],
         'params':{'source':'活期账户','target':'储蓄账户','amount_yuan':amount}}]}
def page_confirm(client,headers,proposal):
    token=client.post(f"/api/proposals/{proposal['id']}/confirmation-token",headers=headers).json()['data']['confirmation_token']
    response=client.post(f"/api/proposals/{proposal['id']}/confirm",headers=headers,json={'confirmation_token':token})
    assert response.status_code==200,response.json()
    return response.json()['data']
def test_r1_and_observe(neuro):
    client,engine=neuro
    result=verify('prepare_transfer',{}, {'input':'如果工资到账才转','emotion':None})
    assert not result.approved and result.violations[0].rule_id=='R1'
    @register('OBS_TEST','观察演练')
    def rule(action,params,context):
        from app.verifier import Violation
        return Violation('OBS_TEST','amount',1,0,'仅观察')
    try:
        result=verify('list_accounts',{}, {})
        assert result.approved and result.would_block[0].rule_id=='OBS_TEST'
        with Session(engine) as db:
            record(db,'u1',None,'test',result)
            db.commit()
            assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.event=='would_block'))==1
    finally:
        RULES.pop('OBS_TEST')
def test_read_write_certificate_trace(neuro):
    client,engine=neuro
    headers=login(client)
    task=live(client,headers,'查一下余额，然后从活期转100元到储蓄账户',normal_plan())
    assert task['status']=='awaiting_confirmation' and len(task['steps'])==2
    trace=client.get('/api/debug/trace/'+task['trace_id']).json()['data']
    assert trace['plan']['source']=='model' and trace['verification']['approved']
    assert trace['plan']['steps'][1]['depends_on']==['read']
    receipt=page_confirm(client,headers,task['proposal'])
    assert receipt['amount_minor']==10000
    with Session(engine) as db:
        certificates=db.scalars(select(VerificationRecord).where(VerificationRecord.phase=='confirm')).all()
        assert certificates and json.loads(certificates[-1].payload_json)['certificate']['ruleset_version']
def test_bad_plan_repair_budget(neuro):
    raw={'steps':[{'id':'bad','tool':'prepare_transfer'}]}
    calls=[]
    def generator(payload,feedback):
        calls.append(feedback)
        return raw
    with pytest.raises(PlanInvalid) as exc:
        generate({},generator=generator)
    assert len(calls)==3 and calls[1]['violations'][0]['rule_id']=='P_READ_BEFORE_WRITE'
    assert exc.value.violations[-1]['rule_id']=='P_REPAIR_LIMIT'
    sequence=iter([raw,normal_plan()])
    plan,rounds,history=generate({},generator=lambda p,f:next(sequence))
    assert rounds==1 and history and plan.steps[1].depends_on==['read']
def test_f01_trace_and_no_unconditional_proposal(neuro):
    client,engine=neuro
    headers=login(client)
    task=live(client,headers,'如果工资到账，才从活期转100元到储蓄账户',normal_plan())
    assert task['status']=='gathering_info' and task['proposal'] is None
    trace=client.get('/api/debug/trace/'+task['trace_id']).json()['data']
    assert any(v['rule_id']=='R1' for v in trace['verification']['violations'])
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Proposal))==0
def test_structured_condition_preserved_and_rechecked(neuro):
    client,engine=neuro
    headers=login(client)
    raw={'steps':[{'id':'spend','tool':'summarize_spending','params':{'category':'餐饮','time':{'type':'period','period':'this_month'}}},
        {'id':'transfer','tool':'prepare_transfer','depends_on':['spend'],
         'params':{'source':'活期','target':'储蓄','amount_yuan':'100'},
         'condition':{'step_ref':'spend','field':'net_minor','op':'gt','value':'500'}}]}
    task=live(client,headers,'如果本月餐饮超过500元，就从活期转100元到储蓄',raw)
    assert task['proposal']['summary']['condition']['threshold_minor']==50000
    assert task['proposal']['summary']['condition']['start']=='2026-09-01'
    assert page_confirm(client,headers,task['proposal'])['amount_minor']==10000
def test_clock_rolls_query_and_expires_proposals(neuro):
    client,engine=neuro
    headers=login(client)
    before=client.get('/api/spending?category=餐饮').json()['data']
    task=start(client,headers,'从活期转100元到储蓄账户')
    get_clock().advance(seconds=301)
    expired=client.get('/api/proposals/'+task['proposal']['id']).json()['data']
    assert expired['status']=='expired'
    response=client.post('/api/debug/clock/advance',headers=headers,json={'days':32})
    assert response.status_code==200,response.json()
    after=client.get('/api/spending?category=餐饮').json()['data']
    assert before['current']['period']['start_date']=='2026-09-01'
    assert after['current']['period']['start_date']=='2026-10-01'
    assert before['current']['totals']['net_minor']!=after['current']['totals']['net_minor']
    configure('real')
    assert client.get('/api/debug/clock').status_code==403
def activate(client,headers,order):
    cid=order['confirmation']['id']
    token=client.post(f'/api/standing-confirmations/{cid}/confirmation-token',headers=headers).json()['data']['confirmation_token']
    response=client.post(f'/api/standing-confirmations/{cid}/confirm',headers=headers,json={'confirmation_token':token})
    assert response.status_code==200,response.json()
    return response.json()['data']
def test_standing_full_flow_idempotency_and_insufficient_funds(neuro):
    client,engine=neuro
    headers=login(client)
    task=start(client,headers,'每月1号从活期转300元到张三尾号3821')
    order=task['standing_order']
    assert order and task['proposal'] is None
    activated=activate(client,headers,order)
    assert activated['status']=='active'
    response=client.post('/api/debug/clock/advance',headers=headers,json={'days':7})
    notifications=response.json()['data']['notifications']
    assert notifications and notifications[0]['status']=='awaiting_confirmation'
    proposal=client.get('/api/proposals/'+notifications[0]['proposal_id']).json()['data']
    page_confirm(client,headers,proposal)
    assert client.post('/api/standing-orders/tick',headers=headers).json()['data']==[]
    with Session(engine) as db:
        db.get(Account,'a1c').balance_minor=100
        db.commit()
    response=client.post('/api/debug/clock/advance',headers=headers,json={'days':31})
    assert '余额不足' in response.json()['data']['notifications'][0]['message']
    assert response.json()['data']['notifications'][0]['status']=='skipped'
def test_standing_missing_day_confirmation_and_month_end(neuro):
    client,engine=neuro
    headers=login(client)
    task=start(client,headers,'每月从活期转300元到张三尾号3821')
    assert '每月1号可以吗' in task['messages'][-1]['content']
    task=say(client,headers,task,'可以')
    assert task['standing_order']['schedule']['day']==1
    assert standing.monthly_due(2027,2,31).isoformat()=='2027-02-28'
    assert standing.monthly_due(2028,2,31).isoformat()=='2028-02-29'
def test_standing_pause_cancel_requires_page_confirmation(neuro):
    client,engine=neuro
    headers=login(client)
    task=start(client,headers,'每月1号从活期转300元到储蓄账户')
    order=activate(client,headers,task['standing_order'])
    oid=order['id']
    assert client.post(f'/api/standing-orders/{oid}/pause',headers=headers).json()['data']['status']=='paused'
    assert client.post(f'/api/standing-orders/{oid}/resume',headers=headers).json()['data']['status']=='active'
    cancellation=client.post(f'/api/standing-orders/{oid}/cancel',headers=headers).json()['data']
    assert cancellation['status']=='active'
    assert activate(client,headers,cancellation)['status']=='cancelled'
def test_multiwrite_preserved_and_confirmed_sequentially(neuro):
    client,engine=neuro
    headers=login(client)
    raw=normal_plan()
    raw['steps'].append({'id':'second','tool':'prepare_transfer','depends_on':['write'],
                        'params':{'source':'活期','target':'储蓄','amount_yuan':'200'}})
    task=live(client,headers,'查余额，然后从活期转100元到储蓄，再从活期转200元到储蓄',raw)
    first=task['proposal']
    page_confirm(client,headers,first)
    task=client.get('/api/tasks/'+task['id']).json()['data']
    assert task['proposal']['id']!=first['id'] and task['proposal']['summary']['amount_minor']==20000
    page_confirm(client,headers,task['proposal'])
    final=client.get('/api/tasks/'+task['id']).json()['data']
    assert final['status']=='completed', final
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(BusinessResult))==2
def test_schema_dag_and_absolute_dates_rejected(neuro):
    for raw in ({'steps':[{'id':'x','tool':'list_accounts','depends_on':['x']}]},
                {'steps':[{'id':'x','tool':'summarize_spending','params':{'start':'2026-09-24'}}]},
                {'steps':[{'id':'x','tool':'confirm_proposal'}]}):
        with pytest.raises(PlanInvalid):
            validate_asset(raw)
def test_urgent_emotion_and_owner_trace_isolation(neuro):
    client,engine=neuro
    headers=login(client)
    raw={'steps':[{'id':'read','tool':'list_accounts'}],
         'emotion':{'state':'urgent','confidence':0.9,'evidence':'赶紧'}}
    task=live(client,headers,'赶紧查余额',raw)
    trace=client.get('/api/debug/trace/'+task['trace_id']).json()['data']
    assert trace['emotion_flag'] and trace['understanding']['emotion']['state']=='urgent'
    login(client,'u2')
    assert client.get('/api/debug/trace/'+task['trace_id']).status_code==404
def test_snapshot_rule_still_blocks_execution(neuro):
    client,engine=neuro
    headers=login(client)
    task=live(client,headers,'从活期转100元到储蓄',normal_plan())
    with Session(engine) as db:
        db.get(Account,'a1c').version+=1
        db.commit()
    pid=task['proposal']['id']
    token=client.post(f'/api/proposals/{pid}/confirmation-token',headers=headers).json()['data']['confirmation_token']
    response=client.post(f'/api/proposals/{pid}/confirm',headers=headers,json={'confirmation_token':token})
    assert response.status_code==409 and response.json()['error_code']=='STALE_RESOURCE'


def test_model_cannot_use_condition_threshold_as_transfer_amount(neuro):
    client,_=neuro
    headers=login(client)
    raw={'steps':[{'id':'spend','tool':'summarize_spending','params':{'category':'餐饮'}},
        {'id':'write','tool':'prepare_transfer','depends_on':['spend'],
         'params':{'source':'活期','target':'储蓄','amount_yuan':'500'},
         'condition':{'step_ref':'spend','field':'net_minor','op':'gt','value':'500'}}]}
    task=live(client,headers,'如果本月餐饮超过500元，就从活期转100元到储蓄',raw)
    assert task['proposal'] is None
    trace=client.get('/api/debug/trace/'+task['trace_id']).json()['data']
    assert any(v['rule_id']=='P_AMOUNT_BINDING' for v in trace['verification']['violations'])

def test_mock_condition_with_spending_words_does_not_turn_into_query(neuro):
    client,_=neuro
    headers=login(client)
    task=start(client,headers,'若本月餐饮至少500元才从活期转100元到储蓄账户')
    assert task['proposal'] is None and task['status']=='gathering_info'

def test_live_missing_source_then_followup_keeps_slots(neuro):
    client,_=neuro
    headers=login(client)
    raw=normal_plan()
    raw['steps'][1]['params']['source']=None
    task=live(client,headers,'查余额然后转100元到储蓄账户',raw)
    assert task['status']=='gathering_info'
    with patch('app.planning.provider.request_plan',return_value=normal_plan()):
        task=say(client,headers,task,'从活期账户')
    assert task['proposal']['summary']['amount_minor']==10000

def test_month_end_real_scheduler_occurrence(neuro):
    client,_=neuro
    get_clock().set('2027-02-01')
    headers=login(client)
    task=start(client,headers,'每月31号从活期转300元到储蓄账户')
    activate(client,headers,task['standing_order'])
    response=client.post('/api/debug/clock/advance',headers=headers,json={'days':27})
    assert response.json()['data']['notifications'][0]['status']=='awaiting_confirmation'

def test_recurring_chat_management_does_not_cancel_from_chat(neuro):
    client,_=neuro
    headers=login(client)
    task=start(client,headers,'每月1号从活期转300元到储蓄账户')
    order=activate(client,headers,task['standing_order'])
    paused=start(client,headers,'暂停定期转账')
    assert paused['status']=='completed'
    start(client,headers,'恢复定期转账')
    cancellation=start(client,headers,'取消定期转账')
    assert cancellation['standing_order']['status']=='active'
    assert cancellation['status']=='awaiting_confirmation'
    assert activate(client,headers,cancellation['standing_order'])['status']=='cancelled'


def test_plan_provider_outage_announces_single_scenario_fallback(neuro):
    client,_=neuro
    headers=login(client)
    from app.domain.errors import DomainError
    with patch.dict('os.environ',{'FINAGENT_MODE':'live'}),patch('app.planning.provider.request_plan',side_effect=DomainError('MODEL_UNAVAILABLE','模型不可用',503)):
        task=start(client,headers,'从活期转100元到储蓄账户')
    assert task['proposal'] and '已回退旧单场景模板' in task['messages'][-1]['content']

def test_period_cannot_silently_change_to_default(neuro):
    client,_=neuro
    headers=login(client)
    raw={'steps':[{'id':'spend','tool':'summarize_spending','params':{'category':'餐饮'}}]}
    task=live(client,headers,'查上个月餐饮消费',raw)
    assert task['status']=='gathering_info'
    trace=client.get('/api/debug/trace/'+task['trace_id']).json()['data']
    assert any(v['rule_id']=='P_TIME_GROUNDING' for v in trace['verification']['violations'])


def test_calendar_jump_discovers_due_month_instead_of_dropping_it(neuro):
    client,_=neuro
    headers=login(client)
    task=start(client,headers,'每月1号从活期转300元到储蓄账户')
    activate(client,headers,task['standing_order'])
    response=client.post('/api/debug/clock/advance',headers=headers,json={'days':32})
    assert response.json()['data']['notifications'][0]['period']=='2026-10'
    assert response.json()['data']['notifications'][0]['status']=='awaiting_confirmation'

def test_scheduler_survives_expired_user_session_and_claim_uses_normal_page_flow(neuro):
    client,engine=neuro
    headers=login(client)
    task=start(client,headers,'每月1号从活期转300元到储蓄账户')
    activate(client,headers,task['standing_order'])
    get_clock().advance(days=7)
    with Session(engine) as db:
        standing.tick_active_sessions(db)
        occurrence=db.scalar(select(StandingOccurrence))
        occurrence_id=occurrence.id
        assert occurrence.status=='awaiting_confirmation'
        assert db.scalar(select(func.count()).select_from(BusinessResult))==0
    headers=login(client)
    response=client.post(f'/api/standing-occurrences/{occurrence_id}/claim',headers=headers)
    assert response.status_code==200,response.json()
    proposal=response.json()['data']['proposal']
    page_confirm(client,headers,proposal)
    again=client.post(f'/api/standing-occurrences/{occurrence_id}/claim',headers=headers)
    assert again.json()['data']['status']=='executed'
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(BusinessResult))==1

@pytest.mark.parametrize('amount',['-100元','1.234元','负一百元','一百二元'])
def test_invalid_or_ambiguous_amount_never_becomes_positive_different_amount(neuro,amount):
    client,_=neuro
    task=start(client,login(client),'从活期转'+amount+'到储蓄账户')
    assert task['proposal'] is None and task['status']=='gathering_info'


def test_plan_get_task_status_uses_existing_owned_registry_tool(neuro):
    client,_=neuro
    headers=login(client)
    pending=start(client,headers,'从活期转100元到储蓄账户')
    raw={'steps':[{'id':'status','tool':'get_task_status','params':{'task_id':pending['proposal']['task_id']}}]}
    result=live(client,headers,'查询当前操作任务的状态',raw)
    assert result['steps'][0]['result']['id']==pending['proposal']['task_id']
    assert result['status']=='completed'

def test_recurrence_on_read_step_is_schema_semantic_error(neuro):
    raw={'steps':[{'id':'read','tool':'list_accounts','recurrence':{'type':'recurring','freq':'monthly','day':1}},
                  {'id':'write','tool':'prepare_transfer','depends_on':['read']}]}
    with pytest.raises(PlanInvalid) as error:
        validate_asset(raw)
    assert any(v['rule_id']=='P_RECURRENCE' for v in error.value.violations)

@pytest.mark.parametrize('amount',['100元5角','两块五'])
def test_compound_money_is_clarified_instead_of_truncated(neuro,amount):
    client,_=neuro
    result=start(client,login(client),'从活期转'+amount+'到储蓄账户')
    assert result['proposal'] is None and result['status']=='gathering_info'
