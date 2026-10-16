import pytest
from test_m4 import demo,login,start
from test_neuro import neuro,live,normal_plan
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
def test_multitask_dependency_variants(neuro,word):
    client,_=neuro
    task=live(client,login(client),'查余额，'+word+'从活期转100元到储蓄账户',normal_plan())
    assert len(task['steps'])==2
    assert task['steps'][0]['tool_name']=='list_accounts'
    assert task['steps'][1]['depends_on']==[1]
    assert task['proposal']['summary']['amount_minor']==10000
def test_metric_counts_failures_and_announced_fallback_correctly():
    result=silent_degradation_rate([
        {'id':'silent','claimed_success':True,'semantics_preserved':False},
        {'id':'safe','claimed_success':False,'clarification':True},
        {'id':'fallback','claimed_success':True,'explicit_fallback':True}])
    assert result['silent_degradation_rate']==pytest.approx(1/3)
    print(result)
