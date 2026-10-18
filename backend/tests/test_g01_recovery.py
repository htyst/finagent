"""Persistent checkpoints, selective replanning and real-domain receipt recovery."""
import json
from copy import deepcopy
import pytest
from sqlalchemy import select
from app.agent import checkpoints,dynamic_runtime as rt,execution_graph as graph,authorization
from app.agent.service import create,send,view
from app.data.models import AgentTask,Account,DemoSession
from app.domain.errors import DomainError
from test_dynamic_tools import dynamic,sequence,calls,reply,session,state,transfers
from test_g01_execution_graph import goals,linked,ledger


def revision(ids,action='update',quote='改成八千',constraints=None):
    return calls(('revise_goals',dict(goal_ids=ids,action=action,instruction_quote=quote,constraints=constraints or [])))

def initial(db,monkeypatch):
    sequence(monkeypatch,calls(('financial_planning_basis',{})),reply('已经查到本人底稿，还需要目标预算。',needs_input=True))
    return create(db,session(db),'live','分析旅行预算，两万元，只咨询不办理')

def test_budget_change_invalidates_derived_not_query(dynamic,monkeypatch):
    db,_=dynamic;first=initial(db,monkeypatch);task=db.get(AgentTask,first['id']);r=state(db,task.id)
    read=next(n for n in r['nodes'] if n['name']=='financial_planning_basis')
    sequence(monkeypatch,revision(['g1'],constraints=[dict(key='budget',value='八千',quote='改成八千')]),reply('新预算已保留，请按新的条件继续分析。',needs_input=True))
    result=send(db,session(db),task.id,'两万改成八千');r=state(db,task.id)
    assert result['plan_version']>first['plan_version']
    assert r['goals'][0]['constraints'][0]['value']=='八千'
    assert not next(n for n in r['nodes'] if n['id']==read['id']).get('invalidated')
    assert all(n.get('invalidated') for n in r['nodes'] if n['name']=='respond' and n['plan_version']<result['plan_version'])
    assert r['checkpoint']['plan_version']==result['plan_version'] and transfers(db)==0


def test_insertion_preserves_wait_and_does_not_reuse_amount(dynamic,monkeypatch):
    db,_=dynamic
    sequence(monkeypatch,calls(('prepare_transfer',dict(source='活期账户',target='储蓄账户',amount_yuan='100'))))
    first=create(db,session(db),'live','从活期账户转100元到储蓄账户');wait=state(db,first['id'])['wait']
    sequence(monkeypatch,goals(('balance','先查余额','query')),calls(linked('list_accounts',{},'balance')),calls(linked('respond',{'text':'已查询余额；原转账仍待本人页面授权。'},'balance')))
    result=send(db,session(db),first['id'],'先查余额')
    assert state(db,first['id'])['wait']==wait
    assert ledger(result)['g1']['status']=='waiting_authorization'
    assert ledger(result)['balance']['status']=='completed' and transfers(db)==0
    assert len([n for n in state(db,first['id'])['nodes'] if n['name']=='prepare_transfer'])==1


def test_cancel_one_goal_preserves_shared_read_and_other_goal(dynamic,monkeypatch):
    db,_=dynamic
    sequence(monkeypatch,goals(('cash','查余额','query'),('travel','分析旅行','analysis')),calls(linked('financial_planning_basis',{},'cash','travel')),calls(linked('respond',dict(text='旅行还缺预算。',needs_input=True),'travel')))
    first=create(db,session(db),'live','查余额，分析旅行')
    sequence(monkeypatch,revision(['travel'],'cancel','取消旅行'),calls(linked('respond',dict(text='旅行的未执行目标已取消；余额查询结果仍保留。'),'travel')))
    result=send(db,session(db),first['id'],'取消旅行')
    assert ledger(result)['travel']['status']=='cancelled' and ledger(result)['cash']['status']=='completed'
    assert not next(n for n in state(db,first['id'])['nodes'] if n['name']=='financial_planning_basis').get('invalidated')


def test_reconcile_paid_before_callback_then_restart_does_not_pay_again(dynamic,monkeypatch):
    db,factory=dynamic
    sequence(monkeypatch,calls(('prepare_transfer',dict(source='活期账户',target='储蓄账户',amount_yuan='100'))))
    first=create(db,session(db),'live','从活期账户转100元到储蓄账户');op=first['authorization_operations'][0]
    token=authorization.issue_token(db,session(db),first['id'],op['resource_kind'],op['resource_id'],op['action_id'])['confirmation_token']
    authorization.confirm(db,session(db),first['id'],op['resource_kind'],op['resource_id'],op['action_id'],token)
    assert transfers(db)==1
    # No callback occurred. A different DB session simulates server restart.
    with factory() as fresh:
        task=fresh.get(AgentTask,first['id']);r=rt.get(task);checkpoints.reconcile(fresh,session(fresh),task,r);rt.save(task,r);fresh.commit()
        assert r['wait'] is None and r['nodes'][0]['receipt_refs']
        sequence(monkeypatch,goals(('balance','再查余额','query')),calls(linked('list_accounts',{},'balance')),calls(linked('respond',dict(text='已查询；之前付款回执保留。'),'balance')))
        done=send(fresh,session(fresh),task.id,'再查余额')
        assert transfers(fresh)==1 and ledger(done)['g1']['status']=='completed'


def test_unknown_preparation_after_restart_blocks_blind_replay(dynamic,monkeypatch):
    db,_=dynamic;first=initial(db,monkeypatch);task=db.get(AgentTask,first['id']);r=state(db,task.id)
    node=dict(id='s99',name='prepare_transfer',title='准备操作',arguments={'amount_yuan':'100'},status='pending',model_call_id='lost')
    graph.bind(task,r,node,{'goal_ids':['g1']});r['nodes'].append(node);graph.start(r,node);rt.save(task,r);db.commit()
    r=state(db,task.id);checkpoints.reconcile(db,session(db),task,r);rt.save(task,r);db.commit()
    result=view(db,task);assert ledger(result)['g1']['status']=='waiting_verification' and transfers(db)==0
    assert r['nodes'][-1]['error_code']=='RESULT_UNKNOWN'


def test_read_recovery_never_replays_running_node(dynamic,monkeypatch):
    db,_=dynamic;first=initial(db,monkeypatch);task=db.get(AgentTask,first['id']);r=state(db,task.id)
    node=dict(id='s99',name='list_cards',title='查询',arguments={},status='pending',model_call_id='lost')
    graph.bind(task,r,node,{});r['nodes'].append(node);graph.start(r,node);rt.save(task,r);db.commit()
    checkpoints.reconcile(db,session(db),task,r)
    assert node['status']=='failed' and node['error_code']=='INTERRUPTED_READ' and not node['verification_required']


def test_owned_resource_change_stales_evidence_but_other_owner_does_not(dynamic,monkeypatch):
    db,_=dynamic;first=initial(db,monkeypatch);task=db.get(AgentTask,first['id']);r=state(db,task.id);v=checkpoints.version(db,task)
    other=db.scalar(select(Account).where(Account.owner_id=='u2'));other.version+=1;db.commit();assert checkpoints.version(db,task)==v
    own=db.scalar(select(Account).where(Account.owner_id=='u1'));own.version+=1;db.commit();checkpoints.reconcile(db,session(db),task,r)
    assert r['stale_evidence'] and all(n.get('invalidated') for n in r['nodes'] if n['name']=='financial_planning_basis')


@pytest.mark.parametrize('change',[dict(goal_ids=['foreign'],action='cancel',instruction_quote='取消'),dict(goal_ids=['g1'],action='update',instruction_quote='不存在'),dict(goal_ids=['g1'],action='update',instruction_quote='取消',constraints=[dict(key='budget',value='999',quote='取消')])])
def test_goal_change_requires_owned_goal_and_current_quote(dynamic,monkeypatch,change):
    db,_=dynamic;first=initial(db,monkeypatch);task=db.get(AgentTask,first['id']);r=state(db,task.id);r['request']='取消'
    with pytest.raises(DomainError):checkpoints.revise(db,session(db),task,r,change)
    assert transfers(db)==0


def test_model_failure_saves_continuation_and_real_receipt(dynamic,monkeypatch):
    db,_=dynamic
    sequence(monkeypatch,calls(('prepare_transfer',dict(source='活期账户',target='储蓄账户',amount_yuan='100'))))
    result=create(db,session(db),'live','从活期账户转100元到储蓄账户');op=result['authorization_operations'][0]
    token=authorization.issue_token(db,session(db),result['id'],op['resource_kind'],op['resource_id'],op['action_id'])['confirmation_token']
    paid=authorization.confirm(db,session(db),result['id'],op['resource_kind'],op['resource_id'],op['action_id'],token)
    def unavailable(*a):raise DomainError('MODEL_UNAVAILABLE','测试网络故障',503)
    sequence(monkeypatch,unavailable)
    result=rt.after_authorization(db,session(db),result['id'],op['resource_kind'],op['resource_id'],op['action_id'],paid)
    assert result['proposal']['receipt'] and transfers(db)==1
    assert state(db,result['id'])['failure_classification']=='MODEL_UNAVAILABLE'
    assert ledger(result)['g1']['status']=='completed'


def test_long_dialogue_24_turns_keeps_goals_and_no_payment(dynamic,monkeypatch):
    db,_=dynamic;result=initial(db,monkeypatch)
    for i in range(24):
        if i==18:
            from app.agent.control import change
            task=db.get(AgentTask,result['id']);change(db,task,'pause');change(db,task,'resume')
        text=f'预算改为{8000+i}元'
        sequence(monkeypatch,revision(['g1'],'update',text,[dict(key='budget',value=str(8000+i),quote=text)]),reply('已更新预算；原查询证据仍保留，可继续补充条件。',needs_input=True))
        if i==6:
            text='先分析另一个目标，预算还不知道'
            sequence(monkeypatch,goals(('other_goal',text,'analysis')),calls(linked('respond',dict(text='新增目标还需要预算；原旅行待办保留。',needs_input=True),'other_goal')))
        elif i==7:
            text='取消另一个目标'
            sequence(monkeypatch,revision(['other_goal'],'cancel',text),calls(linked('respond',dict(text='已取消该目标的未执行部分，原旅行目标保留。'),'other_goal')))
        elif i==12:
            text='不要转账，只继续咨询原旅行目标'
            sequence(monkeypatch,revision(['g1'],'continue',text),calls(linked('respond',dict(text='继续分析，尚需明确时间，没有准备转账。',needs_input=True),'g1')))
        result=send(db,session(db),result['id'],text)
    r=state(db,result['id']);assert len(r['nodes'])==50 and r['turn_revision']==24
    assert r['goals'][0]['constraints'][0]['value']=='8023' and transfers(db)==0
    assert sum(n['name']=='financial_planning_basis' for n in r['nodes'])==1


def test_contract_generation_matches_real_schema_and_compatibility():
    from app.agent import dynamic_protocol as p,tool_contracts,registry
    c=tool_contracts.generate();entries={v['name']:v for v in c['tools']}
    visible={v['function']['name'] for v in p.catalog()}
    assert set(entries)==set(p.models()) and visible=={n for n,e in entries.items() if e['model_visible']}
    assert c['model_tool_count']<c['tool_count']==64
    for n in entries:
        assert entries[n]['input_schema']==p.models()[n].model_json_schema()
        assert entries[n]['risk_policy']==registry.execution_policy(n)
    assert entries['prepare_card_loss']['fixed_arguments']=={'action':'loss'}
    assert entries['prepare_card_loss']['risk_policy']['risk_floor']=='red'


def test_audit_links_actual_goals_nodes_and_receipts_without_secrets(dynamic,monkeypatch):
    from app.agent.audit import task_audit
    db,_=dynamic;sequence(monkeypatch,calls(('list_accounts',{})),reply('已查询本人账户。'))
    result=create(db,session(db),'live','查询余额');audit=task_audit(db,session(db),result['id'])
    events=audit['correlation_events'];assert events and all(e['task_id']==result['id'] and e['request_id'] for e in events)
    assert events[0]['node_id']=='s1' and events[0]['goal_ids']==['g1'] and events[0]['evidence_refs']
    with pytest.raises(DomainError):task_audit(db,db.get(DemoSession,'other'),result['id'])
    assert 'tool_calls' not in json.dumps(audit) and 'confirmation_token' not in json.dumps(audit)


def test_complete_replan_pay_reply_failure_restart_continue_trajectory(dynamic,monkeypatch):
    db,factory=dynamic
    sequence(monkeypatch,calls(('financial_planning_basis',{})),calls(('calculate',dict(title='旅行预算',calculation=dict(operation='sum',inputs=[dict(value='20000',unit='yuan',label='旅行预算',quote='预算两万元')])))),reply('已核对底稿与预算；还需要明确出行时间。',needs_input=True))
    result=create(db,session(db),'live','分析旅行，预算两万元，只分析不办理');tid=result['id']
    sequence(monkeypatch,revision(['g1'],'update','改成八千元',[dict(key='budget',value='八千',quote='改成八千元')]),calls(('calculate',dict(title='新旅行预算',calculation=dict(operation='sum',inputs=[dict(value='8000',unit='yuan',label='旅行预算',quote='改成八千元')])))),reply('已按新预算核对，还需明确时间。',needs_input=True))
    result=send(db,session(db),tid,'预算改成八千元');r=state(db,tid)
    assert next(n for n in r['nodes'] if n['name']=='calculate')['invalidated']
    assert [f['value'] for f in rt.current_evidence(r)['facts'] if f['tool']=='calculate' and f['key']=='result']==['8000.00']
    sequence(monkeypatch,goals(('pay','从活期账户转100元到储蓄账户','operation'),('cash_after','转后查余额','query')),calls(linked('prepare_transfer',dict(source='活期账户',target='储蓄账户',amount_yuan='100'),'pay')))
    result=send(db,session(db),tid,'现在从活期账户转100元到储蓄账户，转后查余额');op=next(x for x in result['authorization_operations'] if x['can_confirm'])
    token=authorization.issue_token(db,session(db),tid,op['resource_kind'],op['resource_id'],op['action_id'])['confirmation_token']
    paid=authorization.confirm(db,session(db),tid,op['resource_kind'],op['resource_id'],op['action_id'],token)
    def fail(*a):raise DomainError('MODEL_UNAVAILABLE','验收回复阶段断网',503)
    sequence(monkeypatch,fail);rt.after_authorization(db,session(db),tid,op['resource_kind'],op['resource_id'],op['action_id'],paid)
    with factory() as fresh:
        sequence(monkeypatch,revision(['cash_after'],'continue','继续转后查余额'),calls(linked('list_accounts',{},'cash_after')),calls(linked('respond',dict(text='已查询余额；转账回执保留，旅行目标仍需出行时间。'),'cash_after')))
        result=send(fresh,session(fresh),tid,'继续转后查余额')
        assert transfers(fresh)==1 and ledger(result)['pay']['status']=='completed' and ledger(result)['cash_after']['status']=='completed'
        assert ledger(result)['g1']['status']!='completed'
        from pathlib import Path
        from app.agent.audit import task_audit
        out=Path('var/reviews/remaining-20261018');out.mkdir(parents=True,exist_ok=True)
        (out/'complete-recovery-trace.json').write_text(json.dumps(task_audit(fresh,session(fresh),tid),ensure_ascii=False,indent=2),encoding='utf-8')


def test_restored_observation_has_matching_tool_call_after_new_turn():
    r=dict(messages=[dict(role='user',content='插入查询后继续')]);node=dict(id='s1',model_call_id='original',name='list_accounts',arguments={})
    rt.append_result(r,node,dict(status='completed'))
    assert r['messages'][-1]['role']=='user' and json.loads(r['messages'][-1]['content'])['kind']=='server_checkpoint_data_not_user_instruction'
    assert not any(m.get('tool_calls') or m.get('tool_call_id') for m in r['messages'])


def test_invalidated_dependency_blocks_even_previously_successful_result(dynamic,monkeypatch):
    db,_=dynamic;result=initial(db,monkeypatch);task=db.get(AgentTask,result['id']);r=state(db,task.id);r['nodes'][0]['invalidated']=True
    node=dict(id='s99',model_call_id='c',name='list_cards',title='读取',arguments={},status='pending')
    graph.bind(task,r,node,{'depends_on':['s1']});r['nodes'].append(node)
    with pytest.raises(DomainError,match='前置'):graph.start(r,node)


@pytest.mark.parametrize('value',['8000','8000元','八千'])
def test_normalized_constraint_uses_existing_chinese_amount_grounding(dynamic,monkeypatch,value):
    db,_=dynamic;first=initial(db,monkeypatch);task=db.get(AgentTask,first['id']);r=state(db,task.id);r['request']='两万改成八千'
    checkpoints.revise(db,session(db),task,r,dict(goal_ids=['g1'],action='update',instruction_quote='改成八千',constraints=[dict(key='budget',value=value,quote='改成八千')]))
    assert r['goals'][0]['constraints'][0]['value']==value


def test_budget_revision_reuses_independent_emergency_reserve(dynamic,monkeypatch):
    db,_=dynamic
    sequence(monkeypatch,calls(('financial_planning_basis',{})),calls(('calculate',dict(title='应急金',calculation=dict(operation='reserve',inputs=[dict(node='s1',key='monthly_expense_mean'),dict(value='3',unit='number',label='预留月数')])))),reply('已查询并测算缓冲，预算仍需核对。',needs_input=True))
    result=create(db,session(db),'live','分析旅行预算，需要保留应急金');task=db.get(AgentTask,result['id']);r=state(db,task.id);r['request']='预算改成八千元'
    checkpoints.revise(db,session(db),task,r,dict(goal_ids=['g1'],action='update',instruction_quote=r['request'],constraints=[dict(key='budget',value='8000',quote=r['request'])]))
    assert not next(n for n in r['nodes'] if n['name']=='calculate').get('invalidated')
    assert any(f['metric']=='reserve' for f in rt.current_evidence(r)['facts'] if f['tool']=='calculate' and f['key']=='result')


def test_failed_revision_cannot_be_bypassed_with_new_calculation(dynamic,monkeypatch):
    db,_=dynamic;first=initial(db,monkeypatch)
    sequence(monkeypatch,revision(['g1'],'update','改八千',[dict(key='budget',value='9000',quote='改八千')]),calls(('calculate',dict(title='不能绕过更正',calculation=dict(operation='sum',inputs=[dict(value='8000',unit='yuan',label='预算',quote='改八千')])))),revision(['g1'],'update','改八千',[dict(key='budget',value='8000',quote='改八千')]),reply('已经按新约束保存，尚需明确期限。',needs_input=True))
    result=send(db,session(db),first['id'],'改八千');r=state(db,result['id'])
    assert any(n.get('error_code')=='GOAL_REVISION_REQUIRED' for n in r['nodes'])
    assert not r['revision_pending'] and r['goals'][0]['constraints'][0]['value']=='8000' and transfers(db)==0


@pytest.mark.parametrize('replacement,expected',[('3',True),('6',False)])
def test_repair_only_identical_untrusted_hypothesis_not_user_constant(dynamic,monkeypatch,replacement,expected):
    db,_=dynamic;first=initial(db,monkeypatch);r=state(db,first['id'])
    def item(nid,value,quote):return dict(id=nid,name='calculate',arguments={'calculation':dict(operation='emergency_check',inputs=[dict(node='s1',key='available_liquid'),dict(value=value,unit='number',label='预留月数（假设）',quote=quote)])},status='succeeded' if nid=='new' else 'failed',error_code=None if nid=='new' else 'ANALYSIS_STALE_ASSUMPTION',goal_ids=['g1'])
    old=item('old','3','模型假设预留3个月');new=item('new',replacement,None);r['nodes']+=[old,new];graph.observed(r,new,dict(source_ref='call:'+r['id']+':new',data={}))
    assert bool(old.get('recovered_by')) is expected


def test_text_cannot_claim_cancel_without_actual_goal_transition(dynamic,monkeypatch):
    db,_=dynamic;first=initial(db,monkeypatch)
    sequence(monkeypatch,reply('旅行目标已取消。'),revision(['g1'],'cancel','取消旅行目标'),reply('旅行目标已取消，已查询记录仍保留。'))
    result=send(db,session(db),first['id'],'取消旅行目标');r=state(db,first['id'])
    assert ledger(result)['g1']['status']=='cancelled' and r['goals'][0]['user_cancelled']
    assert any(n.get('error_code')=='GOAL_CONTROL_REQUIRED' for n in r['nodes'])
    assert transfers(db)==0


def test_cancel_claim_without_query_evidence_still_requires_actual_cancel(dynamic,monkeypatch):
    db,_=dynamic
    sequence(monkeypatch,calls(('prepare_transfer',dict(source='活期账户',target='储蓄账户',amount_yuan='100'))))
    first=create(db,session(db),'live','从活期账户转100元到储蓄账户')
    sequence(monkeypatch,reply('转账目标已取消。'),revision(['g1'],'cancel','取消转账目标'),reply('转账目标已取消，未付款。'))
    result=send(db,session(db),first['id'],'取消转账目标')
    assert result['proposal']['status']=='cancelled' and ledger(result)['g1']['status']=='cancelled' and transfers(db)==0
    assert result['messages'][-1]['content']=='已取消该目标尚未执行的部分。已发生的交易不受此次取消影响，请以固定单和实际回执为准。'


def test_cancel_receipt_cannot_be_borrowed_from_another_goal():
    r=dict(graph_schema=graph.SCHEMA_VERSION,goals=[dict(id='g1',user_cancelled=True),dict(id='g2')],nodes=[dict(name='respond',status='pending',goal_ids=['g2'])])
    with pytest.raises(DomainError,match='本目标'):rt.validate_goal_claim('本目标已取消。',r)


def test_cancel_fallback_uses_only_same_goal_control_state():
    r=dict(graph_schema=graph.SCHEMA_VERSION,goals=[dict(id='g1',user_cancelled=True),dict(id='g2')],turn_goal_ids=['g1'],nodes=[])
    text=rt.cancelled_goal_reply('已取消，余额999元，付款成功。',r)
    assert text and '999' not in text and '付款成功' not in text
    r['turn_goal_ids']=['g2']
    assert rt.cancelled_goal_reply('已取消。',r) is None


def test_completed_transfer_and_distinct_card_form_keep_both_bindings(dynamic,monkeypatch):
    db,factory=dynamic
    from app.agent.service import view
    sequence(monkeypatch,goals(('pay','从活期账户转100元到储蓄账户','operation'),('card','申请虚拟卡','operation')),calls(linked('prepare_transfer',dict(source='活期账户',target='储蓄账户',amount_yuan='100',operation_context={'instruction_quote':'从活期账户转100元到储蓄账户'}),'pay')))
    result=create(db,session(db),'live','从活期账户转100元到储蓄账户，再申请虚拟卡，申请人为本人，关联活期账户')
    op=result['authorization_operations'][0];tid=result['id'];original_proposal=result['proposal']['id']
    token=authorization.issue_token(db,session(db),tid,op['resource_kind'],op['resource_id'],op['action_id'])['confirmation_token']
    paid=authorization.confirm(db,session(db),tid,op['resource_kind'],op['resource_id'],op['action_id'],token)
    sequence(monkeypatch,calls(linked('prepare_virtual_card_application',dict(source='活期账户',applicant='本人',operation_context={'instruction_quote':'申请虚拟卡，申请人为本人，关联活期账户'}),'card')))
    result=rt.after_authorization(db,session(db),tid,op['resource_kind'],op['resource_id'],op['action_id'],paid)
    operations={o['resource_kind']:o for o in result['authorization_operations']}
    assert set(operations)=={'proposal','card'}
    assert result['proposal']['id']==original_proposal and operations['proposal']['receipt'] and not operations['proposal']['can_confirm']
    assert operations['card']['status']=='pending_confirmation' and operations['card']['can_confirm']
    assert ledger(result)['pay']['status']=='completed' and ledger(result)['card']['status']=='waiting_authorization'
    with factory() as fresh:
        result=view(fresh,fresh.get(AgentTask,tid))
        assert len(result['authorization_operations'])==2 and result['proposal']['receipt'] and transfers(fresh)==1
