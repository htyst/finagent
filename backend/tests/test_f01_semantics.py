"""F01 deterministic arithmetic and safety gates; every DB is isolated."""
import json
from copy import deepcopy
from datetime import date,datetime,timezone
from decimal import Decimal
from pathlib import Path
import pytest
from sqlalchemy import select,func
from app.agent import dynamic_runtime as runtime,analysis_workflow as arithmetic,planning_reply
from app.agent import planning_assumptions
from app.domain import planning_obligations,planning_basis
from app.domain.errors import DomainError
from app.data.models import Account,DataCoverage,DemoSetting,Proposal,TransferOrder
from app.data.financial_models import FinancialDatasetRow
from app.domain.ledger import post_entry,post_refund
from app.agent.service import create,send
from test_dynamic_tools import dynamic,sequence,calls,reply,session,state


def rstate(cash='10000',expense='1000',direct='600',dues='2000'):
    rows=[('available_liquid',cash),('monthly_expense_mean',expense),
          ('monthly_direct_cash_expense_mean',direct),('upcoming_due_known',dues),
          ('monthly_income_mean','10000'),('monthly_cashflow_mean','3000')]
    return dict(id='TEST-ONLY-F01',evidence=[],nodes=[],facts=[dict(id='f'+str(i+1),node='basis',tool='financial_planning_basis',key=k,value=v,unit='yuan',label=k,source_ref='basis') for i,(k,v) in enumerate(rows)],
        user_messages=['预算800元，保留一个月'],request='只分析预算')


def ref(key):return dict(node='basis',key=key)
def const(value,label='目标预算',unit='yuan',quote=None):
    return dict(value=str(value),unit=unit,label=label,**({'quote':quote} if quote else {}))
def calc(r,op,inputs):
    return runtime.calculate(r,dict(arguments=dict(title='情景核算',calculation=dict(operation=op,inputs=inputs))))
def archive(db,table,key,row,owner='u1'):
    version=db.get(DemoSetting,1).dataset_version
    db.add(FinancialDatasetRow(dataset_version=version,table_name=table,row_key=key,owner_id=owner,payload_json=json.dumps(row)))
    db.flush()


def test_unpaid_installment_not_entire_debt_stock(bank):
    db,_=bank
    archive(db,'users','u1',{'id':'u1'})
    archive(db,'liabilities','loan',dict(liability_id='loan',liability_type='loan',currency='CNY',status='active',
        next_due_date='2026-10-16',next_principal_minor='10000',next_interest_minor='2000',outstanding_balance_minor='9000000'))
    result=planning_obligations.read(db,'u1',date(2026,9,24))
    assert result['known_unpaid_minor']==12000
    assert result['items'][0]['state']=='scheduled_snapshot'
    assert planning_obligations.read(db,'u2',date(2026,9,24))['known_unpaid_minor']==0
    basis=planning_basis.read(db,'u1')
    assert next(f['value'] for f in basis['facts'] if f['key']=='upcoming_due_known')=='120'
    assert basis['decision_context']['liabilities']['future_due_amount']==12000


@pytest.mark.parametrize('paid,expected',[('0',10000),('10000',0)])
def test_paid_unpaid_cross_month_statement_does_not_double_count(bank,paid,expected):
    db,_=bank;txid=None
    if int(paid):
        txid=post_entry(db,owner_id='u1',account_id='a1c',kind='credit_card_repayment',direction='debit',
            amount_minor=int(paid),business_at=datetime(2026,9,20,tzinfo=timezone.utc),business_id='f01-payment').id
    archive(db,'liabilities','credit1',dict(liability_id='credit1',liability_type='credit_card',currency='CNY',outstanding_balance_minor='10000'))
    for month in ('08','09'):
        archive(db,'credit_card_statements','statement-'+month,dict(statement_id='statement-'+month,
            liability_id='credit1',card_id='cc1',statement_date='2026-'+month+'-10',due_date='2026-'+month+'-28',
            statement_balance_minor='10000',payment_minor=paid,payment_transaction_id=txid,status='paid' if int(paid) else 'unpaid'))
    result=planning_obligations.read(db,'u1',date(2026,9,24))
    assert result['known_unpaid_minor']==expected and len(result['items'])==1
    assert result['items'][0]['reference']=='statement-09'
    assert not result['items'][0]['add_to_historical_expense']


def test_unverified_payment_and_missing_installment_stay_unknown(bank):
    db,_=bank
    archive(db,'liabilities','loan',dict(liability_id='loan',liability_type='loan',currency='CNY',status='active',
        next_due_date='2026-10-16',next_principal_minor='',next_interest_minor='0',outstanding_balance_minor='500000'))
    basis=planning_basis.read(db,'u1')
    assert basis['decision_context']['liabilities']['future_due_amount'] is None
    assert 'upcoming_due_known' not in {f['key'] for f in basis['facts']}
    assert 'loan_installment_unknown' in basis['upcoming_obligations']['unknown_reasons']


def test_cash_refunds_and_repayment_have_distinct_totals(bank):
    db,_=bank
    kwargs=dict(owner_id='u1',account_id='a1c',business_at=datetime(2024,8,10,tzinfo=timezone.utc))
    purchase=post_entry(db,**kwargs,kind='purchase',direction='debit',amount_minor=10000,business_id='f01-purchase')
    post_refund(db,owner_id='u1',original_id=purchase.id,amount_minor=2000,business_at=datetime(2024,8,11,tzinfo=timezone.utc))
    post_entry(db,**kwargs,kind='credit_card_repayment',direction='debit',amount_minor=3000,business_id='f01-repay')
    result=planning_obligations.cash_expenses(db,'u1','2024-08-10','2024-08-11')
    # An independent ledger selection checks both accounting categories, not model text.
    assert result==dict(direct_non_debt_expense_minor=8000,historical_debt_payment_minor=3000)


@pytest.mark.parametrize('bad_key',['monthly_expense_mean','monthly_cash_outflow_mean','snapshot_debt','mixed_net_worth'])
def test_due_budget_rejects_overlapping_or_non_cash_basis(bad_key):
    r=rstate()
    if bad_key not in {f['key'] for f in r['facts']}:r['facts'].append(dict(r['facts'][0],id='f90',key=bad_key))
    with pytest.raises(DomainError):
        calc(r,'obligation_budget',[ref('available_liquid'),ref(bad_key),const(1,'预留月数','number'),ref('upcoming_due_known')])


def test_due_budget_independently_accounts_for_future_payment():
    r=rstate()
    value=calc(r,'obligation_budget',[ref('available_liquid'),ref('monthly_direct_cash_expense_mean'),const(1,'预留月数','number'),ref('upcoming_due_known'),const(800,quote='预算800元')])
    assert value['value']=='6600.00'  # 10000 - 600 - 2000 - 800
    assert value['metric']=='commitment_buffer' and '未出账' in value['scope_note']
    assert value['supporting_facts'][0]['value']=='600.00'


@pytest.mark.parametrize('change,old,new,quote',[
    ('两万改八千','20000','8000','八千'),
    ('不是两万，是八千','20000','8000','八千'),
    ('预算改成8000元','20000','8000','8000元'),
    ('期限改为两个月','3','2','两个月'),
])
def test_revised_amount_or_horizon_rejects_old_quote(change,old,new,quote):
    from app.agent.analysis_workflow import Operand
    messages=['旅行预算两万，保留三个月',change]
    old_op=Operand(value=old,unit='yuan' if old=='20000' else 'number',label='预算',quote='两万' if old=='20000' else '三个月')
    with pytest.raises(DomainError):planning_assumptions.validate_quote(old_op,messages)
    planning_assumptions.validate_quote(Operand(value=new,unit='yuan' if old=='20000' else 'number',label='预算',quote=quote),messages)


def test_negated_old_amount_in_same_correction_cannot_be_reused():
    r=rstate();r['user_messages']=['预算两万','不是两万，是八千']
    for quote in ('两万','不是两万，是八千'):
        with pytest.raises(DomainError):
            calc(r,'goal_budget',[ref('available_liquid'),ref('monthly_expense_mean'),const(1,'预留月数','number'),const(20000,quote=quote)])


def test_goal_switch_revokes_old_cost_but_not_receipts():
    from app.agent.analysis_workflow import Operand
    with pytest.raises(DomainError):
        planning_assumptions.validate_quote(Operand(value='20000',unit='yuan',label='购车预算',quote='买车两万'),['买车两万','取消买车先还债'])


def test_only_new_budget_is_computed_after_two_wan_to_eight_thousand():
    r=rstate(cash='30000');r['user_messages']=['旅游预算两万，留一个月','两万改八千']
    value=calc(r,'goal_budget',[ref('available_liquid'),ref('monthly_expense_mean'),const(1,'预留月数','number'),const(8000,quote='八千')])
    assert value['value']=='21000.00'


@pytest.mark.parametrize('cost_quote',['1000美元','1000日元','1000 EUR'])
def test_foreign_cost_not_silently_treated_as_yuan(cost_quote):
    r=rstate();r['user_messages']=[cost_quote]
    with pytest.raises(DomainError,match='外币'):
        calc(r,'goal_budget',[ref('available_liquid'),ref('monthly_expense_mean'),const(1,'预留月数','number'),const(1000,quote=cost_quote)])


def test_unknown_travel_price_must_not_be_invented():
    r=rstate();r['user_messages']=['不知道旅行价格，先给建议']
    with pytest.raises(DomainError,match='未知费用'):
        calc(r,'goal_budget',[ref('available_liquid'),ref('monthly_expense_mean'),const(1,'预留月数','number'),const(5000)])
    # A reserve-only scenario is still useful, with a labeled assumption.
    assert calc(r,'emergency_check',[ref('available_liquid'),ref('monthly_expense_mean'),const(1,'预留月数','number')])['value']=='9000.00'


@pytest.mark.parametrize('text',['购物都是可选支出。','餐饮全是浪费。','经营成本全部是生活费。','建议把消费削减30%。'])
def test_unfounded_necessity_or_reduction_judgments_rejected(text):
    s=planning_reply.Synthesis(goal='预算',conclusion=text,reasons=[dict(text='根据当前可用现金判断。',fact_ids=['f1'])])
    with pytest.raises(DomainError):planning_reply.response_text(s,rstate())


def test_action_and_two_questions_are_grounded_and_bounded():
    s=planning_reply.Synthesis(goal='预算',conclusion='应先明确目标成本，再保留缓冲。',
        reasons=[dict(text='当前可用活期{{f1}}。',fact_ids=['f1'])],
        actions=['先选择旅行和储蓄目标的优先级，暂不办理。'],questions=['目标人民币预算是多少？','是否还有未记录的大额应付款？'])
    text,_,_=planning_reply.response_text(s,rstate())
    assert '优先级' in text and text.count('？')==2
    s.questions=['你当前可用余额是多少？']
    with pytest.raises(DomainError):planning_reply.response_text(s,rstate())


def test_model_cannot_prepare_operation_during_advice_only_turn(dynamic,monkeypatch):
    db,_=dynamic
    sequence(monkeypatch,calls(('financial_planning_basis',{})),calls(('prepare_transfer',dict(source='活期',target='储蓄',amount_yuan='100'))),reply('本轮仅分析，不办理资金操作。'))
    result=create(db,session(db),'live','只给预算建议，不要转账')
    assert not result['proposal'] and not result['authorization_operations']
    assert db.scalar(select(func.count()).select_from(Proposal))==0
    assert db.scalar(select(func.count()).select_from(TransferOrder))==0
    assert any(n.get('error_code')=='ANALYSIS_ONLY' for n in state(db,result['id'])['nodes'])


def test_basis_incomplete_months_are_not_zero(bank):
    db,_=bank;db.query(DataCoverage).filter(DataCoverage.owner_id=='u1').delete();db.flush()
    data=planning_basis.read(db,'u1')
    assert data['coverage']['status']=='incomplete'
    assert not {'monthly_expense_mean','monthly_direct_cash_expense_mean'} & {f['key'] for f in data['facts']}


def test_multi_goal_comparison_uses_actual_combined_cost_and_no_priority_choice():
    r=rstate(cash='15000',expense='2000');r['user_messages']=['旅行3000元，装修5000元，先只比较不办理']
    total=calc(r,'sum',[const(3000,label='旅行预算',quote='旅行3000元'),const(5000,label='装修预算',quote='装修5000元')])
    r['facts'].append(dict(total,node='costs',id='f8',tool='calculate',source_ref='costs'))
    value=calc(r,'goal_budget',[ref('available_liquid'),ref('monthly_expense_mean'),const(1,'预留月数','number'),dict(node='costs',key='result')])
    assert value['value']=='5000.00'
    assert {a['label'] for a in value['assumptions']} >= {'旅行预算','装修预算'}
    assert planning_assumptions.advice_only(r['user_messages'][0])


def test_positive_buffer_cannot_be_described_as_eating_emergency_reserve():
    with pytest.raises(DomainError):
        planning_reply.validate_judgment('旅行费用必须挪用已保留应急资金。',[dict(metric='funding_buffer',verdict='positive_buffer',value='100')])
    planning_reply.validate_judgment('已保留所列预留并扣除目标成本，仍有正余量；未记录义务尚需核实。',[dict(metric='funding_buffer',verdict='positive_buffer',value='100')])


MATRIX=json.loads((Path(__file__).resolve().parents[2]/'evals/f01-cases-20261017.json').read_text(encoding='utf-8'))['cases']
@pytest.mark.parametrize('case',MATRIX,ids=[c['id'] for c in MATRIX])
def test_fourteen_profile_matrix(case):
    r=rstate(cash=case.get('cash','10000'),expense=case.get('expense','1000'),
        direct=case.get('direct','600'),dues=case.get('dues','2000'))
    r['user_messages']=[case['message']]
    if case.get('scope'):r['facts'][1]['expense_scope']=case['scope']
    if case.get('incomplete'):r['facts'][1]['data_complete']=False
    op=case['operation']
    if op=='income_shock':inputs=[ref('monthly_income_mean'),ref('monthly_cashflow_mean'),const(0,'收入保留比例','number')]
    elif op=='savings_capacity':inputs=[ref('monthly_cashflow_mean'),const(case['target'],quote=case['quote'])]
    elif op=='obligation_budget':inputs=[ref('available_liquid'),ref('monthly_direct_cash_expense_mean'),const(1,'预留月数','number'),ref('upcoming_due_known'),const(case['target'],quote=case['quote'])]
    else:
        inputs=[ref('available_liquid'),ref('monthly_expense_mean'),const(case.get('months','1'),'预留月数','number')]
        if op=='goal_budget':inputs.append(const(case['target'],quote=case.get('quote')))
    if case.get('error'):
        with pytest.raises(DomainError) as error:calc(r,op,inputs)
        assert error.value.code==case['error']
    else:
        result=calc(r,op,inputs)
        assert Decimal(result['value'])==Decimal(case['expected'])
        if case.get('scope'):assert '含经营' in result['label']


def test_budget_change_preserves_unchanged_explicit_horizon():
    from app.agent.analysis_workflow import Operand
    planning_assumptions.validate_quote(Operand(value='3',unit='number',label='预留月数',quote='三个月'),
        ['旅行预算两万，保留三个月','两万改八千'])
    planning_assumptions.validate_quote(Operand(value='20000',unit='yuan',label='预算',quote='两万'),
        ['旅行预算两万，保留三个月','期限改为两个月'])


def test_source_date_and_obligation_context_reach_model(bank):
    db,_=bank
    archive(db,'users','u1',{'id':'u1'})
    archive(db,'liabilities','loan',dict(liability_id='loan',liability_type='loan',currency='CNY',status='active',
        next_due_date='2026-10-16',next_principal_minor='10000',next_interest_minor='2000'))
    data=planning_basis.read(db,'u1')
    r=rstate();node=dict(id='s1',name='financial_planning_basis',arguments={},model_call_id='fake',status='pending')
    result=runtime.observation(r,node,data)
    assert result['data']['upcoming_obligations']['through']=='2026-10-31'
    assert result['data']['decision_context']['spending']['necessity_verified'] is False


def test_advice_guard_preserves_an_explicit_different_account_instruction():
    assert not planning_assumptions.advice_only('不要从储蓄账户转账，直接从活期转100元')


def test_plain_text_does_not_bypass_necessity_guard():
    from app.agent.dynamic_protocol import Respond
    with pytest.raises(DomainError):
        runtime.render_response(Respond(text='购物都是可选支出。'),rstate())


def test_current_cash_cannot_be_used_as_an_invented_travel_price():
    with pytest.raises(DomainError):
        calc(rstate(),'goal_budget',[ref('available_liquid'),ref('monthly_expense_mean'),const(1,'预留月数','number'),ref('available_liquid')])


@pytest.mark.parametrize('bad',['营业额就是利润。','毛回款全部是可自由支配收入。','授信额度就是可用现金。','贷款余额就是本月应付。','我已为你锁定预算。'])
def test_economic_type_and_actual_action_claims_are_checked(bad):
    with pytest.raises(DomainError):planning_reply.validate_assertions(bad,rstate())


def test_known_dues_cannot_be_ignored_in_affordability_verdict():
    r=rstate();value=calc(r,'goal_budget',[ref('available_liquid'),ref('monthly_expense_mean'),const(1,'预留月数','number'),const(800,quote='预算800元')])
    assert value['known_dues_not_deducted']
    r['facts'].append(dict(value,id='f7',node='cash',tool='calculate',source_ref='cash'))
    s=planning_reply.Synthesis(goal='旅行',conclusion='预算足够。',reasons=[dict(text='已测算余量。',fact_ids=['f7'])])
    with pytest.raises(DomainError):planning_reply.response_text(s,r)


def test_plain_text_cannot_ignore_known_unpaid_bill():
    r=rstate();r['facts'][0]['known_dues_not_deducted']=True
    with pytest.raises(DomainError):runtime.render_response(__import__('app.agent.dynamic_protocol',fromlist=['Respond']).Respond(text='预算足够。'),r)


def test_explicit_reserve_is_not_asked_again():
    r=rstate();r['facts'][0]['assumptions']=[dict(label='预留月数',unit='number',value='3',source='用户明确参数')]
    s=planning_reply.Synthesis(goal='旅行',conclusion='仍需核对其他未记录义务。',
        reasons=[dict(text='已有当前可用现金。',fact_ids=['f1'])],questions=['希望保留几个月生活费？'])
    with pytest.raises(DomainError):planning_reply.response_text(s,r)


@pytest.mark.parametrize('quote',['不是两万，是八千','两万改八千','不是两万，是八千，其他条件不变'])
def test_full_correction_quote_supports_only_new_value(quote):
    r=rstate(cash='30000');r['user_messages']=['预算两万元，保留三个月',quote]
    good=calc(r,'goal_budget',[ref('available_liquid'),ref('monthly_expense_mean'),const(3,'预留月数','number',quote='三个月'),const(8000,quote=quote)])
    assert good['value']=='19000.00'
    with pytest.raises(DomainError):
        calc(r,'goal_budget',[ref('available_liquid'),ref('monthly_expense_mean'),const(3,'预留月数','number'),const(20000,quote=quote)])


def test_paid_loan_installment_is_not_counted_again(bank):
    db,_=bank
    tx=post_entry(db,owner_id='u1',account_id='a1c',kind='debt_repayment',direction='debit',amount_minor=12000,
        business_at=datetime(2026,9,16,tzinfo=timezone.utc),business_id='f01-loan-paid')
    archive(db,'liabilities','loan',dict(liability_id='loan',liability_type='loan',currency='CNY',status='active',
        next_due_date='2026-09-16',next_principal_minor='10000',next_interest_minor='2000',outstanding_balance_minor='9000000'))
    archive(db,'liability_events','paid-loan',dict(liability_id='loan',event_type='loan_payment',cash_transaction_id=tx.id,occurred_at_local='2026-09-16T09:00:00+08:00'))
    result=planning_obligations.read(db,'u1',date(2026,9,24))
    assert result['known_unpaid_minor']==0 and result['items'][0]['state']=='paid'


@pytest.mark.parametrize('text',['断收时进货可放缓。','收入中断后经营成本可以暂停。'])
def test_income_interruption_does_not_prove_cost_flexibility(text):
    with pytest.raises(DomainError):planning_reply.validate_assertions(text,rstate())
    planning_reply.validate_assertions('假设经用户核实可暂缓进货，另列情景；原合同付款刚性未知。',rstate())


def test_real_business_candidate_cannot_hide_flexibility_behind_other_negation():
    candidate='断收时进货可放缓，但直接现金支出仍需{{f19}}/月，不能与净现金流相加重复计算。'
    with pytest.raises(DomainError):planning_reply.validate_assertions(candidate,rstate())
    planning_reply.validate_assertions('不能认为断收时进货可放缓，合同付款刚性仍须核实。',rstate())
