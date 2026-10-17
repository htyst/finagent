import json
from decimal import Decimal
import pytest
from pydantic import ValidationError
from app.agent import dynamic_protocol as protocol,dynamic_runtime as runtime,planning_reply
from app.agent.service import create
from app.domain.planning_basis import read
from app.domain.errors import DomainError
from app.data.models import DataCoverage
from test_dynamic_tools import dynamic,sequence,calls,session,reply


def sample():
    facts=[dict(id='f1',node='s1',tool='financial_planning_basis',key='available_liquid',label='当前可用活期',value='10000',unit='yuan',source_ref='basis'),
        dict(id='f2',node='s1',tool='financial_planning_basis',key='monthly_expense_mean',label='月均净消费',value='3000',unit='yuan',source_ref='basis'),
        dict(id='f3',node='s2',tool='calculate',key='result',label='生活费预留',value='9000',unit='yuan',source_ref='calc',metric='reserve',
            assumptions=[dict(label='预留月数',value='3',unit='number',source='情景假设，尚未得到用户确认')]),
        dict(id='f4',node='s2',tool='calculate',key='parameter_0',label='预留月数',value='3',unit='number',source_ref='calc')]
    return dict(facts=facts,evidence=[dict(tool='financial_planning_basis',source_ref='basis',data=dict(period_start='2026-06-01',period_end='2026-08-31',snapshot_as_of='2026-09-24',coverage={'status':'complete'})),
        dict(tool='calculate',source_ref='calc',data={})])


def synthesis(**changes):
    return planning_reply.Synthesis.model_validate(dict(goal='旅行资金规划',conclusion='需要先核对旅行总成本，不能仅凭存款判断预算足够。',
        reasons=[dict(text='可用活期{{f1}}，应先为日常生活保留缓冲。',fact_ids=['f1'])],
        scenarios=[dict(text='假设按三个月生活费预留，需要{{f3}}；旅行成本尚未提供。',fact_ids=['f3'])],questions=['旅行总预算是多少？'],**changes))


def test_concise_synthesis_keeps_provenance_and_assumptions():
    r=sample();text,refs,chosen=planning_reply.response_text(synthesis(),r)
    response=protocol.Respond(text=text,evidence_refs=refs)
    rendered,_=runtime.render_response(response,r)
    assert '10,000.00 元' in rendered and '9,000.00 元' in rendered
    assert refs==['basis','calc'] and chosen=={'f1','f3'}
    footer=planning_reply.notes(r,chosen)
    assert '假设，待确认' in footer and '2026-09-24' in footer and '核算依据' not in footer


@pytest.mark.parametrize('bad_id',['f99','old:f1'])
def test_reason_rejects_non_current_fact(bad_id):
    answer=synthesis().model_copy(update={'reasons':[planning_reply.Point(text='当前资产足够。',fact_ids=[bad_id])]})
    with pytest.raises(DomainError):planning_reply.response_text(answer,sample())


def test_scenario_requires_real_calculation():
    answer=synthesis().model_copy(update={'scenarios':[planning_reply.Point(text='旅行可以按这笔余额安排。',fact_ids=['f1'])]})
    with pytest.raises(DomainError,match='calculate'):planning_reply.response_text(answer,sample())


def test_conclusion_cannot_smuggle_a_fact_not_returned_by_tools():
    answer=synthesis().model_copy(update={'conclusion':'建议按{{f999}}安排。'})
    with pytest.raises(DomainError):planning_reply.response_text(answer,sample())


def test_questions_and_scenarios_are_bounded():
    raw=synthesis().model_dump();raw['questions']=['问一个条件？']*3
    with pytest.raises(ValidationError):planning_reply.Synthesis.model_validate(raw)
    raw=synthesis().model_dump();raw['scenarios']=raw['scenarios']*3
    with pytest.raises(ValidationError):planning_reply.Synthesis.model_validate(raw)
    with pytest.raises(ValidationError):protocol.Respond()
    assert protocol.Respond(text='你好').synthesis is None


def test_only_selected_scenario_assumptions_are_shown():
    r=sample();assert '情景参数' not in planning_reply.notes(r,{'f1'})
    r['facts'][2]['assumptions'][0]['source']='用户明确参数'
    assert '用户提供' in planning_reply.notes(r,{'f3'})


def test_incomplete_coverage_remains_visible_without_invented_means(bank):
    db,_=bank;db.query(DataCoverage).filter(DataCoverage.owner_id=='u1').delete();db.flush()
    basis=read(db,'u1');r=sample();r['evidence'][0]['data']=basis
    assert basis['coverage']['status']=='incomplete'
    assert '部分月份缺失' in planning_reply.notes(r,set())
    assert basis['decision_context']['liabilities']['future_due_amount'] is None


def test_planning_basis_separates_spending_cashflow_and_liquidity(bank):
    db,_=bank;data=read(db,'u1');context=data['decision_context']
    assert context['assets']['budget_base']=='available_liquid_only'
    assert not context['assets']['non_cash_assets_are_spendable']
    assert not context['spending']['necessity_verified']
    assert not context['cashflow']['future_income_guaranteed']
    assert not context['goal']['unknown_cost_is_zero']
    assert 'data' not in context  # No hidden query of protected subscriptions.
    complete=[m for m in data['months'] if m['coverage']=='complete']
    if complete:
        facts={f['key']:f for f in data['facts']}
        expected=sum(Decimal(m['outflow_minor']) for m in complete)/len(complete)/100
        assert abs(Decimal(facts['monthly_cash_outflow_mean']['value'])-expected)<Decimal('.01')


def test_live_loop_renders_synthesis_without_duplicate_archive_dump(dynamic,monkeypatch):
    db,_=dynamic
    def answer(messages,context):
        obs=json.loads(messages[-1]['content']);amount=next(f for f in obs['facts'] if f['key']=='available_liquid')
        return calls(('respond',{'evidence_refs':['untrusted-old-source'],'synthesis':dict(goal='旅行',conclusion='先核对总成本和预留要求，再判断安排。',
            reasons=[dict(text='当前可用活期{{'+amount['id']+'}}，需要与目标成本及生活预留分开考虑。',fact_ids=[amount['id']])],
            questions=['这次旅行计划花多少？']), 'needs_input':True}))
    sequence(monkeypatch,calls(('financial_planning_basis',{})),answer)
    result=create(db,session(db),'live','依据我的存款和消费习惯给旅行预算建议')
    text=result['messages'][-1]['content']
    assert result['status']=='gathering_info' and '元' in text and '快照截至' in text
    assert '核算依据' not in text and '测算参数' not in text and '月均净消费：' not in text
    assert len(text)<400 and not result['authorization_operations']


def test_synthesis_cannot_substitute_for_personal_query(dynamic,monkeypatch):
    db,_=dynamic
    sequence(monkeypatch,calls(('respond',{'synthesis':synthesis().model_dump()})),reply('尚需查询个人财务数据再分析。'))
    result=create(db,session(db),'live','给我个人旅行建议')
    assert runtime.get(runtime.load(db,session(db),result['id'])[1])['nodes'][0]['error_code']=='ANALYSIS_REFERENCE'


@pytest.mark.parametrize('text',['不能仅凭存款判断预算足够。','这不代表预算充足。','尚无法确认负担得起。'])
def test_qualified_uncertainty_is_not_mistaken_for_a_positive_verdict(text):
    from app.agent.analysis_workflow import unsupported_affordability_claim
    assert not unsupported_affordability_claim(text)


@pytest.mark.parametrize('text',['预算足够。','不能判断，但预算足够。','无法确认预算充足；实际上负担得起。'])
def test_negation_cannot_hide_a_separate_unsupported_promise(text):
    from app.agent.analysis_workflow import unsupported_affordability_claim
    assert unsupported_affordability_claim(text)


@pytest.mark.parametrize('text',['旅行当月几乎不留额外缓冲。','如果收入中断，18000元就只能从三个月预留里挪用。'])
def test_positive_funding_cannot_be_explained_as_no_buffer_or_unpaid_goal(text):
    f=dict(metric='funding_buffer',verdict='positive_buffer',value='4979.75')
    with pytest.raises(DomainError):planning_reply.validate_judgment(text,[f])
    planning_reply.validate_judgment('当前活期支付目标成本并保留生活费后还有额外余量；其他未来应付款尚待核对。',[f])


def goal_runtime(cash='46223.24',expense='7747.83'):
    return {'evidence':[],'facts':[
        dict(id='f1',node='s1',key='available_liquid',label='可用活期',value=cash,unit='yuan',source_ref='basis'),
        dict(id='f2',node='s1',key='monthly_expense_mean',label='月均净消费',value=expense,unit='yuan',source_ref='basis')],
        'user_messages':['旅游预算18000元，保留三个月生活费']}


def test_goal_budget_keeps_reserve_and_target_separate_without_future_income():
    r=goal_runtime();inputs=[dict(node='s1',key='available_liquid'),dict(node='s1',key='monthly_expense_mean'),
        dict(value='3',unit='number',label='预留月数',quote='保留三个月生活费'),dict(value='18000',unit='yuan',label='旅游预算',quote='旅游预算18000元')]
    result=runtime.calculate(r,{'arguments':{'title':'旅游预算测算','calculation':dict(operation='goal_budget',inputs=inputs)}})
    assert result['value']=='4979.75' and result['verdict']=='positive_buffer'
    assert result['operand_values']==['46223.24','23243.49','18000']
    assert result['supporting_facts'][0]['value']=='23243.49'
    assert '未计未来收入' in result['funding_scope']


def test_income_gap_buffer_does_not_require_inventing_zero_target_cost():
    r=goal_runtime(cash='280',expense='6000')
    result=runtime.calculate(r,{'arguments':{'title':'缓冲测算','calculation':dict(operation='emergency_check',inputs=[
        dict(node='s1',key='available_liquid'),dict(node='s1',key='monthly_expense_mean'),dict(value='2',unit='number',label='压力期月数')])}})
    assert result['value']=='-11720.00' and result['metric']=='emergency_buffer'
    assert result['supporting_facts'][0]['value']=='12000.00'
    assert result['assumptions'][0]['source'].startswith('情景假设')


@pytest.mark.parametrize('first_key,cost',[('mixed_net_worth','18000'),('available_liquid','0')])
def test_scenario_rejects_non_liquid_wealth_or_unknown_cost_placeholder(first_key,cost):
    r=goal_runtime();r['facts'].append(dict(r['facts'][0],id='f3',key='mixed_net_worth'))
    c=dict(operation='goal_budget',inputs=[dict(node='s1',key=first_key),dict(node='s1',key='monthly_expense_mean'),
        dict(value='3',unit='number',label='预留月数'),dict(value=cost,unit='yuan',label='目标预算')])
    with pytest.raises(DomainError):runtime.calculate(r,{'arguments':dict(title='预算测算',calculation=c)})


def test_monthly_saving_is_not_a_new_expense_against_existing_cash():
    r=goal_runtime();r['user_messages']=['我想每月固定存3000元']
    r['facts'].append(dict(id='f3',node='s1',key='monthly_cashflow_mean',label='月均净现金流',value='227.39',unit='yuan',source_ref='basis'))
    target=dict(value='3000',unit='yuan',label='月储蓄目标',quote='每月固定存3000元')
    result=runtime.calculate(r,{'arguments':dict(title='储蓄能力',calculation=dict(operation='savings_capacity',inputs=[dict(node='s1',key='monthly_cashflow_mean'),target]))})
    assert result['value']=='-2772.61' and result['metric']=='monthly_saving_margin'
    with pytest.raises(DomainError,match='储蓄'):
        runtime.calculate(r,{'arguments':dict(title='误用储蓄',calculation=dict(operation='goal_budget',inputs=[
            dict(node='s1',key='available_liquid'),dict(node='s1',key='monthly_expense_mean'),dict(value='3',unit='number',label='预留月数'),target]))})


def test_business_and_household_consumption_is_not_labelled_pure_living_cost():
    r=goal_runtime();r['facts'][1]['expense_scope']='business_and_household'
    result=runtime.calculate(r,{'arguments':dict(title='合计支出缓冲',calculation=dict(operation='emergency_check',inputs=[
        dict(node='s1',key='available_liquid'),dict(node='s1',key='monthly_expense_mean'),dict(value='3',unit='number',label='预留月数')]))})
    assert '含经营' in result['label'] and '含经营' in result['supporting_facts'][0]['label']


@pytest.mark.parametrize('amount,accepted',[('300000',True),('30',False)])
def test_wan_currency_quote_preserves_scale_instead_of_accepting_component(amount,accepted):
    r=goal_runtime();r['user_messages']=['装修预算30万元，保留三个月生活费']
    args=dict(title='装修测算',calculation=dict(operation='goal_budget',inputs=[
        dict(node='s1',key='available_liquid'),dict(node='s1',key='monthly_expense_mean'),
        dict(value='3',unit='number',label='预留月数',quote='保留三个月生活费'),
        dict(value=amount,unit='yuan',label='装修费用',quote='装修预算30万元')]))
    if not accepted:
        with pytest.raises(DomainError):runtime.calculate(r,{'arguments':args})
    else:
        result=runtime.calculate(r,{'arguments':args})
        assert result['value']=='-277020.25'
        assert next(f['value'] for f in result['supporting_facts'] if f['key']=='gap_required')=='277020.25'
