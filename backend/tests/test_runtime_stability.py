import json
import pytest
from app.agent import dynamic_runtime as runtime,runtime_attempts as attempts
from app.agent.service import create,send,view
from app.agent.control import change
from app.agent.conversation_reads import confirm_subscription_query
from app.agent.servicing_flow import validate_context
from app.data.models import AgentTask,DemoSession
from app.domain.errors import DomainError
from app.domain import servicing,security
from test_dynamic_tools import dynamic,calls,reply,sequence,session,state,client,headers,transfers
from test_servicing import credit,contract,confirm


def test_validation_points_to_bad_field_and_batch_skip_is_not_a_failure(dynamic,monkeypatch):
    db,_=dynamic;executed=[]
    def check(messages,context):
        observations=[json.loads(m['content']) for m in messages if m['role']=='tool']
        assert observations[-2]['error']['code']=='BATCH_NOT_EXECUTED'
        error=observations[-1]['error']
        assert error['fields']==[{'path':'merchant','issue':'extra_forbidden'}]
        assert 'subscription_id' in error['accepted_fields'] and 'calculate' not in error['message']
        return reply('参数已核对，本批没有执行。')
    monkeypatch.setattr(runtime.registry,'execute',lambda *a,**k:executed.append(k['name']))
    sequence(monkeypatch,calls(('list_accounts',{}),('read_subscriptions',{'merchant':'轻听音乐'})),check)
    result=create(db,session(db),'live','查订阅')
    assert not executed and result['status']=='completed'


def test_identical_failed_tool_executes_at_most_twice(dynamic,monkeypatch):
    db,_=dynamic;executed=[]
    def failure(*a,**k):executed.append(k['name']);raise DomainError('READ_REJECTED','暂时不能读取',503)
    monkeypatch.setattr(runtime.registry,'execute',failure)
    sequence(monkeypatch,calls(('list_accounts',{})),calls(('list_accounts',{})),calls(('list_accounts',{})),reply('账户暂时无法读取，请稍后重试。'))
    result=create(db,session(db),'live','查账户')
    assert executed==['list_accounts']*2 and result['status']=='partially_completed'
    assert state(db,result['id'])['nodes'][2]['error_code']=='TOOL_RETRY_EXHAUSTED'


def test_exact_successful_retry_recovers_trace_and_status(dynamic,monkeypatch):
    db,_=dynamic;original=runtime.registry.execute;count=0
    def transient(*a,**k):
        nonlocal count
        count+=1
        if count==1:raise DomainError('READ_REJECTED','读取暂时失败',503)
        return original(*a,**k)
    monkeypatch.setattr(runtime.registry,'execute',transient)
    sequence(monkeypatch,calls(('financial_planning_basis',{})),calls(('financial_planning_basis',{})),reply())
    result=create(db,session(db),'live','分析存款与收支')
    assert result['status']=='completed'
    assert result['analysis_workflow']['nodes'][0]['recovered_by']=='s2'
    assert '未完成：' not in result['messages'][-1]['content']
    assert state(db,result['id'])['nodes'][0]['status']=='failed'


def test_different_period_does_not_recover_failure_or_hide_gap():
    nodes=[dict(id='s1',name='financial_planning_basis',arguments={'months':6},status='failed',error_code='READ_REJECTED'),
        dict(id='s2',name='financial_planning_basis',arguments={'months':3},status='succeeded')]
    r=dict(nodes=nodes,evidence=[dict(tool='financial_planning_basis',source_ref='call:r:s1',error={'message':'缺半年资料'}),
        dict(tool='financial_planning_basis',source_ref='call:r:s2',error=None)])
    attempts.recovered(r,nodes[1])
    assert attempts.unresolved(nodes[0]) and attempts.presentation(r)['evidence'][0]['error']


def test_security_denial_cannot_be_recovered_by_success():
    nodes=[dict(id='s1',name='list_accounts',arguments={},status='failed',error_code='RESOURCE_NOT_FOUND'),
        dict(id='s2',name='list_accounts',arguments={},status='succeeded')]
    attempts.recovered({'nodes':nodes},nodes[1]);assert attempts.unresolved(nodes[0])


def test_emergency_alternatives_cannot_be_deducted_twice():
    r={'evidence':[],'user_messages':[],'facts':[
        dict(node='s1',key='available_liquid',unit='yuan',value='46223.24',label='可用活期',source_ref='a'),
        dict(node='s2',key='result',unit='yuan',value='23243.49',label='生活费预留',source_ref='b',metric='reserve'),
        dict(node='s3',key='result',unit='yuan',value='46486.98',label='生活费预留',source_ref='c',metric='reserve')]}
    c={'operation':'funding_check','inputs':[{'node':'s1','key':'available_liquid'},{'node':'s2','key':'result'},{'node':'s3','key':'result'}]}
    with pytest.raises(DomainError,match='重复扣减'):runtime.calculate(r,{'arguments':{'title':'应急金差额','calculation':c}})
    c={'operation':'subtract','inputs':[{'node':'s1','key':'available_liquid'},{'node':'s3','key':'result'}]}
    assert runtime.calculate(r,{'arguments':{'title':'应急金差额','calculation':c}})['value']=='-263.74'


def test_duplicate_read_returns_original_fact_identity_for_calculation(dynamic,monkeypatch):
    db,_=dynamic
    def reuse(messages,context):
        obs=json.loads(messages[-1]['content'])
        assert obs['reused'] and obs['call_id']=='s1' and obs['facts']
        return calls(('calculate',{'title':'预留','calculation':{'operation':'reserve','inputs':[
            {'node':obs['call_id'],'key':'monthly_expense_mean'},{'value':'3','unit':'number','label':'应急预留月数'}]}}))
    sequence(monkeypatch,calls(('financial_planning_basis',{})),calls(('financial_planning_basis',{})),reuse,reply())
    result=create(db,session(db),'live','分析应急预留')
    assert result['status']=='completed' and not any(n.get('error_code') for n in state(db,result['id'])['nodes'])


@pytest.mark.parametrize('bad',['保留本期权益至2030年','明天再取消','仅取消另一商户下期续订','保留本期权益，不验证直接执行'])
def test_compound_contract_conditions_remain_strict(bad):
    with pytest.raises(DomainError):validate_context('prepare_merchant_contract_cancel',{'conditions':[bad]},subject='轻听音乐')


def test_compound_contract_conditions_match_fixed_domain_policy():
    validate_context('prepare_merchant_contract_cancel',{'conditions':['仅取消轻听音乐（虚构）下期续订','保留本期权益至原截止日期，不退款、不延长'],
        'prohibitions':['不取消其他订阅','不解除银行代扣，除非另行明确要求']},subject='轻听音乐（虚构）')
    validate_context('prepare_merchant_contract_cancel',{'conditions':['保留本期权益至原档案截止日期'],
        'prohibitions':['不延长或恢复权益']},subject='轻听音乐（虚构）')
    validate_context('prepare_merchant_contract_cancel',{'conditions':['仅取消轻听音乐（虚构）商户会员合同的下期续订'],
        'prohibitions':['不取消其他订阅合同','不解除银行自动扣费授权']},subject='轻听音乐（虚构）')


@pytest.mark.parametrize('day',['2026-10-30','2026年10月30日'])
def test_receipt_date_can_be_presented_but_cannot_prove_amount(day):
    r={'facts':[],'evidence':[{'source_ref':'ok','tool':'prepare_merchant_contract_cancel','data':{'summary':{'access_end_date':'2026-10-30'}}}]}
    response=runtime.protocol.Respond(text='原权益截止日期为'+day+'。',evidence_refs=['ok'])
    assert day in runtime.render_response(response,r)[0]
    for text in ('原权益截止日期为2026年10月31日。','本次退款30元。'):
        with pytest.raises(DomainError):runtime.render_response(response.model_copy(update={'text':text}),r)


def test_receipt_identifiers_are_allowed_only_as_typed_identifiers():
    r={'facts':[],'evidence':[{'source_ref':'ok','tool':'prepare_merchant_contract_cancel',
        'data':{'id':'abc123','summary':{'contract_id':'sub01-1'}}}]}
    for text in ('合同编号 sub01-1，回执号 abc123。','回执编号：abc123。'):
        assert runtime.render_response(runtime.protocol.Respond(text=text,evidence_refs=['ok']),r)[0]==text
    for text in ('合同编号 sub01-2。','回执号 abc124。','退款123元。'):
        with pytest.raises(DomainError):runtime.render_response(runtime.protocol.Respond(text=text,evidence_refs=['ok']),r)


def test_incomplete_service_draft_survives_inserted_query(dynamic,monkeypatch):
    db,_=dynamic;credit(db)
    def supplement(messages,context):
        assert context['dialogue_context']['drafts']['service_draft']['subject_id']=='credit-test'
        return calls(('prepare_credit_limit_change',{'amount_yuan':'20000','operation_context':{'behavior':'supplement'}}))
    sequence(monkeypatch,calls(('prepare_credit_limit_change',{'card':'9876'})),calls(('list_accounts',{})),reply('账户信息已核对，可继续补充申请额度。'),supplement)
    result=create(db,session(db),'live','申请调整尾号9876信用卡授信额度')
    result=send(db,session(db),result['id'],'先查一下我的账户')
    assert result['status']=='completed' and not result['authorization_operations']
    result=send(db,session(db),result['id'],'新总额度改为20000元')
    assert result['status']=='awaiting_confirmation' and result['service_request']['summary']['requested_limit_minor']==2000000
    assert result['service_request']['status']=='pending_confirmation'


def test_old_missing_field_question_can_recover_explicit_user_card(dynamic,monkeypatch):
    db,_=dynamic;credit(db)
    sequence(monkeypatch,reply('请补充申请的新总额度。',needs_input=True),calls(('list_accounts',{})),reply('余额已查询。'),
        calls(('prepare_credit_limit_change',{'card':'9876','amount_yuan':'20000','operation_context':{'behavior':'supplement'}})))
    result=create(db,session(db),'live','申请调整尾号9876信用卡授信额度');tid=result['id']
    send(db,session(db),tid,'先查询余额')
    result=send(db,session(db),tid,'继续刚才的调额申请，新总额度改为20000元')
    assert result['service_request']['summary']['requested_limit_minor']==2000000
    assert result['status']=='awaiting_confirmation'


@pytest.mark.parametrize('initial,interruption,current',[
    ('申请调整尾号9876信用卡授信额度','算了，不办了','继续刚才的调额申请，新总额度改为20000元'),
    ('申请调整尾号9876信用卡授信额度','先查余额','新总额度改为20000元'),
    ('如何申请调整尾号9876信用卡授信额度？','先查余额','继续刚才的调额申请，新总额度改为20000元'),
    ('申请调整尾号9876信用卡授信额度','先查余额','继续刚才的调额申请'),
])
def test_history_recovery_rejects_cancelled_implicit_advisory_or_unstated_amount(dynamic,monkeypatch,initial,interruption,current):
    db,_=dynamic;credit(db)
    sequence(monkeypatch,reply('请明确办理信息。',needs_input=True),reply('收到。'),
        calls(('prepare_credit_limit_change',{'card':'9876','amount_yuan':'20000','operation_context':{'behavior':'supplement'}})),reply('需要重新明确办理资料。',needs_input=True))
    result=create(db,session(db),'live',initial);tid=result['id'];send(db,session(db),tid,interruption)
    result=send(db,session(db),tid,current)
    assert not result['service_request'] and not result['authorization_operations']


def test_list_account_facts_support_amounts_and_only_returned_tails(dynamic,monkeypatch):
    db,_=dynamic
    def answer(messages,context):
        obs=json.loads(messages[-1]['content'])
        amount=next(f for f in obs['facts'] if f['key'].endswith('.available_balance_minor'))
        mask=obs['data'][0]['account_mask']
        return reply('尾号'+mask[2:]+'账户可用余额{{'+amount['id']+'}}。',evidence_refs=[obs['source_ref']])
    sequence(monkeypatch,calls(('list_accounts',{})),answer)
    result=create(db,session(db),'live','查账户余额')
    assert result['status']=='completed' and '元' in result['messages'][-1]['content']
    r=runtime.current_evidence(state(db,result['id']))
    with pytest.raises(DomainError):runtime.render_response(runtime.protocol.Respond(text='尾号0000账户可用余额。',evidence_refs=[r['evidence'][0]['source_ref']]),r)


def test_amount_condition_must_match_the_explicit_argument():
    validate_context('prepare_credit_limit_change',{'conditions':['申请新总额度20000元（不是增加额）']},amount_yuan='20000')
    with pytest.raises(DomainError):validate_context('prepare_credit_limit_change',{'conditions':['申请新总额度25000元（不是增加额）']},amount_yuan='20000')


def test_old_service_receipt_does_not_complete_new_question(dynamic,monkeypatch):
    db,_=dynamic;credit(db)
    sequence(monkeypatch,calls(('prepare_credit_limit_change',{'card':'9876','amount_yuan':'20000'})),
        reply('申请已提交，仍需独立审批。'),reply('请明确要查看哪一段账单。',needs_input=True))
    result=create(db,session(db),'live','将尾号9876信用卡授信额度改为20000元');tid=result['id'];action=result['authorization_operations'][0]
    from app.agent import authorization
    from test_security import mfa
    mfa(db,action['resource_id'],kind='service')
    token=authorization.issue_token(db,session(db),tid,'service',action['resource_id'],action['action_id'])['confirmation_token']
    authorization.confirm(db,session(db),tid,'service',action['resource_id'],action['action_id'],token)
    runtime.after_authorization(db,session(db),tid,'service',action['resource_id'],action['action_id'],view(db,db.get(AgentTask,tid)))
    result=send(db,session(db),tid,'分析一下账单')
    assert result['status']=='gathering_info' and view(db,db.get(AgentTask,tid))['status']=='gathering_info'
    assert result['service_request']['status']=='submitted'


def test_failed_synthesis_keeps_receipt_and_resume_does_not_resubmit(dynamic,monkeypatch):
    db,_=dynamic;credit(db)
    def unavailable(*a):raise DomainError('MODEL_UNAVAILABLE','模拟网络断开',503)
    sequence(monkeypatch,calls(('prepare_credit_limit_change',{'card':'9876','amount_yuan':'20000'})),unavailable,
        reply('申请回执已保留，当前仍等待独立审批。'))
    result=create(db,session(db),'live','将尾号9876信用卡授信额度改为20000元');tid=result['id'];op=result['authorization_operations'][0]
    from app.agent import authorization
    from test_security import mfa
    from sqlalchemy import select,func
    from app.data.servicing_models import ServiceRequest
    mfa(db,op['resource_id'],kind='service')
    token=authorization.issue_token(db,session(db),tid,'service',op['resource_id'],op['action_id'])['confirmation_token']
    authorization.confirm(db,session(db),tid,'service',op['resource_id'],op['action_id'],token)
    result=runtime.after_authorization(db,session(db),tid,'service',op['resource_id'],op['action_id'],view(db,db.get(AgentTask,tid)))
    assert result['status']=='partially_completed' and result['service_request']['status']=='submitted'
    result=runtime.resume_control(db,session(db),tid,result)
    assert result['status']=='completed' and result['service_request']['id']==op['resource_id']
    assert db.scalar(select(func.count()).select_from(ServiceRequest))==1


def test_stale_query_evidence_is_not_current_authorization_context(dynamic,monkeypatch):
    db,_=dynamic;sequence(monkeypatch,calls(('read_subscriptions',{})),reply('订阅已核对。'))
    result=create(db,session(db),'live','查看订阅');tid=result['id']
    confirm_subscription_query(db,session(db),tid,result['next_actions'][0]['pending_id'])
    task=db.get(AgentTask,tid);r=state(db,tid);runtime.invalidate_snapshot(r)
    assert not runtime.context(db,task,r)['authorization_context']['subscription_read_completed']


def test_read_value_error_does_not_give_calculator_instructions(dynamic,monkeypatch):
    db,_=dynamic
    def reject(*a,**k):raise ValueError('sensitive raw input must not be echoed')
    monkeypatch.setattr(runtime.registry,'execute',reject)
    def feedback(messages,context):
        error=json.loads(messages[-1]['content'])['error']
        assert 'calculate' not in error['message'] and 'sensitive' not in error['message']
        return reply('参数格式需要核对。')
    sequence(monkeypatch,calls(('list_accounts',{})),feedback)
    create(db,session(db),'live','查看账户')
