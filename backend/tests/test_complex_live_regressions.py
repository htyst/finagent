"""Bugs reproduced with the real provider, including safe negative boundaries."""
import json
from types import SimpleNamespace
import pytest
from sqlalchemy import select,func
from app.agent import dynamic_runtime as rt, checkpoints, cancellation_language as cl
from app.agent.subscription_selection import most_expensive, continuation_quote
from app.agent.service import create,send
from app.agent.conversation_reads import confirm_subscription_query
from app.data.models import AgentTask,DemoSetting
from app.data.servicing_models import ServiceRequest,MerchantContractState
from app.domain.errors import DomainError
from app.domain import security
from test_dynamic_tools import dynamic,sequence,calls,reply,session,state,transfers
from test_servicing import add


def subscriptions(db):
    for key,name,amount,enabled in [('cheap','音乐',1500,True),('chosen','影音',2500,True),('off','工具',3900,False)]:
        add(db,'subscriptions',key,dict(subscription_id=key,service_name=name,owner_id='u1',
            auto_renew_enabled=str(enabled).lower(),status='active',current_price_minor=str(amount),
            cycle_months='1',billing_cycle='monthly',access_end_date='2026-10-30',next_billing_date='2026-10-30'))


def test_relative_cancellation_stages_unique_renewing_contract_after_read_consent(dynamic,monkeypatch):
    db,_=dynamic;subscriptions(db)
    sequence(monkeypatch,calls(('read_subscriptions',{})),calls(('prepare_merchant_contract_cancel',{})))
    result=create(db,session(db),'live','先查看订阅扣费，然后帮我把最贵的那一项取消订阅')
    assert not result.get('service_request')
    result=confirm_subscription_query(db,session(db),result['id'],result['next_actions'][0]['pending_id'])
    assert result['service_request']['subject_id']=='chosen'
    assert result['service_request']['status']=='pending_confirmation'
    assert '影音' in result['messages'][-1]['content'] and len(result['messages'][-1]['content'])<250
    assert not db.scalar(select(func.count()).select_from(MerchantContractState))
    assert transfers(db)==0


@pytest.mark.parametrize('phrasing', [
    '先查看订阅扣费，然后帮我把最贵的那一项取消订阅',   # verb-first (original acceptance case)
    '查一下我的订阅扣费，把最贵的那项取消',             # object-first
    '把最贵的订阅取消',                                 # object-first, no 取消订阅
    '我的订阅里最贵那个，帮我取消掉',                   # object-first with filler
])
def test_relative_cancellation_accepts_any_word_order(dynamic, monkeypatch, phrasing):
    """The intent gate must not depend on the verb preceding the noun."""
    db, _ = dynamic;subscriptions(db)
    sequence(monkeypatch,calls(('read_subscriptions',{})),calls(('prepare_merchant_contract_cancel',{})))
    result=create(db,session(db),'live',phrasing)
    if result.get('next_actions'):
        result=confirm_subscription_query(db,session(db),result['id'],result['next_actions'][0]['pending_id'])
    assert result['service_request']['subject_id']=='chosen'
    assert result['service_request']['status']=='pending_confirmation'
    assert not db.scalar(select(func.count()).select_from(MerchantContractState))
    assert transfers(db)==0


@pytest.mark.parametrize('text', [
    '怎么取消订阅','如何取消我的会员','别取消订阅','不要取消会员合同','已经取消订阅了','订阅多少钱一个月',
])
def test_cancellation_phrasing_still_rejects_questions_and_refusals(text):
    from app.agent.subscription_selection import cancels_subscription
    assert cancels_subscription(text) is False


def test_short_confirmation_continues_unfinished_business_not_goal_cancel(dynamic,monkeypatch):
    db,_=dynamic;subscriptions(db)
    sequence(monkeypatch,calls(('read_subscriptions',{})),reply('仍在续费的最高价是影音，是否准备它的取消固定单？',needs_input=True))
    result=create(db,session(db),'live','先查订阅，然后取消影音的订阅')
    result=confirm_subscription_query(db,session(db),result['id'],result['next_actions'][0]['pending_id'])
    sequence(monkeypatch,calls(('prepare_merchant_contract_cancel',{})))
    result=send(db,session(db),result['id'],'确认取消')
    assert result['service_request']['subject_id']=='chosen'
    assert result['execution_graph']['status']=='waiting_authorization'
    assert not any(g.get('user_cancelled') for g in state(db,result['id'])['goals'])
    assert transfers(db)==0


@pytest.mark.parametrize('tool',['manage_task','revise_goals'])
def test_business_cancel_confirmation_cannot_terminate_goal(dynamic,monkeypatch,tool):
    db,_=dynamic
    sequence(monkeypatch,calls(('list_accounts',{})),reply('尚需核对订阅对象。',needs_input=True))
    result=create(db,session(db),'live','帮我取消最贵的订阅')
    args=dict(action='cancel',instruction_quote='确认取消')
    if tool=='revise_goals':args.update(goal_ids=['g1'])
    sequence(monkeypatch,calls((tool,args)),reply('请核对要办理的订阅，尚未取消。',needs_input=True))
    result=send(db,session(db),result['id'],'确认取消')
    assert not any(g.get('user_cancelled') for g in state(db,result['id'])['goals'])
    assert result['execution_graph']['status']!='cancelled'
    assert not db.get(AgentTask,result['id']).stop_requested


@pytest.mark.parametrize('text',['已关闭自动续费，取消它没有实际效果。','未能生成取消固定单。','订阅尚未取消。','本轮「确认取消」没能生成单据。','已关闭续费，无需取消。','如果已取消就不必重复申请。'])
def test_explanation_is_not_a_cancel_receipt(text):
    assert not cl.claims(text)


@pytest.mark.parametrize('text',['已取消星幕影音订阅。','我已为你取消订阅。','订阅取消成功。','已经把这份合同取消。'])
def test_real_cancel_claim_still_requires_receipt(text):
    r=dict(graph_schema='g01-v1',goals=[dict(id='g1')],nodes=[],turn_goal_ids=['g1'],evidence=[],facts=[])
    with pytest.raises(DomainError) as error:rt.validate_goal_claim(text,r)
    assert error.value.code=='GOAL_CONTROL_REQUIRED'


@pytest.mark.parametrize('message',['取消订阅','确认取消','帮我取消最贵那一项订阅','关闭会员续费，然后取消这项订阅'])
def test_business_words_are_not_workflow_control(message):
    assert not cl.goal_cancel_allowed('取消',message,['取消订阅'])


@pytest.mark.parametrize('message',['取消旅行目标','取消订阅这个目标','取消整个任务','取消旅行','算了','取消'])
def test_explicit_workflow_cancellation_remains_available(message):
    assert cl.goal_cancel_allowed(message,message)


def test_rank_ties_and_mixed_periods_require_selection():
    a=dict(subscription_id='a',service_name='月付',auto_renew_enabled=True,status='active',current_price_minor=2000,cycle_months=1)
    b=dict(a,subscription_id='b',service_name='另一项')
    with pytest.raises(DomainError):most_expensive([a,b],'取消最贵订阅')
    b.update(current_price_minor=18000,cycle_months=12)
    with pytest.raises(DomainError):most_expensive([a,b],'取消最贵订阅')
    assert most_expensive([a,b],'按折月费用取消最贵订阅')['subscription_id']=='a'
    assert most_expensive([a,b],'按每期价格取消最贵订阅')['subscription_id']=='b'


def test_prior_cancel_cannot_replay_completed_or_foreign_goal():
    r=dict(request='确认取消',goals=[dict(id='g1',quote='取消最贵的订阅',status='failed')],nodes=[])
    assert continuation_quote(r,{'goal_ids':['g1']},'确认取消').startswith('取消最贵的订阅；')
    assert continuation_quote(r,{'goal_ids':['g2']},'确认取消')=='确认取消'
    r['goals'][0]['receipt_refs']=['service:paid']
    assert continuation_quote(r,{'goal_ids':['g1']},'确认取消')=='确认取消'


def test_subscription_dates_and_counts_can_be_rendered_without_fake_literals():
    from app.agent import analysis_workflow as facts
    from app.agent.dynamic_protocol import Respond
    data={'as_of':'2026-09-24','period':{'start_date':'2025-09-25','end_date':'2026-09-24'},
        'summary':dict(contract_count=5,auto_renew_contract_count=4,paid_attempt_count=60,failed_attempt_count=0),
        'contracts':[dict(service_name='影音',current_price_minor=2500,billing_cycle='monthly',access_end_date='2026-10-07',next_billing_date='2026-10-07')]}
    r=dict(graph_schema='g01-v1',goals=[dict(id='g1')],nodes=[],facts=[],evidence=[dict(tool='read_subscriptions',data=data,source_ref='call:test:s1')])
    facts.add_facts(r,dict(id='s1',name='read_subscriptions'),facts.facts_from(dict(name='read_subscriptions'),data),'call:test:s1')
    text,_=rt.render_response(Respond(text='查询期2025-09-25至2026-09-24，共5份合同，4份续费，已付60次。影音权益至2026-10-07。',evidence_refs=['call:test:s1']),r)
    assert '2026-10-07' in text
    with pytest.raises(DomainError):rt.render_response(Respond(text='权益至2099-10-07，已有99份合同。',evidence_refs=['call:test:s1']),r)


@pytest.mark.parametrize('changed',[False,True])
def test_subscription_read_consent_invalidates_only_changed_financial_snapshot(dynamic,monkeypatch,changed):
    from app.data.models import Account
    db,_=dynamic
    sequence(monkeypatch,calls(('list_accounts',{})),calls(('read_subscriptions',{})),reply('查询结果已核对，具体以合同快照为准。'))
    result=create(db,session(db),'live','查询账户余额和订阅')
    tid=result['id'];reference=state(db,tid)['evidence'][0]['source_ref']
    if changed:
        db.get(Account,'a1c').version+=1;db.commit()
    confirm_subscription_query(db,session(db),tid,result['next_actions'][0]['pending_id'])
    assert (reference in state(db,tid).get('stale_evidence',[]))==changed


def test_lookup_cannot_finish_requested_cancellation(dynamic,monkeypatch):
    db,_=dynamic;subscriptions(db)
    sequence(monkeypatch,calls(('read_subscriptions',{})),reply('查询已完成，最贵的是已停续费的工具，所以无需办理。'),calls(('prepare_merchant_contract_cancel',{})))
    result=create(db,session(db),'live','先查订阅，然后帮我取消最贵的订阅')
    result=confirm_subscription_query(db,session(db),result['id'],result['next_actions'][0]['pending_id'])
    assert result['service_request']['subject_id']=='chosen'
    assert result['execution_graph']['status']=='waiting_authorization'
    assert any(n.get('error_code')=='OPERATION_NOT_PREPARED' for n in state(db,result['id'])['nodes'])


def test_receipt_claim_does_not_escape_through_unrelated_negative_clause():
    assert cl.claims('订阅已取消但没有退款。')
    assert not cl.goal_cancel_allowed('取消目标','不要取消目标')


def test_amount_cannot_fill_expiry_date():
    from app.agent.dynamic_protocol import Respond
    r=dict(evidence=[dict(tool='read_subscriptions',source_ref='call:test:s1',data={})],
        facts=[dict(id='f1',key='contract_0',unit='yuan',value='39',source_ref='call:test:s1')])
    with pytest.raises(DomainError) as error:
        rt.render_response(Respond(text='权益截止{{f1}}。',evidence_refs=['call:test:s1']),r)
    assert error.value.code=='ANALYSIS_UNIT'


def test_unbacked_instruction_suffix_cannot_inherit_old_operation():
    r=dict(request='确认取消',goals=[dict(id='g1',quote='查询余额',status='completed')],nodes=[])
    assert continuation_quote(r,{'goal_ids':['g1']},'随便取消；确认取消')=='随便取消；确认取消'


def test_operation_goal_needs_more_than_a_read_result():
    from app.agent.execution_graph import refresh
    r=dict(graph_schema='g01-v1',status='completed',goals=[dict(id='cancel',kind='operation',answer_observed=True)],
        nodes=[dict(id='s1',name='read_subscriptions',goal_ids=['cancel'],policy={'effect':'read'},status='succeeded',result_refs=['call:test:s1'])])
    refresh(r)
    assert r['goals'][0]['status']!='completed'


def test_explicit_dues_condition_cannot_be_dropped_with_a_caveat():
    from app.agent.planning_reply import validate_assertions
    r=dict(user_messages=['已知近期应付款先扣除再判断旅行预算'],facts=[dict(id='f1',key='result',value='100',unit='yuan',known_dues_not_deducted=True)],evidence=[])
    with pytest.raises(DomainError):validate_assertions('余量{{f1}}，尚未扣应付款。',r)
    r['user_messages'].append('本次不考虑应付款，只看基础口径')
    validate_assertions('余量{{f1}}，尚未扣应付款。',r)


def test_chat_confirmation_preserves_existing_cancel_form_and_graph(dynamic,monkeypatch):
    db,_=dynamic;subscriptions(db)
    sequence(monkeypatch,calls(('read_subscriptions',{})),calls(('prepare_merchant_contract_cancel',{})))
    result=create(db,session(db),'live','先查订阅，再取消最贵那项订阅')
    result=confirm_subscription_query(db,session(db),result['id'],result['next_actions'][0]['pending_id'])
    rid=result['service_request']['id']
    def no_model(*args):raise AssertionError('Existing confirmation should need no model')
    monkeypatch.setattr(rt.protocol,'step',no_model)
    result=send(db,session(db),result['id'],'确认取消')
    assert result['service_request']['id']==rid and result['service_request']['status']=='pending_confirmation'
    assert result['execution_graph']['status']=='waiting_authorization'
    assert db.scalar(select(func.count()).select_from(ServiceRequest))==1
    assert not db.scalar(select(func.count()).select_from(MerchantContractState))
    assert not next(n for n in state(db,result['id'])['nodes'] if n.get('resource_ref')).get('invalidated')


def test_monthly_selection_accepts_renewal_word_and_rejects_wrong_annual_target(dynamic,monkeypatch):
    db,_=dynamic;subscriptions(db)
    add(db,'subscriptions','annual',dict(subscription_id='annual',owner_id='u1',service_name='年付存储',
        auto_renew_enabled='true',status='active',current_price_minor='19800',cycle_months='12',billing_cycle='annual',access_end_date='2026-10-30'))
    sequence(monkeypatch,calls(('read_subscriptions',{})),calls(('prepare_merchant_contract_cancel',{'merchant':'年付存储'})),calls(('prepare_merchant_contract_cancel',{})))
    text='先查看订阅，帮我取消按折月费用比较最贵且仍在自动续费的一项，只准备单，不执行。'
    result=create(db,session(db),'live',text)
    result=confirm_subscription_query(db,session(db),result['id'],result['next_actions'][0]['pending_id'])
    assert result['service_request']['subject_id']=='chosen'
    assert result['execution_graph']['status']=='waiting_authorization'


def test_financial_snapshot_change_does_not_cancel_existing_prepared_resource(dynamic,monkeypatch):
    from app.data.models import Account
    db,_=dynamic;subscriptions(db)
    sequence(monkeypatch,calls(('read_subscriptions',{})),calls(('prepare_merchant_contract_cancel',{})))
    result=create(db,session(db),'live','查询订阅并取消最贵的订阅')
    result=confirm_subscription_query(db,session(db),result['id'],result['next_actions'][0]['pending_id'])
    task=db.get(AgentTask,result['id']);r=state(db,task.id)
    db.get(Account,'a1c').version+=1;db.commit()
    checkpoints.reconcile(db,session(db),task,r)
    assert r.get('stale_evidence')
    assert not next(n for n in r['nodes'] if n.get('resource_ref')).get('invalidated')


def test_rejected_selection_does_not_require_a_fictitious_receipt_after_real_cancel(dynamic,monkeypatch):
    from test_servicing import confirm
    from app.agent.execution_graph import refresh
    db,_=dynamic;subscriptions(db)
    sequence(monkeypatch,calls(('read_subscriptions',{})),calls(('prepare_merchant_contract_cancel',{'merchant':'工具'})),calls(('prepare_merchant_contract_cancel',{})))
    result=create(db,session(db),'live','查询订阅并取消最贵订阅')
    result=confirm_subscription_query(db,session(db),result['id'],result['next_actions'][0]['pending_id'])
    confirm(db,result['service_request']['id']) # Deterministic unit fixture, never a live evaluation.
    task=db.get(AgentTask,result['id']);r=state(db,task.id)
    checkpoints.reconcile(db,session(db),task,r);refresh(r)
    assert r['goals'][0]['status']=='completed'
    assert any(n.get('error_code')=='SUBSCRIPTION_SELECTION_MISMATCH' for n in r['nodes'])
