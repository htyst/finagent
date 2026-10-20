"""Regressions from the 2026-10-19 real-provider cross-scenario evaluation."""
import json
from datetime import timedelta,datetime,timezone
from uuid import uuid4
from types import SimpleNamespace
import pytest
from sqlalchemy import select,func
from app.agent import planning_assumptions as assumptions,planning_reply,dynamic_runtime as rt
from app.agent import read_presentation,checkpoints
from app.agent.service import create,send
from app.data.models import AgentTask,DemoSession
from app.data.workflow_models import WorkflowJob,WorkflowReview
from app.domain import workflows as w
from app.domain.errors import DomainError
from test_dynamic_tools import dynamic,sequence,calls,reply,session,state,transfers
from test_http import client


def test_jobs_survive_user_switch_without_retaining_session_authority(dynamic,client):
    db,_=dynamic;now=w.stamp(w.now());tid=uuid4().hex
    db.add(AgentTask(id=tid,owner_id='u1',session_id='s1',dataset_version=session(db).dataset_version,mode='live',status='completed',context_json='{}',created_at_utc=now,updated_at_utc=now));db.flush()
    job=WorkflowJob(id=uuid4().hex,event_key='nonfinancial-fixture',task_id=tid,owner_id='u1',session_id='s1',dataset_version=session(db).dataset_version,payload_json='{}',status='blocked',attempts=0,next_run_at_utc=now,created_at_utc=now,updated_at_utc=now)
    db.add(job);db.commit();client.cookies.set('finagent_demo_session','s1')
    assert client.post('/api/demo/session',json={'user_id':'u2'}).status_code==200
    db.expire_all();assert db.get(WorkflowJob,job.id)
    assert datetime.fromisoformat(db.get(DemoSession,'s1').expires_at_utc)<=datetime.now(timezone.utc)
    client.cookies.clear();client.cookies.set('finagent_demo_session','s1')
    assert client.get('/api/workflows').status_code==401


def test_conditional_advice_is_scoped_to_alternative_branch(dynamic,monkeypatch):
    db,_=dynamic
    text='如果可用余额足够100元，准备从活期账户向储蓄账户转100元；否则只给建议。不要替我确认。'
    assert not assumptions.advice_only(text)
    sequence(monkeypatch,calls(('list_accounts',{})),calls(('prepare_transfer',dict(source='活期账户',target='储蓄账户',amount_yuan='100',operation_context={'instruction_quote':'准备从活期账户向储蓄账户转100元'}))))
    result=create(db,session(db),'live',text)
    assert result['proposal']['status']=='pending_confirmation' and transfers(db)==0


@pytest.mark.parametrize('text',['只分析。如果条件成立就准备转账，否则只建议','不要转账。如果余额够就分析，否则建议','如果余额够就准备，否则只给建议。不要转账。','如果余额够就准备，否则只给建议；不要转账。'])
def test_global_analysis_prohibition_remains(text):assert assumptions.advice_only(text)


def test_deferred_confirmation_keeps_original_six_month_constraint():
    messages=['取消轻听音乐，并分析未来六个月可少付多少钱。','先不确认取消，先把六个月可少付的金额算给我看。']
    assert assumptions.validate_quote(SimpleNamespace(quote='未来六个月',unit='number',label='期限'),messages)=='未来六个月'
    assert assumptions.barriers(messages)['time']==0
    assert assumptions.barriers(messages+['期限改为三个月'])['time']==2


def test_timeout_retry_resolves_logical_goal(dynamic,monkeypatch):
    db,_=dynamic;original=rt.registry.execute;once=[False]
    def read(*a,**kw):
        if kw['name']=='list_accounts' and not once[0]:
            once[0]=True;raise DomainError('READ_TIMEOUT','读取暂时失败',503)
        return original(*a,**kw)
    monkeypatch.setattr(rt.registry,'execute',read)
    sequence(monkeypatch,calls(('list_accounts',{})),reply('本次查询失败，请稍后重试。'),calls(('list_accounts',{})),reply('查询已恢复，余额以查询结果为准。'))
    result=create(db,session(db),'live','查询可用余额')
    assert result['execution_graph']['goals'][0]['status']=='failed'
    result=send(db,session(db),result['id'],'重试刚才的查询')
    assert result['execution_graph']['goals'][0]['status']=='completed'
    assert result['execution_graph']['nodes'][0]['recovered_by']
    assert '未完成：' not in result['messages'][-1]['content']


def test_emergency_stop_cancels_dag_and_pending_form(dynamic,monkeypatch):
    db,_=dynamic;sequence(monkeypatch,calls(('prepare_transfer',dict(source='活期账户',target='储蓄账户',amount_yuan='100'))))
    result=create(db,session(db),'live','准备从活期账户转100元到储蓄账户')
    result=send(db,session(db),result['id'],'停止任务')
    assert result['status']=='cancelled' and result['execution_graph']['status']=='cancelled'
    assert all(n['status']=='cancelled' for n in result['execution_graph']['nodes'])
    assert transfers(db)==0


def test_handoff_instruction_can_contain_unrelated_negation(dynamic,monkeypatch):
    db,_=dynamic;text='不要再问，我要找人工'
    sequence(monkeypatch,calls(('manage_task',dict(action='handoff',instruction_quote=text))))
    result=create(db,session(db),'live',text)
    assert result['control']['state']=='handoff' and result['execution_graph']['status']=='paused'


def test_negative_control_with_pronoun_keeps_pending_form(dynamic,monkeypatch):
    db,_=dynamic
    sequence(monkeypatch,calls(('prepare_transfer',dict(source='活期账户',target='储蓄账户',amount_yuan='100'))))
    result=create(db,session(db),'live','准备从活期账户转100元到储蓄账户')
    text='不要帮我取消这笔转账'
    sequence(monkeypatch,calls(('manage_task',dict(action='cancel',instruction_quote=text))),reply('原待确认操作保留，没有取消。'))
    result=send(db,session(db),result['id'],text)
    assert result['proposal']['status']=='pending_confirmation' and not db.get(AgentTask,result['id']).stop_requested
    assert transfers(db)==0


def test_independent_query_runs_after_pending_transfer_in_same_batch(dynamic,monkeypatch):
    db,_=dynamic
    sequence(monkeypatch,calls(('declare_goals',{'goals':[{'id':'pay','text':'准备转账','quote':'准备转账','kind':'operation'},{'id':'balance','text':'查余额','quote':'查余额','kind':'query'}]})),calls(
        ('prepare_transfer',dict(source='活期账户',target='储蓄账户',amount_yuan='100',execution={'goal_ids':['pay']})),
        ('list_accounts',{'execution':{'goal_ids':['balance']}})))
    result=create(db,session(db),'live','准备转账，从活期账户转100元到储蓄账户，同时独立查余额')
    assert result['proposal']['status']=='pending_confirmation'
    goals={g['id']:g['status'] for g in result['execution_graph']['goals']}
    assert goals=={'pay':'waiting_authorization','balance':'completed'}
    assert '独立查询已完成' in result['messages'][-1]['content'] and '可用余额' in result['messages'][-1]['content']
    assert transfers(db)==0


def test_model_interruption_keeps_analysis_incomplete(dynamic,monkeypatch):
    db,_=dynamic
    def interrupted(*a):raise DomainError('MODEL_TIMEOUT','模型中断',503)
    sequence(monkeypatch,calls(('financial_planning_basis',{}),('list_accounts',{})),interrupted)
    result=create(db,session(db),'live','结合资金与消费判断旅行预算')
    assert result['execution_graph']['goals'][0]['kind']=='analysis'
    assert result['execution_graph']['status']!='completed'


def test_no_successful_evidence_does_not_claim_results_preserved():
    text=read_presentation.fallback({'facts':[],'evidence':[{'tool':'list_accounts','error':{'code':'READ_TIMEOUT'}}]},'MODEL_TIMEOUT')
    assert '已保留查询结果' not in text


def test_unknown_budget_and_selection_questions_remain_waiting_input(dynamic,monkeypatch):
    from app.agent.dynamic_protocol import Respond
    assert Respond(text='请告诉我取消哪几项。').needs_input
    db,_=dynamic;sequence(monkeypatch,calls(('list_accounts',{})),reply('资金已核对。请告诉我旅行预算。'))
    result=create(db,session(db),'live','先看看资金，再规划预算待定的旅行')
    assert result['execution_graph']['status']=='waiting_input'


def test_fallback_removes_array_paths_and_unknown_field_names():
    from app.agent.analysis_workflow import facts_from
    items=facts_from({'name':'list_accounts'},[{'balance_minor':100,'new_internal_minor':200}])
    for f in items:f['source_ref']='safe:query'
    text=read_presentation.fact_block({'facts':items})
    assert '_minor' not in text and '.0.' not in text and '账面余额' in text


@pytest.mark.parametrize('wrong',[True,False])
@pytest.mark.parametrize('price',[19800,'19800'])
def test_single_charge_is_compared_to_limit_without_monthly_amortization(wrong,price):
    r={'user_messages':['把单笔限额设为100元'],'facts':[], 'evidence':[{'tool':'read_subscriptions','raw_data':{'contracts':[{'current_price_minor':price}]}}]}
    if wrong:
        with pytest.raises(DomainError):planning_reply.validate_assertions('单期金额都低于该限额，不会让任何一笔订阅失败。',r)
    else:planning_reply.validate_assertions('年费198元高于限额；月付的合同需逐项比较。',r)


def test_month_comparison_cannot_contradict_actual_mean():
    r={'user_messages':[], 'facts':[{'key':'monthly_expense_mean','value':'7747.83'}], 'evidence':[{'tool':'summarize_spending','query':{'start':'2026-08-01','end':'2026-08-31'},'raw_data':{'current':{'totals':{'net_minor':684195,'is_complete':True}}}}]}
    with pytest.raises(DomainError):planning_reply.validate_assertions('8月消费高于近三个月月均水平。',r)
    planning_reply.validate_assertions('8月消费低于近三个月月均水平。',r)


def test_review_draft_requires_explicit_ui_consent_before_scheduling(dynamic,monkeypatch):
    db,_=dynamic;instant=w.now();text='从明天开始每5天检查余额是否低于5000元，持续30天，先准备复查草稿。'
    args=dict(title='旅行资金复查',run_at=w.stamp(instant+timedelta(days=1)),end_at=w.stamp(instant+timedelta(days=31)),interval_days=5,target_yuan='5000',instruction_quote=text)
    sequence(monkeypatch,calls(('prepare_review_plan',args)))
    result=create(db,session(db),'live',text)
    assert db.scalar(select(func.count()).select_from(WorkflowReview))==0
    portfolio=w.portfolio(db,session(db));draft=portfolio['drafts'][0]
    assert draft['interval_days']==5 and draft['target_minor']==500000
    assert result['execution_graph']['status']=='waiting_input'
    terms={k:v for k,v in draft.items() if k in w.ReviewInput.model_fields}
    payload=w.ReviewInput(**terms,consent=False,request_id='explicit-ui-test')
    with pytest.raises(DomainError):w.create_review(db,session(db),payload)
    created=w.create_review(db,session(db),payload.model_copy(update={'consent':True}))
    assert created['status']=='scheduled' and not w.portfolio(db,session(db))['drafts']
    assert transfers(db)==0


def test_pending_form_count_requires_actual_prepared_resources():
    r={'nodes':[{'resource_ref':{'kind':'proposal','id':'actual'},'status':'awaiting_confirmation'}]}
    with pytest.raises(DomainError):rt.validate_goal_claim('转账和挂失两张固定单都会保持待确认。',r)
    rt.validate_goal_claim('一张固定单待确认，另一目标尚未准备。',r)
