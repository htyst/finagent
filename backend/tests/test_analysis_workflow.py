import json
from decimal import Decimal
import pytest
from sqlalchemy import select,func
from app.agent import analysis_workflow as w, language
from app.agent.dialogue_schema import AnalysisWorkflow,AnalysisNode,DialogueTurn,Goal
from app.agent.service import create,send
from app.agent.conversation_reads import confirm_subscription_query
from app.data.models import DemoSession,AgentTask,TransferOrder,Account
from app.domain.errors import DomainError


def node(id,name,depends_on=None,args=None):
    return AnalysisNode(id=id,title=id,name=name,depends_on=depends_on or [],arguments_json=json.dumps(args or {}))


def plan(*nodes):return AnalysisWorkflow(objective='依据用户经济状况比较目标可行性',nodes=list(nodes))
def decision(p):return language.Understanding(intent='wealth_guidance',dialogue=DialogueTurn(behavior='knowledge',workflow=p,goals=[Goal(text='结合财务状况规划目标',kind='read')]))
def valid():return plan(node('basis','financial_planning_basis'),node('answer','compose_answer',['basis']))


@pytest.mark.parametrize('p',[
    plan(node('a','list_accounts',['b']),node('b','compose_answer',['a'])),
    plan(node('a','list_accounts'),node('a','compose_answer',['a'])),
    plan(node('a','prepare_transfer'),node('b','compose_answer',['a'])),
    plan(node('a','list_accounts'),node('b','list_cards'),node('c','compose_answer',['a'])),
    plan(node('a','list_accounts',args={'owner_id':'u2'}),node('b','compose_answer',['a'])),
    plan(node('a','calculate',args={'operation':'eval','inputs':[{'value':'1','unit':'number','label':'x'}]}),node('b','compose_answer',['a'])),
])
def test_reject_whole_graph_before_any_execution(p):
    with pytest.raises(DomainError):w.validate(p)


def test_dag_uses_domain_and_synthesis_without_writes(bank,monkeypatch):
    db,_=bank;monkeypatch.setattr(language,'interpret',lambda payload:decision(valid()))
    seen=[]
    def compose(payload):
        seen.append(payload);f=next(f for f in payload['facts'] if f['key']=='available_liquid')
        return w.Answer(answer_template='先核对可用资金，保留必要生活费。请补充目标预算与近期必付款。',evidence_refs=[f['source_ref']])
    monkeypatch.setattr(w,'compose',compose)
    state=create(db,db.get(DemoSession,'s1'),'live','结合我的经济情况规划换工作的缓冲期')
    assert state['analysis_workflow']['status']=='completed'
    assert all(n['status']=='succeeded' for n in state['analysis_workflow']['nodes'])
    assert '当前可用活期' in state['messages'][-1]['content'] and len(seen)==1
    assert state['proposal'] is None and not db.scalar(select(func.count()).select_from(TransferOrder))
    assert seen[0]['dialogue_context']['messages'][-1]['content']=='结合我的经济情况规划换工作的缓冲期'


def test_subscription_checkpoint_resume_idempotent_and_owner_bound(bank,monkeypatch):
    db,_=bank;p=plan(node('basis','financial_planning_basis'),node('subs','read_subscriptions'),node('answer','compose_answer',['basis','subs']))
    monkeypatch.setattr(language,'interpret',lambda payload:decision(p));calls=[]
    def compose(payload):
        calls.append(1);f=payload['facts'][0]
        return w.Answer(answer_template='可依据已查数据选择压缩订阅，需由你选择保留项目。',evidence_refs=[f['source_ref']])
    monkeypatch.setattr(w,'compose',compose)
    s=db.get(DemoSession,'s1');state=create(db,s,'live','分析我的订阅与消费习惯，找出节省空间')
    assert state['analysis_workflow']['status']=='awaiting_confirmation' and not calls
    pending=state['next_actions'][0]['pending_id']
    with pytest.raises(DomainError):confirm_subscription_query(db,db.get(DemoSession,'other'),state['id'],pending)
    state=confirm_subscription_query(db,s,state['id'],pending)
    assert state['analysis_workflow']['status']=='completed' and calls==[1]
    repeated=confirm_subscription_query(db,s,state['id'],pending)
    assert repeated['analysis_workflow']['status']=='completed' and calls==[1]
    assert len([x for x in state['steps'] if x['tool_name']=='financial_planning_basis'])==2
    assert state['proposal'] is None


def test_changed_question_revokes_old_checkpoint(bank,monkeypatch):
    db,_=bank;s=db.get(DemoSession,'s1');p=plan(node('subs','read_subscriptions'),node('answer','compose_answer',['subs']))
    monkeypatch.setattr(language,'interpret',lambda payload:decision(p))
    state=create(db,s,'live','查看订阅节省空间');pending=state['next_actions'][0]['pending_id']
    monkeypatch.setattr(language,'interpret',lambda payload:language.Understanding(intent='reply',reply='你好'))
    send(db,s,state['id'],'换个话题，介绍一下功能')
    with pytest.raises(DomainError):confirm_subscription_query(db,s,state['id'],pending)


def test_missing_or_incomplete_months_do_not_become_zero_spend(bank):
    from app.domain.planning_basis import read
    from app.data.models import DataCoverage
    db,_=bank;db.query(DataCoverage).filter(DataCoverage.owner_id=='u1').delete();db.flush();data=read(db,'u1',3)
    assert data['coverage']['status']=='incomplete'
    if data['coverage']['complete_months']==0:
        assert not any(f['key']=='monthly_expense_mean' for f in data['facts'])
    a=db.scalar(select(Account).where(Account.owner_id=='u1'));old=a.balance_minor;a.status='frozen'
    frozen=read(db,'u1',3)
    before=Decimal(next(f['value'] for f in data['facts'] if f['key']=='available_liquid'))
    after=Decimal(next(f['value'] for f in frozen['facts'] if f['key']=='available_liquid'))
    assert before-after==Decimal(old)/100


def calc(op,inputs,deps=None):return {'title':'目标余量','arguments':{'operation':op,'inputs':inputs},'depends_on':deps or []}


def test_calculator_units_provenance_and_explicit_assumptions():
    facts=[{'node':'basis','key':'available_liquid','value':'56155.01','unit':'yuan','source_ref':'test'}]
    result=w.calculate(calc('subtract',[{'node':'basis','key':'available_liquid'},{'value':'12000','unit':'yuan','label':'目标成本','quote':'预算12000元'}]),facts,['预算12000元'])
    assert result['value']=='44155.01' and result['assumptions'][0]['source']=='用户明确参数'
    result=w.calculate(calc('multiply',[{'node':'basis','key':'available_liquid'},{'value':'0.5','unit':'number','label':'示意比例'}]),facts,[])
    assert result['value']=='28077.51' and '尚未' in result['assumptions'][0]['source']
    for c in [calc('divide',[{'value':'1','unit':'yuan','label':'x'},{'value':'0','unit':'number','label':'zero'}]),
        calc('multiply',[{'value':'2','unit':'yuan','label':'x'},{'value':'3','unit':'yuan','label':'y'}]),
        calc('sum',[{'node':'missing','key':'result'}]),
        calc('sum',[{'value':'10000','unit':'yuan','label':'用户预算','quote':'预算10000'}])]:
        with pytest.raises(DomainError):w.calculate(c,facts,['预算20000'])


def test_synthesis_rejects_fabricated_money_refs_and_completion():
    runtime={'facts':[{'id':'f1','label':'可用活期','value':'100','unit':'yuan','source_ref':'ok'}],'evidence':[{'source_ref':'ok'}]}
    assert '核对生活费' in w.render(w.Answer(answer_template='还需核对生活费。',evidence_refs=['ok']),runtime)
    for text,refs in [('预算10000元',['ok']),('{{f99}}',['ok']),('{{f1}}',['other']),('{{f1}}已转账',['ok']),('{{f1}}够花十万元',['ok'])]:
        with pytest.raises(DomainError):w.render(w.Answer(answer_template=text,evidence_refs=refs),runtime)


def test_synthesis_failure_preserves_facts(bank,monkeypatch):
    db,_=bank;monkeypatch.setattr(language,'interpret',lambda payload:decision(valid()))
    def unavailable(payload):raise DomainError('MODEL_UNAVAILABLE','连接失败',503)
    monkeypatch.setattr(w,'compose',unavailable)
    state=create(db,db.get(DemoSession,'s1'),'live','根据我的存款规划大额采购')
    assert state['analysis_workflow']['status']=='partially_completed'
    assert '当前可用活期' in state['messages'][-1]['content'] and '连接失败' in state['messages'][-1]['content']


def test_late_synthesis_cannot_apply_after_plan_changed(bank,monkeypatch):
    db,_=bank;monkeypatch.setattr(language,'interpret',lambda payload:decision(valid()))
    def changed(payload):
        task=db.scalar(select(AgentTask).order_by(AgentTask.created_at_utc.desc()));task.plan_version+=1;db.commit()
        f=payload['facts'][0];return w.Answer(answer_template='过时分析',evidence_refs=[f['source_ref']])
    monkeypatch.setattr(w,'compose',changed)
    state=create(db,db.get(DemoSession,'s1'),'live','依据存款判断预算')
    assert '过时分析' not in state['messages'][-1]['content'] and state['proposal'] is None


def test_write_goal_cannot_enter_analysis():
    d=decision(valid());d.dialogue.goals.append(Goal(text='顺便转账',kind='write'))
    with pytest.raises(DomainError):w.validate(d.dialogue.workflow,d)


def test_funding_check_direction_and_user_chinese_quantities():
    result=w.calculate(calc('funding_check',[{'value':'46023.24','unit':'yuan','label':'现金'},
        {'value':'23243.49','unit':'yuan','label':'预留'}, {'value':'12000','unit':'yuan','label':'预算','quote':'预算12000元'}]),[],['预算12000元'])
    assert result['value']=='10779.75' and result['verdict']=='positive_buffer'
    runtime={'facts':[dict(result,id='f1',node='x',source_ref='ok')],'evidence':[{'source_ref':'ok'}]}
    with pytest.raises(DomainError):w.render(w.Answer(answer_template='{{f1}}，存在资金缺口。',evidence_refs=['ok']),runtime)
    c=w.calculate(calc('multiply',[{'value':'2','unit':'number','label':'人数','quote':'两个人'},{'value':'7','unit':'number','label':'天数','quote':'七天'}]),[],['两个人七天'])
    assert c['value']=='14.00' and all(a['source']=='用户明确参数' for a in c['assumptions'])


@pytest.mark.parametrize('value,label',[('7000','人数乘天数'),('0','未知预算占位'),('14','两人7天')])
def test_ungrounded_derived_or_placeholder_constants_rejected(value,label):
    p=plan(node('basis','financial_planning_basis'),node('c','calculate',args={'operation':'sum','inputs':[{'value':value,'unit':'number','label':label}]}),node('answer','compose_answer',['basis','c']))
    with pytest.raises(DomainError):w.validate(p)


def test_income_shock_removes_lost_income_not_fraction_of_surplus():
    facts=[{'node':'basis','key':'monthly_income_mean','label':'收入','value':'18000','unit':'yuan','source_ref':'ok'},
        {'node':'basis','key':'monthly_cashflow_mean','label':'净现金流','value':'5401.31','unit':'yuan','source_ref':'ok'}]
    result=w.calculate(calc('income_shock',[{'node':'basis','key':'monthly_income_mean'},{'node':'basis','key':'monthly_cashflow_mean'},{'value':'0.8','unit':'number','label':'情景收入保留比例'}]),facts,[])
    assert result['value']=='1801.31' and result['metric']=='stressed_cashflow'
    with pytest.raises(DomainError):w.calculate(calc('income_shock',[{'node':'basis','key':'monthly_cashflow_mean'},{'node':'basis','key':'monthly_income_mean'},{'value':'0.8','unit':'number','label':'情景收入保留比例'}]),facts,[])


def test_verified_chinese_months_allowed_but_unverified_money_rejected():
    runtime={'facts':[{'id':'f1','key':'parameter_0','node':'a','label':'目标月数','value':'6','unit':'number','source_ref':'ok'}],'evidence':[{'source_ref':'ok'}]}
    assert '六个月' in w.render(w.Answer(answer_template='六个月内可先比较目标成本与储蓄情景。',evidence_refs=['ok']),runtime)
    with pytest.raises(DomainError):w.render(w.Answer(answer_template='六个月能保证赚十万元。',evidence_refs=['ok']),runtime)


def test_read_plan_title_cannot_claim_execution():
    p=valid();p.nodes[0].title='已转账成功'
    with pytest.raises(DomainError):w.validate(p)


def test_public_numbered_questions_are_not_rejected_as_financial_claims():
    from app.agent.dialogue import safe_explanation
    d=language.Understanding(intent='reply',reply='可以先列预算。\n1. 几个人？\n2. 预计几天？')
    assert '• 几个人' in safe_explanation(d,[])


def test_zero_income_condition_is_a_supported_scenario():
    facts=[{'node':'basis','key':'monthly_income_mean','label':'月均收入','value':'3000','unit':'yuan','source_ref':'ok'},
        {'node':'basis','key':'monthly_cashflow_mean','label':'净现金流','value':'500','unit':'yuan','source_ref':'ok'}]
    result=w.calculate(calc('income_shock',[{'node':'basis','key':'monthly_income_mean'},{'node':'basis','key':'monthly_cashflow_mean'},
        {'value':'0','unit':'number','label':'收入保留比例','quote':'不要默认还有工资'}]),facts,['不要默认还有工资'])
    assert result['value']=='-2500.00'


def test_unknown_purchase_cost_cannot_be_declared_affordable():
    runtime={'facts':[],'evidence':[{'source_ref':'ok'}]}
    with pytest.raises(DomainError):w.render(w.Answer(answer_template='买车本身不至于把现金耗尽。',evidence_refs=['ok']),runtime)


def test_decision_facts_prioritize_income_shock_and_savings_over_reserves():
    facts=[{'key':'result','metric':m,'value':'1','unit':'yuan','label':m,'source_ref':m} for m in ('reserve','runway_months','projected_saving','stressed_cashflow')]
    chosen=w.focused_facts(facts)
    assert [f['metric'] for f in chosen[:3]]==['stressed_cashflow','projected_saving','runway_months']


def test_retrieved_knowledge_citations_are_valid_but_invented_ones_are_not():
    runtime={'facts':[],'evidence':[{'tool':'search_knowledge','source_ref':'tool:kb','data':{'items':[{'source_ref':'kb:financial-guidance'}]}}]}
    assert w.render(w.Answer(answer_template='先保留应急资金，再考虑目标成本。',evidence_refs=['kb:financial-guidance']),runtime)
    with pytest.raises(DomainError):w.render(w.Answer(answer_template='先保留应急资金。',evidence_refs=['kb:invented']),runtime)
