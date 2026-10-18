import pytest
from test_m4 import demo,login,start
from test_neuro import neuro
from test_dynamic_tools import calls, sequence, session
from app.agent.service import create
from app.planning.metrics import silent_degradation_rate

@pytest.mark.parametrize('message',[
    '从活期账户转100元到储蓄账户','从活期转给储蓄账户100元',
    '从活期账户转入100元到储蓄账户','从活期账户转到储蓄账户100元'])
def test_transfer_variants(demo,message):
    client,_=demo
    task=start(client,login(client),message)
    assert task['proposal']['summary']['amount_minor']==10000
@pytest.mark.parametrize('amount',['一百元','一百块钱','100块','100圆'])
def test_amount_variants(demo,amount):
    client,_=demo
    task=start(client,login(client),'从活期账户转'+amount+'到储蓄账户')
    assert task['proposal']['summary']['amount_minor']==10000
@pytest.mark.parametrize('marker',['若','如果','至少','不超过'])
def test_unsupported_conditions_cannot_drop(demo,marker):
    client,_=demo
    task=start(client,login(client),marker+'工资到账才从活期账户转100元到储蓄账户')
    assert task['proposal'] is None and task['status']=='gathering_info'
@pytest.mark.parametrize('word',['最大','最小','最多','最贵'])
def test_extreme_queries_clarify(demo,word):
    client,_=demo
    task=start(client,login(client),'本月餐饮'+word+'的消费')
    assert task['status']=='gathering_info' and '统计对象' in task['messages'][-1]['content']
@pytest.mark.parametrize('word',['然后','再','接着','并且'])
def test_multitask_dependency_variants(neuro,monkeypatch,word):
    db,_=neuro
    def after_read(messages,context):
        import json
        observation=json.loads(messages[-1]['content'])
        assert observation['status']=='completed'
        return calls(('prepare_transfer',{'source':'活期账户','target':'储蓄账户','amount_yuan':'100'}))
    sequence(monkeypatch,calls(('list_accounts',{})),after_read)
    task=create(db,session(db),'live','查余额，'+word+'从活期账户转100元到储蓄账户')
    assert [step['tool_name'] for step in task['steps']]==['list_accounts','prepare_transfer']
    assert task['proposal']['summary']['amount_minor']==10000
    assert task['status']=='awaiting_confirmation'
def test_metric_counts_failures_and_announced_fallback_correctly():
    result=silent_degradation_rate([
        {'id':'silent','claimed_success':True,'semantics_preserved':False},
        {'id':'safe','claimed_success':False,'clarification':True},
        {'id':'fallback','claimed_success':True,'explicit_fallback':True}])
    assert result['silent_degradation_rate']==pytest.approx(1/3)
    print(result)
