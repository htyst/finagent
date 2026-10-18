"""G01 phase 1: contracts and real serial runtime, synthetic funds only."""
import json
from copy import deepcopy
import pytest
from sqlalchemy import select,func
from app.agent import execution_graph as graph,dynamic_runtime as runtime,authorization
from app.agent.service import create,send,view
from app.agent.control import change
from app.data.models import AgentTask,DemoSession,TransferOrder
from app.domain.errors import DomainError
from test_dynamic_tools import dynamic,sequence,calls,reply,session,state,transfers


def goals(*items):
    return calls(('declare_goals',{'goals':[dict(id=i,text=text,quote=text,kind=kind) for i,text,kind in items]}))

def linked(name,args=None,*ids):return (name,dict(args or {},execution={'goal_ids':list(ids)}))
def evidence(name,result):
    from pathlib import Path
    out=Path('var/reviews/g01-phase1-20261017/traces');out.mkdir(parents=True,exist_ok=True)
    (out/(name+'.json')).write_text(json.dumps(dict(mode='model_substitute_real_domain',execution_graph=result['execution_graph'],reply=result['messages'][-1]['content'],business_status=result['proposal']['status'] if result.get('proposal') else None),ensure_ascii=False,indent=2),encoding='utf-8')

def ledger(result):return {g['id']:g for g in result['execution_graph']['goals']}

def test_greeting_has_goal_without_forced_plan(dynamic,monkeypatch):
    db,_=dynamic;sequence(monkeypatch,{'role':'assistant','content':'你好，可以先说说想了解什么。'})
    result=create(db,session(db),'live','你好')
    evidence('greeting',result)
    assert result['execution_graph']['nodes']==[]
    assert ledger(result)['g1']['status']=='completed' and not result['steps'] and transfers(db)==0


def test_single_balance_records_trusted_policy_and_no_fake_edges(dynamic,monkeypatch):
    db,_=dynamic;sequence(monkeypatch,calls(('list_accounts',{})),reply('已查询本人账户，余额见数据卡片。'))
    result=create(db,session(db),'live','查一下余额');g=result['execution_graph'];nodes=g['nodes'];evidence('balance',result)
    assert ledger(result)['g1']['status']=='completed'
    assert nodes[0]['policy']==dict(effect='read',risk_floor='green',risk_policy='owned_read',policy_version='g01-tool-policy-v1',can_submit=False)
    assert nodes[0]['depends_on']==[] and nodes[1]['depends_on']==[]
    assert nodes[1]['observed_refs']==nodes[0]['result_refs']
    assert nodes[0]['attempts']==1 and nodes[0]['task_id']==result['id']
    assert nodes[0]['goal_ids']==['g1'] and g['serial'] is True


def test_unknown_budget_is_waiting_input_not_complete(dynamic,monkeypatch):
    db,_=dynamic
    sequence(monkeypatch,calls(('financial_planning_basis',{})),reply('已核对本人现金与收支；要判断目标是否可行，还需要你明确预算。',needs_input=True))
    result=create(db,session(db),'live','分析旅行是否可行，预算还没想好')
    evidence('analysis_missing_budget',result)
    assert ledger(result)['g1']['status']=='waiting_input'
    assert ledger(result)['g1']['evidence_refs'] and transfers(db)==0


def test_multiple_goals_share_one_real_query_then_partial_complete(dynamic,monkeypatch):
    db,_=dynamic
    sequence(monkeypatch,goals(('cash','查余额','query'),('plan','分析旅行预算','analysis')),
        calls(linked('financial_planning_basis',{},'cash','plan')),
        calls(linked('respond',{'text':'已核对本人资金；旅行还缺目标预算，请补充。','needs_input':True},'plan')))
    result=create(db,session(db),'live','查余额，分析旅行预算');g=ledger(result);evidence('multiple_goals_partial',result)
    assert g['cash']['status']=='completed' and g['plan']['status']=='waiting_input'
    assert result['execution_graph']['status']=='partially_completed'
    shared=result['execution_graph']['nodes'][1]
    assert shared['goal_ids']==['cash','plan'] and shared['name']=='financial_planning_basis' and shared['depends_on']==[]
    assert len([n for n in result['execution_graph']['nodes'] if n['name']=='financial_planning_basis'])==1


def test_actual_operand_and_fact_dependencies_are_recorded(dynamic,monkeypatch):
    db,_=dynamic
    def calculation(messages,context):
        obs=json.loads(messages[-1]['content']);f=next(f for f in obs['facts'] if f['unit']=='yuan')
        return calls(('calculate',{'title':'取证金额','calculation':{'operation':'sum','inputs':[{'node':obs['call_id'],'key':f['key']}]}}))
    def answer(messages,context):
        obs=json.loads(messages[-1]['content']);f=obs['facts'][0]
        return reply('本次计算结果为{{'+f['id']+'}}。')
    sequence(monkeypatch,calls(('financial_planning_basis',{})),calculation,answer)
    result=create(db,session(db),'live','查询并计算本人可用资金');nodes=result['execution_graph']['nodes']
    assert nodes[1]['depends_on']==['s1'] and nodes[2]['depends_on']==['s2']
    assert nodes[1]['dependency_sources']=={'s1':'argument_reference'}


def test_independent_batch_has_no_chronological_dependencies(dynamic,monkeypatch):
    db,_=dynamic;sequence(monkeypatch,calls(('list_accounts',{}),('list_cards',{})),reply('已查询账户和卡片。'))
    result=create(db,session(db),'live','查看账户和卡片')
    assert all(n['depends_on']==[] for n in result['execution_graph']['nodes'])


@pytest.mark.parametrize('ask',[False,True])
def test_failed_tool_does_not_complete_query_goal(dynamic,monkeypatch,ask):
    db,_=dynamic;original=runtime.registry.execute
    def broken(*args,**kw):
        if kw['name']=='list_accounts':raise DomainError('TEST_READ_FAILURE','测试读取失败',503)
        return original(*args,**kw)
    monkeypatch.setattr(runtime.registry,'execute',broken)
    sequence(monkeypatch,calls(('list_accounts',{})),reply('本次查询失败，暂时无法给出余额，请稍后重新查询。',needs_input=ask))
    result=create(db,session(db),'live','查询余额')
    evidence('tool_failure',result)
    assert ledger(result)['g1']['status']=='failed' and not ledger(result)['g1']['evidence_refs']
    assert result['execution_graph']['nodes'][0]['error_code']=='TEST_READ_FAILURE' and transfers(db)==0


@pytest.mark.parametrize('binding',[{'goal_ids':['not_owned']},{'depends_on':['s2']},{'depends_on':['future']},{'risk_floor':'green'},{'goal_ids':['g1','g1']}])
def test_bad_binding_rejects_entire_batch_without_business(dynamic,monkeypatch,binding):
    db,_=dynamic;executed=[]
    monkeypatch.setattr(runtime.registry,'execute',lambda *a,**kw:executed.append(kw['name']))
    sequence(monkeypatch,calls(('list_cards',{}),('list_accounts',{'execution':binding})),reply('调用未通过校验，没有执行业务。'))
    result=create(db,session(db),'live','查看账户')
    assert executed==[] and transfers(db)==0
    assert all(n['status']=='failed' for n in result['execution_graph']['nodes'][:-1])


@pytest.mark.parametrize('tool',[('prepare_card_loss',{'card':'日常卡'}),('prepare_card_unloss',{}),('prepare_wealth_buy',{'amount_yuan':'1'})])
def test_fixed_red_metadata_cannot_be_downgraded(tool):
    from app.agent.registry import execution_policy
    assert execution_policy(*tool)['risk_floor']=='red' and not execution_policy(*tool)['can_submit']


def test_waiting_form_and_receipt_use_same_node_without_repeat_write(dynamic,monkeypatch):
    db,_=dynamic
    sequence(monkeypatch,calls(('prepare_transfer',{'source':'活期账户','target':'储蓄账户','amount_yuan':'100'})),reply('已收到业务回执。'))
    result=create(db,session(db),'live','从活期账户转100元到储蓄账户');g=ledger(result)['g1']
    evidence('waiting_authorization',result)
    assert g['status']=='waiting_authorization' and transfers(db)==0
    n=result['execution_graph']['nodes'][0];assert n['result_plan_version']==result['plan_version'] and n['attempts']==1
    a=result['authorization_operations'][0]
    token=authorization.issue_token(db,session(db),result['id'],a['resource_kind'],a['resource_id'],a['action_id'])['confirmation_token']
    paid=authorization.confirm(db,session(db),result['id'],a['resource_kind'],a['resource_id'],a['action_id'],token)
    # Before synthesis/callback, a server read already shows actual receipt.
    assert ledger(paid)['g1']['status']=='completed' and ledger(paid)['g1']['receipt_refs']
    done=runtime.after_authorization(db,session(db),result['id'],a['resource_kind'],a['resource_id'],a['action_id'],paid)
    evidence('same_node_after_receipt',done)
    assert ledger(done)['g1']['status']=='completed' and transfers(db)==1
    assert done['execution_graph']['nodes'][0]['id']==n['id']
    replay=authorization.confirm(db,session(db),result['id'],a['resource_kind'],a['resource_id'],a['action_id'],token)
    assert ledger(replay)['g1']['receipt_refs']==ledger(done)['g1']['receipt_refs'] and transfers(db)==1


def test_missing_fields_are_waiting_input(dynamic,monkeypatch):
    db,_=dynamic;sequence(monkeypatch,calls(('prepare_transfer',{'target':'小林','amount_yuan':'200'})))
    result=create(db,session(db),'live','给小林转200元')
    assert ledger(result)['g1']['status']=='waiting_input' and result['transfer_details'] and transfers(db)==0


def test_query_goal_remains_complete_while_transfer_waits(dynamic,monkeypatch):
    db,_=dynamic
    sequence(monkeypatch,goals(('cash','查余额','query'),('pay','转100元到储蓄账户','operation')),
        calls(linked('list_accounts',{},'cash')),
        calls(linked('prepare_transfer',{'source':'活期账户','target':'储蓄账户','amount_yuan':'100'},'pay')))
    result=create(db,session(db),'live','查余额，再从活期账户转100元到储蓄账户')
    g=ledger(result);assert g['cash']['status']=='completed' and g['pay']['status']=='waiting_authorization'
    assert result['execution_graph']['status']=='partially_completed' and transfers(db)==0


@pytest.mark.parametrize('mutation',['cycle','missing_node','foreign_owner','foreign_session','future_version','invalid_state','forged_risk','negative_attempts','unknown_goal','unknown_result','forged_receipt'])
def test_contract_rejects_invalid_graph(dynamic,monkeypatch,mutation):
    db,_=dynamic;sequence(monkeypatch,calls(('list_accounts',{})),reply('已查询。'))
    result=create(db,session(db),'live','查询余额');task=db.get(AgentTask,result['id']);r=deepcopy(state(db,result['id']));n=r['nodes'][0]
    if mutation=='cycle':n['depends_on']=['s2'];r['nodes'][1]['depends_on']=['s1']
    if mutation=='missing_node':n['depends_on']=['s99']
    if mutation=='foreign_owner':n['owner_id']='u2'
    if mutation=='foreign_session':r['session_id']='s2'
    if mutation=='future_version':n['plan_version']=task.plan_version+1
    if mutation=='invalid_state':n['status']='authorized_by_model'
    if mutation=='forged_risk':n['policy']['can_submit']=True
    if mutation=='negative_attempts':n['attempts']=-1
    if mutation=='unknown_goal':n['goal_ids']=['another_user_goal']
    if mutation=='unknown_result':n['result_refs']=['call:foreign:s1']
    if mutation=='forged_receipt':n['receipt_refs']=['proposal:invented']
    with pytest.raises(DomainError):graph.validate(task,r)


def test_completed_node_cannot_be_restarted_or_have_arguments_changed(dynamic,monkeypatch):
    db,_=dynamic;sequence(monkeypatch,calls(('list_accounts',{})),reply('已查询。'))
    result=create(db,session(db),'live','查询余额');task=db.get(AgentTask,result['id']);old=state(db,task.id)
    for field,value in [('status','running'),('arguments',{'owner_id':'u2'})]:
        new=deepcopy(old);new['nodes'][0][field]=value
        with pytest.raises(DomainError):graph.validate(task,new,old)


def test_old_model_response_does_not_override_new_goal(dynamic,monkeypatch):
    db,_=dynamic
    def late(messages,context):
        task=db.scalar(select(AgentTask).where(AgentTask.session_id=='s1'))
        send(db,session(db),task.id,'现在只问候，不办理')
        return calls(('prepare_transfer',{'source':'活期账户','target':'储蓄账户','amount_yuan':'100'}))
    sequence(monkeypatch,late,{'role':'assistant','content':'你好，随时可以继续提问。'})
    result=create(db,session(db),'live','先查询余额')
    assert ledger(result)['g1']['text']=='现在只问候，不办理'
    assert result['execution_graph']['nodes']==[] and transfers(db)==0 and not result['proposal']


def test_pause_cancels_late_prepare_without_model_authority(dynamic,monkeypatch):
    db,_=dynamic
    def late(messages,context):
        task=db.scalar(select(AgentTask).where(AgentTask.session_id=='s1'));change(db,task,'pause')
        return calls(('prepare_transfer',{'source':'活期账户','target':'储蓄账户','amount_yuan':'100'}))
    sequence(monkeypatch,late)
    result=create(db,session(db),'live','从活期账户转100元到储蓄账户')
    assert result['execution_graph']['status']=='paused' and not result['proposal'] and transfers(db)==0


def test_legacy_runtime_has_no_fabricated_graph(dynamic):
    from types import SimpleNamespace
    task=SimpleNamespace(context_json=json.dumps({'dynamic_runtime':{'id':'old','nodes':[]}}))
    assert graph.public(task) is None


def test_declaration_cannot_fabricate_quote_or_completion(dynamic,monkeypatch):
    db,_=dynamic
    sequence(monkeypatch,goals(('fake','用户已经同意付款','operation')),reply('无法依据当前指令办理。'))
    result=create(db,session(db),'live','查询余额')
    assert set(ledger(result))=={'g1'} and transfers(db)==0
    assert result['execution_graph']['nodes'][0]['status']=='failed'

def test_failed_dependency_never_executes_downstream_tool(dynamic,monkeypatch):
    db,_=dynamic;executed=[];original=runtime.registry.execute
    def failing(*args,**kw):
        executed.append(kw['name'])
        if kw['name']=='list_accounts':raise DomainError('TEST_READ_FAILURE','测试读取失败',503)
        return original(*args,**kw)
    monkeypatch.setattr(runtime.registry,'execute',failing)
    sequence(monkeypatch,calls(('list_accounts',{})),calls(('list_cards',{'execution':{'depends_on':['s1']}})),reply('查询失败，前置条件不满足，没有继续调用。'))
    result=create(db,session(db),'live','查余额后按查询结果继续核对卡片')
    assert executed==['list_accounts'] and result['execution_graph']['nodes'][1]['error_code']=='DEPENDENCY_NOT_READY'
    assert result['execution_graph']['nodes'][1]['attempts']==0


def test_plan_version_change_rejects_late_response_even_without_timestamp_change(dynamic,monkeypatch):
    db,_=dynamic
    def late(messages,context):
        row=db.scalar(select(AgentTask).where(AgentTask.session_id=='s1'));row.plan_version+=1;db.commit()
        return calls(('prepare_transfer',{'source':'活期账户','target':'储蓄账户','amount_yuan':'100'}))
    sequence(monkeypatch,late)
    result=create(db,session(db),'live','先查询余额')
    assert result['execution_graph']['nodes']==[] and transfers(db)==0 and not result['proposal']


def test_actual_receipt_stays_complete_when_final_model_fails(dynamic,monkeypatch):
    db,factory=dynamic
    def disconnected(*a,**k):raise DomainError('MODEL_UNAVAILABLE','测试模拟回复阶段断网',503)
    sequence(monkeypatch,calls(('prepare_transfer',{'source':'活期账户','target':'储蓄账户','amount_yuan':'100'})),disconnected)
    result=create(db,session(db),'live','从活期账户转100元到储蓄账户');a=result['authorization_operations'][0]
    token=authorization.issue_token(db,session(db),result['id'],a['resource_kind'],a['resource_id'],a['action_id'])['confirmation_token']
    paid=authorization.confirm(db,session(db),result['id'],a['resource_kind'],a['resource_id'],a['action_id'],token)
    failed=runtime.after_authorization(db,session(db),result['id'],a['resource_kind'],a['resource_id'],a['action_id'],paid)
    assert ledger(failed)['g1']['status']=='completed' and transfers(db)==1
    with factory() as fresh:
        recovered=view(fresh,fresh.get(AgentTask,result['id']))
        assert ledger(recovered)['g1']['receipt_refs']==ledger(failed)['g1']['receipt_refs'] and transfers(fresh)==1


def test_public_graph_minimizes_local_sensitive_fields(dynamic,monkeypatch):
    db,_=dynamic;sequence(monkeypatch,calls(('prepare_transfer',{'target':'小林','amount_yuan':'200','target_account':'1234567890123456789'})))
    result=create(db,session(db),'live','给小林转200元，账号1234567890123456789')
    public=json.dumps(result['execution_graph'],ensure_ascii=False)
    assert '1234567890123456789' not in public and result['execution_graph']['nodes'][0]['arguments']['target_account'] is None
    assert transfers(db)==0


def test_goals_can_be_refined_after_real_query_without_fake_dependencies(dynamic,monkeypatch):
    db,_=dynamic
    def answer(messages,context):
        f=context['facts'][0]
        return calls(linked('respond',{'text':'查询结果为{{'+f['id']+'}}；旅行还缺预算。','needs_input':True},'cash','plan'))
    sequence(monkeypatch,calls(('financial_planning_basis',{})),goals(('cash','查余额','query'),('plan','分析旅行预算','analysis')),answer)
    result=create(db,session(db),'live','查余额，分析旅行预算');g=ledger(result)
    assert g['cash']['status']=='completed' and g['plan']['status']=='waiting_input'
    nodes=result['execution_graph']['nodes']
    assert nodes[0]['goal_ids']==['cash','plan'] and nodes[0]['depends_on']==[] and nodes[2]['depends_on']==['s1']


def test_structured_questions_keep_analysis_goal_waiting_even_without_flag(dynamic,monkeypatch):
    from app.agent.dynamic_protocol import Respond
    db,_=dynamic;sequence(monkeypatch,calls(('financial_planning_basis',{})),reply('需要先明确目标预算。',needs_input=True))
    result=create(db,session(db),'live','分析旅行预算');r=state(db,result['id'])
    # Production structured responses infer the wait from validated questions,
    # never from keyword matching a generated sentence.
    response=Respond.model_validate({'synthesis':{'goal':'旅行预算','conclusion':'先明确预算。','reasons':[{'text':'已读取本人现金，目标成本未知。','fact_ids':[r['facts'][0]['id']]}],'scenarios':[],'actions':[],'questions':['旅行预算是多少？']}})
    r['goals'][0]['needs_input']=False
    graph.responded(r,response);graph.refresh(r)
    assert r['goals'][0]['status']=='waiting_input'


def test_goal_declaration_and_queries_in_one_batch_still_execute_serially(dynamic,monkeypatch):
    db,_=dynamic
    declaration={'goals':[dict(id='cash',text='查余额',quote='查余额',kind='query'),dict(id='plan',text='分析旅行预算',quote='分析旅行预算',kind='analysis')]}
    sequence(monkeypatch,calls(('declare_goals',declaration),linked('list_accounts',{},'cash'),linked('financial_planning_basis',{},'plan')),
        calls(linked('respond',{'text':'余额已查到，旅行还需要明确预算。','needs_input':True},'plan')))
    result=create(db,session(db),'live','查余额，分析旅行预算');g=ledger(result)
    assert g['cash']['status']=='completed' and g['plan']['status']=='waiting_input'
    assert [n['name'] for n in result['execution_graph']['nodes']]==['declare_goals','list_accounts','financial_planning_basis','respond']
    assert not result['execution_graph']['nodes'][1]['depends_on'] and transfers(db)==0


def test_rejected_declaration_batch_executes_no_business_tool(dynamic,monkeypatch):
    db,_=dynamic;executed=[]
    monkeypatch.setattr(runtime.registry,'execute',lambda *a,**kw:executed.append(kw['name']))
    bad={'goals':[dict(id='cash',text='杜撰目标',quote='用户并没有说这个',kind='query')]}
    sequence(monkeypatch,calls(('declare_goals',bad),linked('list_accounts',{},'cash')),reply('未通过目标原文校验，未执行业务。'))
    result=create(db,session(db),'live','查余额')
    assert executed==[] and set(ledger(result))=={'g1'} and transfers(db)==0


def test_rejected_unknown_cost_calculation_can_be_repaired_without_completing_budget_goal(dynamic,monkeypatch):
    db,_=dynamic
    def calc(operation):
        inputs=[{'node':'s1','key':'available_liquid'},{'node':'s1','key':'monthly_expense_mean'},{'value':'3','unit':'number','label':'假设预留月数'}]
        if operation=='goal_budget':inputs.append({'value':'0','unit':'yuan','label':'未知旅行费用'})
        return calls(('calculate',dict(title='假设预留后的现金余量',calculation=dict(operation=operation,inputs=inputs))))
    sequence(monkeypatch,calls(('financial_planning_basis',{})),calc('goal_budget'),calc('emergency_check'),reply('已核对生活费缓冲；旅行还需要明确预算。',needs_input=True))
    result=create(db,session(db),'live','分析旅行预算，还没定旅行花费');nodes=result['execution_graph']['nodes']
    assert nodes[1]['status']=='failed' and nodes[1]['recovered_by']=='s3'
    assert ledger(result)['g1']['status']=='waiting_input' and transfers(db)==0


@pytest.mark.parametrize('keep_goal',[False,True])
def test_calculation_repair_does_not_hide_other_goals_or_drop_user_constants(dynamic,monkeypatch,keep_goal):
    db,_=dynamic
    sequence(monkeypatch,calls(('financial_planning_basis',{})),reply('先明确必要预算条件。',needs_input=True))
    result=create(db,session(db),'live','分析预算');r=state(db,result['id'])
    old=dict(id='old',name='calculate',arguments={'calculation':{'inputs':[{'node':'s1','key':'available_liquid'},{'value':'12000','unit':'yuan','quote':'预算12000'}]}},status='failed',error_code='INVALID_CALCULATION',goal_ids=['g1'])
    new=dict(id='new',name='calculate',arguments={'calculation':{'inputs':[{'node':'s1','key':'available_liquid'}]}},status='succeeded',goal_ids=['g1'] if keep_goal else ['another'])
    r['nodes']+=[old,new];graph.observed(r,new,dict(source_ref='call:'+r['id']+':new',data={}))
    assert not old.get('recovered_by')
