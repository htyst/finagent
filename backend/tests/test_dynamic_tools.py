import json
from uuid import uuid4
from types import SimpleNamespace
import pytest
from sqlalchemy import select,func
from app.agent import dynamic_protocol as protocol,dynamic_runtime as runtime,language,authorization
from app.agent.service import create,send
from app.agent.conversation_reads import confirm_subscription_query
from app.agent.control import change
from app.data.models import DemoSession,AgentTask,Account,TransferOrder,AgentStep
from app.domain.errors import DomainError
from test_http import client
from test_security import mfa


@pytest.fixture
def dynamic(bank,monkeypatch):
    monkeypatch.setenv('FINAGENT_AGENT_ENGINE','dynamic')
    return bank


def calls(*items):
    return {'role':'assistant','content':None,'tool_calls':[{'id':uuid4().hex,'type':'function','function':{'name':name,'arguments':json.dumps(args,ensure_ascii=False)}} for name,args in items]}


def reply(text='已按证据核对，建议先保留必要生活开支，再明确目标成本。',**kw):return calls(('respond',{'text':text,**kw}))


def sequence(monkeypatch,*steps):
    seen=[];it=iter(steps)
    def step(messages,context):
        seen.append((messages.copy(),context));item=next(it)
        return item(messages,context) if callable(item) else item
    monkeypatch.setattr(protocol,'step',step);return seen


def session(db):return db.get(DemoSession,'s1')
def state(db,tid):return runtime.get(db.get(AgentTask,tid))
def headers():return {'Origin':'http://testserver','X-FinAgent-CSRF':'test-csrf'}
def transfers(db):return db.scalar(select(func.count()).select_from(TransferOrder))


def test_default_engine_is_dynamic(monkeypatch):
    monkeypatch.delenv('FINAGENT_AGENT_ENGINE',raising=False);assert runtime.enabled()


def test_greeting_needs_no_tool_or_old_intent_router(dynamic,monkeypatch):
    db,_=dynamic
    def forbidden(*a,**k):raise AssertionError('old semantic router invoked')
    monkeypatch.setattr(language,'interpret',forbidden)
    seen=sequence(monkeypatch,{'role':'assistant','content':'你好，我可以帮你查账和准备业务。'})
    result=create(db,session(db),'live','你好')
    assert result['status']=='completed' and len(seen)==1 and not result['steps']
    assert result['analysis_workflow']['nodes']==[] and transfers(db)==0


def test_model_selects_followup_after_reading_actual_result(dynamic,monkeypatch):
    db,_=dynamic
    def follow(messages,context):
        obs=json.loads(messages[-1]['content']);assert obs['status']=='completed' and obs['call_id']=='s1'
        assert any(f['key']=='available_liquid' for f in obs['facts'])
        return calls(('analyze_finances',{'kind':'cashflow'}))
    seen=sequence(monkeypatch,calls(('financial_planning_basis',{})),follow,reply())
    result=create(db,session(db),'live','根据我的存款和日常收支分析换工作能否缓冲')
    assert len(seen)==3 and result['status']=='completed'
    assert [n['name'] for n in result['analysis_workflow']['nodes']]==['financial_planning_basis','analyze_finances','respond']
    assert '当前可用活期' in result['messages'][-1]['content'] and transfers(db)==0
    assert 'analysis_workflow' not in seen[0][1]['dialogue_context']


def test_multiple_tools_same_round(dynamic,monkeypatch):
    db,_=dynamic
    sequence(monkeypatch,calls(('list_accounts',{}),('list_cards',{})),reply('已核对账户与卡片，具体金额和状态见下方。'))
    result=create(db,session(db),'live','查看我的账户和卡片')
    assert [s['tool_name'] for s in result['steps']]==['list_accounts','list_cards']
    assert result['status']=='completed' and transfers(db)==0


@pytest.mark.parametrize('bad',[('execute_transfer',{}),('list_accounts',{'owner_id':'u2'}),('read_subscriptions',{'authorized':True})])
def test_whole_batch_validated_before_any_tool(dynamic,monkeypatch,bad):
    db,_=dynamic;executed=[]
    monkeypatch.setattr(runtime.registry,'execute',lambda *a,**k:executed.append(k['name']))
    sequence(monkeypatch,calls(('list_cards',{}),bad),reply('这批调用未通过校验，没有执行业务。'))
    result=create(db,session(db),'live','查询本人账户')
    assert not executed and not result['proposal'] and transfers(db)==0


def test_subscription_tool_suspends_then_continues_same_loop(dynamic,monkeypatch):
    db,_=dynamic
    def continued(messages,context):
        obs=json.loads(messages[-1]['content']);assert obs['status']=='completed'
        assert context['authorization_context']['subscription_read_completed']
        return calls(('financial_planning_basis',{}))
    seen=sequence(monkeypatch,calls(('read_subscriptions',{})),continued,reply('已核对订阅和消费；是否值得保留还需要了解使用频率。'))
    result=create(db,session(db),'live','核对订阅与日常消费的节省空间')
    tid=result['id'];rid=state(db,tid)['id'];pending=result['next_actions'][0]['pending_id']
    assert len(seen)==1 and state(db,tid)['wait']['kind']=='subscription_query'
    for sid in ('other','s2'):
        with pytest.raises(DomainError):confirm_subscription_query(db,db.get(DemoSession,sid),tid,pending)
    result=confirm_subscription_query(db,session(db),tid,pending)
    assert result['status']=='completed' and len(seen)==3 and state(db,tid)['id']==rid
    assert confirm_subscription_query(db,session(db),tid,pending)['id']==tid and len(seen)==3
    assert transfers(db)==0


def test_paused_task_cannot_grant_query(dynamic,monkeypatch):
    db,_=dynamic;sequence(monkeypatch,calls(('read_subscriptions',{})))
    result=create(db,session(db),'live','查看订阅');pending=result['next_actions'][0]['pending_id']
    change(db,db.get(AgentTask,result['id']),'pause')
    with pytest.raises(DomainError):confirm_subscription_query(db,session(db),result['id'],pending)


def test_new_question_invalidates_old_query_checkpoint(dynamic,monkeypatch):
    db,_=dynamic;sequence(monkeypatch,calls(('read_subscriptions',{})),reply('可以，先讨论你的目标。'))
    result=create(db,session(db),'live','查看订阅');pending=result['next_actions'][0]['pending_id']
    send(db,session(db),result['id'],'换个话题')
    with pytest.raises(DomainError):confirm_subscription_query(db,session(db),result['id'],pending)


def test_transfer_waits_for_fixed_confirmation_then_model_sees_receipt(dynamic,client,monkeypatch):
    db,_=dynamic;client.cookies.set('finagent_demo_session','s1')
    def receipt(messages,context):
        obs=json.loads(messages[-1]['content']);assert obs['status']=='completed'
        assert obs['data']['status']=='executed'
        assert transfers(db)==1
        return reply('已收到真实业务回执，具体办理结果见操作单。')
    seen=sequence(monkeypatch,calls(('prepare_transfer',{'source':'活期账户','target':'储蓄账户','amount_yuan':'100'})),receipt)
    result=create(db,session(db),'live','从活期账户转100元到储蓄账户')
    assert transfers(db)==0 and len(seen)==1 and state(db,result['id'])['wait']['kind']=='authorization'
    action=result['authorization_operations'][0];path=action['authorization_path']
    if action['level']=='red':mfa(db,action['resource_id'])
    token=client.post(path+'/confirmation-token',headers=headers(),json={'action_id':action['action_id']}).json()['data']['confirmation_token']
    payload={'action_id':action['action_id'],'confirmation_token':token}
    response=client.post(path+'/confirm',headers=headers(),json=payload);assert response.status_code==200,response.text
    assert transfers(db)==1 and len(seen)==2 and state(db,result['id'])['wait'] is None
    replay=client.post(path+'/confirm',headers=headers(),json=payload);assert replay.status_code==200
    assert transfers(db)==1 and len(seen)==2


def test_chat_confirmation_does_not_execute(dynamic,monkeypatch):
    db,_=dynamic
    sequence(monkeypatch,calls(('prepare_transfer',{'source':'活期账户','target':'储蓄账户','amount_yuan':'100'})),reply('请在固定操作单核对并点击确认，聊天不会付款。'))
    result=create(db,session(db),'live','从活期账户转100元到储蓄账户')
    result=send(db,session(db),result['id'],'确认')
    assert transfers(db)==0 and result['proposal']['status']=='pending_confirmation'


def test_form_changes_waiter_to_exact_prepared_operation(dynamic,client,monkeypatch):
    db,_=dynamic;client.cookies.set('finagent_demo_session','s1')
    seen=sequence(monkeypatch,calls(('prepare_transfer',{'target':'小林','amount_yuan':'200'})))
    result=create(db,session(db),'live','给小林转200元');tid=result['id']
    assert result['transfer_details'] and state(db,tid)['wait']['kind']=='input'
    response=client.post('/api/tasks/'+tid+'/transfer-details',headers=headers(),json={'source_reference':'a1c','name':'小林','bank_name':'演示银行','account_number':'2131244534'})
    assert response.status_code==200,response.text
    current=response.json()['data'];wait=state(db,tid)['wait']
    assert wait['kind']=='authorization' and current['proposal']['summary']['amount_minor']==20000
    assert current['proposal']['summary']['source']['alias']=='活期账户' and transfers(db)==0 and len(seen)==1


def test_old_user_instruction_cannot_be_replayed_for_new_consultation(dynamic,monkeypatch):
    db,_=dynamic;sequence(monkeypatch,reply('请在需要时明确办理需求。'),
        calls(('prepare_transfer',{'source':'活期账户','target':'储蓄账户','amount_yuan':'100','operation_context':{'instruction_quote':'从活期账户转100元到储蓄账户'}})),reply('本次只讨论理财规划，没有准备转账。'))
    create(db,session(db),'live','从活期账户转100元到储蓄账户')
    result=create(db,session(db),'live','给我理财建议')
    assert not result['proposal'] and transfers(db)==0


def test_unknown_calculation_and_unreturned_facts_rejected():
    r={'facts':[],'user_messages':[]}
    for c in [{'operation':'exec','inputs':[{'value':'1','unit':'number','label':'x'}]},
        {'operation':'sum','inputs':[{'node':'s99','key':'balance'}]}]:
        with pytest.raises(DomainError):runtime.calculate(r,{'arguments':{'title':'计算','calculation_json':json.dumps(c)}})


def test_model_failure_retains_financial_facts(dynamic,monkeypatch):
    db,_=dynamic
    def failure(*args):raise DomainError('MODEL_UNAVAILABLE','模拟网络超时',503)
    sequence(monkeypatch,calls(('financial_planning_basis',{})),failure)
    result=create(db,session(db),'live','根据存款分析资金安排')
    assert result['status']=='partially_completed' and '当前可用活期' in result['messages'][-1]['content']
    assert result['mode']=='live' and transfers(db)==0


@pytest.mark.parametrize('kind',['proposal','wealth','card','subscription','birthday','schedule','collection'])
def test_every_domain_receipt_resumes_tool_loop_once(dynamic,client,monkeypatch,kind):
    from test_chat_authorization import prepare
    db,_=dynamic;client.cookies.set('finagent_demo_session','s1');row,action=prepare(db,kind)
    name={'proposal':'prepare_transfer','wealth':'prepare_wealth_buy','card':'prepare_card_management',
        'subscription':'prepare_subscription_revoke','birthday':'prepare_birthday_create',
        'schedule':'prepare_schedule','collection':'prepare_collection'}[kind]
    message=calls((name,{}));node={'id':'s1','model_call_id':message['tool_calls'][0]['id'],'name':name,'arguments':{},'title':'固定操作单','depends_on':[],'status':'awaiting_confirmation'}
    r={'id':uuid4().hex,'request':'办理测试','nodes':[node],'evidence':[],'facts':[],'messages':[{'role':'user','content':'办理测试'},message],
        'user_messages':['办理测试'],'queue':[],'status':'awaiting_confirmation','completed_operations':[],
        'wait':dict(kind='authorization',node_id='s1',action_id=action['action_id'],resource_kind=kind,resource_id=action['resource_id'],plan_version=row.plan_version,dataset_version=row.dataset_version)}
    runtime.save(row,r);row.mode='live';db.commit()
    def receipt(messages,context):
        data=json.loads(messages[-1]['content'])['data']
        assert data['resource_kind']==kind and data['resource_id']==action['resource_id']
        assert data['status'] in {'executed','settled','submitted','scheduled','collecting'}
        return calls(('list_accounts',{}))
    seen=sequence(monkeypatch,receipt,reply('已收到业务回执，并重新核对账户；具体办理状态以操作单为准。'))
    if action['level']=='red':mfa(db,action['resource_id'],kind=kind)
    path=action['authorization_path'];token=client.post(path+'/confirmation-token',headers=headers(),json={'action_id':action['action_id']}).json()['data']['confirmation_token']
    payload={'action_id':action['action_id'],'confirmation_token':token}
    result=client.post(path+'/confirm',headers=headers(),json=payload)
    assert result.status_code==200,result.text
    assert result.json()['data']['status']=='running_readonly'
    db.expire_all()
    assert len(seen)==2 and state(db,row.id)['status']=='completed' and state(db,row.id)['wait'] is None
    assert client.post(path+'/confirm',headers=headers(),json=payload).status_code==200
    assert len(seen)==2


def test_fact_placeholders_are_resolved_only_from_current_verified_facts():
    r={'evidence':[{'source_ref':'ok','tool':'financial_planning_basis'}],
        'facts':[{'id':'f1','node':'s1','key':'available_liquid','label':'可用活期','value':'1234.56','unit':'yuan','source_ref':'ok'}]}
    text,inserted=runtime.render_response(protocol.Respond(text='当前可用活期{{f1}}，先保留生活费。',evidence_refs=['ok']),r)
    assert inserted and '1,234.56 元' in text
    for text in ('可用{{f99}}','可用1234.57元'):
        with pytest.raises(DomainError):runtime.render_response(protocol.Respond(text=text,evidence_refs=['ok']),r)
    runtime.invalidate_snapshot(r)
    assert not runtime.current_evidence(r)['facts']
    with pytest.raises(DomainError):runtime.render_response(protocol.Respond(text='可用{{f1}}',evidence_refs=['ok']),runtime.current_evidence(r))
    with pytest.raises(DomainError):runtime.calculate(r,{'arguments':{'title':'合计','calculation_json':json.dumps({'operation':'sum','inputs':[{'node':'s1','key':'available_liquid'}]})}})


def test_user_pause_button_can_resume_dynamic_loop(dynamic,client,monkeypatch):
    db,_=dynamic;client.cookies.set('finagent_demo_session','s1')
    seen=sequence(monkeypatch,calls(('list_accounts',{}),('manage_task',{'action':'pause','instruction_quote':'暂停任务'})),reply('账户已查询，可以继续讨论你的目标。'))
    result=create(db,session(db),'live','查询账户，然后暂停任务')
    assert result['status']=='paused' and len(seen)==1
    result=client.post('/api/tasks/'+result['id']+'/control',headers=headers(),json={'action':'resume'})
    assert result.status_code==200,result.text
    assert result.json()['data']['status']=='completed' and len(seen)==2


def test_cancelled_operation_returns_cancelled_observation_without_payment(dynamic,client,monkeypatch):
    db,_=dynamic;client.cookies.set('finagent_demo_session','s1')
    def cancelled(messages,context):
        assert json.loads(messages[-1]['content'])['data']['status']=='cancelled'
        return reply('本单已取消，可以继续查询账户。')
    seen=sequence(monkeypatch,calls(('prepare_transfer',{'source':'活期账户','target':'储蓄账户','amount_yuan':'100'})),cancelled)
    result=create(db,session(db),'live','从活期账户转100元到储蓄账户');a=result['authorization_operations'][0]
    response=client.post(a['authorization_path']+'/cancel',headers=headers(),json={'action_id':a['action_id']})
    assert response.status_code==200,response.text
    assert len(seen)==2 and transfers(db)==0 and state(db,result['id'])['wait'] is None


def test_funding_cannot_treat_one_month_expense_as_multimonth_reserve():
    r={'evidence':[],'facts':[{'id':'f1','node':'s1','key':'available_liquid','value':'46023.24','unit':'yuan','label':'可用活期','source_ref':'ok'},
        {'id':'f2','node':'s1','key':'monthly_expense_mean','value':'7747.83','unit':'yuan','label':'月消费','source_ref':'ok'}],'user_messages':['预算12000元']}
    calculation={'operation':'funding_check','inputs':[{'node':'s1','key':'available_liquid'},{'node':'s1','key':'monthly_expense_mean'},
        {'value':'12000','unit':'yuan','label':'目标成本','quote':'预算12000元'}]}
    with pytest.raises(DomainError,match='单月消费'):runtime.calculate(r,{'arguments':{'title':'压力','calculation':calculation}})


def test_unknown_target_cost_cannot_be_disguised_as_zero():
    r={'evidence':[],'facts':[],'user_messages':[]}
    c={'operation':'funding_check','inputs':[{'node':'s1','key':'available_liquid'},
        {'node':'s2','key':'result'},{'value':'0','unit':'yuan','label':'目标成本（未定）'}]}
    r['facts']=[{'node':'s1','key':'available_liquid','source_ref':'a','value':'1000','unit':'yuan','label':'活期'},
        {'node':'s2','key':'result','source_ref':'b','value':'300','unit':'yuan','label':'预留','metric':'reserve'}]
    with pytest.raises(DomainError,match='零占位'):runtime.calculate(r,{'arguments':{'title':'目标','calculation':c}})


def test_subscription_analysis_cannot_get_stuck_on_rejected_write(dynamic,monkeypatch):
    db,_=dynamic
    seen=sequence(monkeypatch,calls(('read_subscriptions',{})),
        calls(('prepare_subscription_reminder',{'merchant':'云音乐','days_before':2,'hour':9,'minute':0})),
        reply('本次只分析；订阅是否保留还需要了解实际使用频率。'))
    result=create(db,session(db),'live','分析订阅，只分析，不取消，也不设置提醒')
    pending=result['next_actions'][0]['pending_id']
    result=confirm_subscription_query(db,session(db),result['id'],pending)
    assert len(seen)==3 and state(db,result['id'])['wait'] is None
    assert not result['authorization_operations'] and result['status']=='completed' and transfers(db)==0


def test_incomplete_month_cannot_be_used_in_calculations():
    r={'facts':[{'id':'f1','node':'s1','key':'current.totals.net_minor','label':'已记录消费','value':'634.07','unit':'yuan',
        'source_ref':'a','data_complete':False}],'evidence':[],'user_messages':[]}
    c={'operation':'sum','inputs':[{'node':'s1','key':'current.totals.net_minor'}]}
    with pytest.raises(DomainError,match='数据有缺口'):runtime.calculate(r,{'arguments':{'title':'完整月消费','calculation':c}})


def test_queried_month_names_are_allowed_but_incomplete_trend_is_not():
    r={'facts':[{'id':'f1','node':'s1','key':'current.totals.net_minor','label':'已记录消费','value':'634.07','unit':'yuan',
        'source_ref':'a','data_complete':False}],
        'evidence':[{'tool':'summarize_spending','source_ref':'a','query':{'start':'2026-08-01','compare_start':'2026-07-01'}}]}
    text,_=runtime.render_response(protocol.Respond(text='8月已记录{{f1}}；7月完整，不能判断实际下降。',evidence_refs=['a']),r)
    assert '8月' in text and '634.07 元' in text
    with pytest.raises(DomainError,match='真实增减'):runtime.render_response(protocol.Respond(text='8月整体低于7月。',evidence_refs=['a']),r)


def test_resume_after_receipt_before_background_does_not_execute_again(dynamic,client,monkeypatch):
    db,_=dynamic;client.cookies.set('finagent_demo_session','s1')
    seen=sequence(monkeypatch,calls(('prepare_transfer',{'source':'活期账户','target':'储蓄账户','amount_yuan':'100'})),reply('原业务回执已恢复，继续后续分析。'))
    result=create(db,session(db),'live','从活期账户转100元到储蓄账户');a=result['authorization_operations'][0]
    if a['level']=='red':mfa(db,a['resource_id'])
    token=authorization.issue_token(db,session(db),result['id'],'proposal',a['resource_id'],a['action_id'])['confirmation_token']
    authorization.confirm(db,session(db),result['id'],'proposal',a['resource_id'],a['action_id'],token)
    assert transfers(db)==1 and len(seen)==1
    change(db,db.get(AgentTask,result['id']),'pause')
    resumed=client.post('/api/tasks/'+result['id']+'/control',headers=headers(),json={'action':'resume'})
    assert resumed.status_code==200,resumed.text
    assert transfers(db)==1 and len(seen)==2 and state(db,result['id'])['wait'] is None


def test_pause_during_model_request_discards_late_result_and_resumes(dynamic,client,monkeypatch):
    db,_=dynamic;client.cookies.set('finagent_demo_session','s1')
    def paused(messages,context):
        change(db,db.get(AgentTask,context['dialogue_context']['task']['id']),'pause')
        return reply('这条迟到回答不能被应用。')
    seen=sequence(monkeypatch,calls(('list_accounts',{})),paused,reply('已从暂停点继续，账户信息已核对。'))
    result=create(db,session(db),'live','查询账户')
    assert result['status']=='paused' and not any('迟到回答' in m['content'] for m in result['messages'])
    resumed=client.post('/api/tasks/'+result['id']+'/control',headers=headers(),json={'action':'resume'})
    assert resumed.status_code==200,resumed.text
    assert resumed.json()['data']['status']=='completed' and len(seen)==3 and transfers(db)==0
