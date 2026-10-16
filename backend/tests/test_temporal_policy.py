"""Backend temporal decisions, with deliberately misleading fake model outputs."""
from unittest.mock import patch
import pytest
from test_m4 import demo,login,start,say
from test_neuro import neuro,live,normal_plan
from app.planning.temporal import resolve

QUESTION='每月转账需要固定日期，设置为每月1号可以吗？'
def trace(client,task):
    return client.get('/api/debug/trace/'+task['trace_id']).json()['data']
def reply(client,headers,task,text,raw):
    with patch.dict('os.environ',{'FINAGENT_MODE':'live'}),patch('app.planning.provider.request_plan',return_value=raw):
        return say(client,headers,task,text)
def question_asset():
    return {'steps':[],'clarification':'请问是单次还是每月转账？'}

@pytest.mark.parametrize('text,expected',[
    ('每月转300给张三',True),('每月15号转300给张三',False),('转300给张三',False)])
def test_three_required_acceptance_cases(neuro,text,expected):
    client,_=neuro
    task=live(client,login(client),text,question_asset())
    data=trace(client,task)
    assert (task['messages'][-1]['content']==QUESTION)==expected
    assert bool(data['verification']['clarifications'])==expected
    assert task['proposal'] is None
    if expected:
        assert data['verification']['clarifications'][0]['rule_id']=='R10'
        assert not data['verification']['approved']
    else:
        assert '每月' not in task['messages'][-1]['content']

@pytest.mark.parametrize('day',[None,1,15])
def test_model_cannot_invent_monthly_or_date(neuro,day):
    client,_=neuro
    raw=normal_plan('300')
    raw['recurrence']={'type':'recurring','freq':'monthly','day':day}
    task=live(client,login(client),'从活期转300元到储蓄账户',raw)
    assert task['status']=='awaiting_confirmation'
    assert task['standing_order'] is None
    assert trace(client,task)['plan']['recurrence'] is None

def test_model_default_is_reset_and_backend_asks(neuro):
    client,_=neuro
    raw=normal_plan('300')
    raw['recurrence']={'type':'recurring','freq':'monthly','day':1}
    task=live(client,login(client),'每月从活期转300元到储蓄账户',raw)
    assert task['messages'][-1]['content']==QUESTION
    assert trace(client,task)['plan']['recurrence']['day'] is None
    assert task['standing_order'] is None and task['proposal'] is None

def test_accept_default_requires_backend_question(neuro):
    client,_=neuro
    h=login(client)
    task=live(client,h,'每月从活期转300元到储蓄账户',normal_plan('300'))
    task=reply(client,h,task,'可以',normal_plan('300'))
    assert task['standing_order'] is not None
    assert trace(client,task)['temporal']['intent']['accepted_default']
    assert trace(client,task)['plan']['recurrence']['day']==1

@pytest.mark.parametrize('reject',['不行','不要1号','不选1号'])
def test_reject_default_then_supply_date(neuro,reject):
    client,_=neuro
    h=login(client)
    task=live(client,h,'每月从活期转300元到储蓄账户',normal_plan('300'))
    task=reply(client,h,task,reject,normal_plan('300'))
    assert task['messages'][-1]['content']=='请告诉我每月几号转账（1至31号）。'
    assert task['standing_order'] is None
    assert trace(client,task)['plan']['recurrence']['day'] is None
    task=reply(client,h,task,'15号',normal_plan('300'))
    assert task['standing_order'] is not None
    assert trace(client,task)['plan']['recurrence']['day']==15

def test_switch_to_single_clears_monthly(neuro):
    client,_=neuro
    h=login(client)
    task=live(client,h,'每月从活期转300元到储蓄账户',normal_plan('300'))
    raw=normal_plan('300')
    raw['recurrence']={'type':'recurring','freq':'monthly','day':1}
    task=reply(client,h,task,'不需要每月，只转一次',raw)
    assert task['status']=='awaiting_confirmation' and task['standing_order'] is None
    assert trace(client,task)['plan']['recurrence'] is None

def test_assistant_cannot_create_monthly_intent_or_default():
    context={}
    messages=[{'role':'user','content':'转300给张三'},
        {'role':'assistant','content':QUESTION},{'role':'user','content':'可以'}]
    assert resolve(messages,context)['recurrence'] is None
    messages[0]['content']='每月转300给张三'
    state=resolve(messages,{})
    assert state['recurrence']['day'] is None and not state['accepted_default']
