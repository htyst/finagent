"""Planning facts from full owner-scoped ledgers, never transaction excerpts."""
from calendar import monthrange
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from sqlalchemy import select
from app.data.models import Account, DemoSetting
from app.domain.financial_profile import portfolio, rows
from app.domain.spending import summarize_spending
from app.domain.financial_analysis import analyze
from app.domain.ledger import available_balance
from app.domain import planning_obligations


def read(db, owner, lookback_months=3):
    setting = db.get(DemoSetting, 1)
    today = date.fromisoformat(setting.demo_date)
    end = today.replace(day=1)
    periods = []
    for _ in range(lookback_months):
        year, month = (end.year-1, 12) if end.month == 1 else (end.year, end.month-1)
        start = date(year, month, 1)
        periods.append((start.isoformat(), date(year, month, monthrange(year, month)[1]).isoformat()))
        end = start
    periods.reverse()
    term_ids = {r['id'] for r in rows(db, owner, 'accounts') if r['account_type'] == 'term_deposit'}
    accounts = db.scalars(select(Account).where(Account.owner_id == owner)).all()
    liquid = sum(available_balance(db, a) for a in accounts if a.status == 'active' and a.id not in term_ids)
    profile = portfolio(db, owner)
    history, category_totals = [], {}
    for start, finish in periods:
        expense = summarize_spending(db, owner_id=owner, start=start, end=finish, evidence_limit=1)['current']
        cash = analyze(db, owner, 'cashflow', start=start, end=finish)['summary']
        history.append({'start':start, 'end':finish, 'expense_minor':expense['totals']['net_minor'],
                        **cash, **planning_obligations.cash_expenses(db,owner,start,finish), 'coverage':expense['coverage']['status']})
        if expense['coverage']['status'] == 'complete':
            for row in expense['categories']:
                category_totals[row['category']] = category_totals.get(row['category'],0)+row['net_minor']
    complete = [m for m in history if m['coverage'] == 'complete']
    facts = []
    def fact(key, label, value, unit='yuan',**extra):
        facts.append({'key':key, 'label':label, 'value':str(value), 'unit':unit,**extra})
    def yuan(minor): return Decimal(minor)/100
    def mean(key):
        return (sum(Decimal(m[key]) for m in complete)/len(complete)/100).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    fact('available_liquid', '当前可用活期', yuan(liquid))
    fact('complete_months', '完整月份数', len(complete), 'number')
    if profile.get('available'):
        balances = profile['balances']
        for key, label, field in [('term_principal','定期本金','term_minor'),
            ('snapshot_debt','快照贷款及信用卡欠款','liabilities_snapshot_minor'),
            ('snapshot_investments','快照基金市值','investment_snapshot_minor')]:
            fact(key, label, yuan(balances[field]))
    has_business='经营支出' in category_totals
    if complete:
        fact('monthly_expense_mean','月均净消费（含经营支出，不是纯生活费）' if has_business else '月均净消费',mean('expense_minor'),
            expense_scope='business_and_household' if has_business else 'visible_consumption')
        fact('monthly_income_mean','历史月均已入账收入',mean('income_minor'))
        fact('monthly_cashflow_mean','历史月均净现金流',mean('net_cashflow_minor'))
        fact('monthly_expense_peak','最高月份净消费',yuan(max(m['expense_minor'] for m in complete)))
        for index, (category,total) in enumerate(sorted(category_totals.items(), key=lambda p:p[1], reverse=True)[:6]):
            fact('category_'+str(index), category+'月均净消费',
                 (yuan(total)/len(complete)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP))
        fact('monthly_cash_outflow_mean','历史月均对外现金流出（不与消费相加）',mean('outflow_minor'))
        fact('monthly_income_low','完整月份中最低已入账收入',yuan(min(m['income_minor'] for m in complete)))
        fact('monthly_income_high','完整月份中最高已入账收入',yuan(max(m['income_minor'] for m in complete)))
    if complete:
        fact('monthly_direct_cash_expense_mean','月均直接现金净消费（不含信用卡消费及还款）',mean('direct_non_debt_expense_minor'),expense_scope='non_debt_cash')
        fact('monthly_debt_payment_mean','历史月均还款现金流（已计对外现金流，不再相加）',mean('historical_debt_payment_minor'))
        fact('monthly_cashflow_low','完整月份最低净现金流',yuan(min(m['net_cashflow_minor'] for m in complete)))
        fact('monthly_cashflow_high','完整月份最高净现金流',yuan(max(m['net_cashflow_minor'] for m in complete)))
    obligations=planning_obligations.read(db,owner,today)
    if obligations['all_visible_amounts_known']:
        fact('upcoming_due_known','截至档案日可见的近期未付账单及下一期分期（不是负债余额）',yuan(obligations['known_unpaid_minor']),expense_scope='visible_due')
    assets={'profile_available':bool(profile.get('available')),'budget_base':'available_liquid_only',
        'non_cash_assets_are_spendable':False}
    if profile.get('available'):
        for key,label,field in [('snapshot_physical_assets','房车等估值快照','physical_minor'),
            ('native_investments','本系统理财持仓估值（不计可用活期）','new_app_wealth_minor'),
            ('mixed_net_worth','混合时点净资产（仅档案参考）','net_worth_minor')]:fact(key,label,yuan(profile['balances'][field]))
        assets['net_worth_timing']='当前银行余额和导入资产负债快照混合，不是当前可支配预算'
    user=next(iter(rows(db,owner,'users')), {})
    decision_context={'assets':assets,
        'income_source':{'pattern':user.get('income_pattern'),'scope':user.get('income_scope'),
            'source':'导入画像，仅描述可见入账范围；经营毛回款不等于工资或利润，不假定覆盖全家庭收入'},
        'liabilities':{'snapshot_only':True,'future_due_amount':None,'future_due_dates_verified':False,
            'rule':'欠款余额不是近期到期账单；贷款还本及信用卡还款已影响历史现金流，不能再次从历史净现金流重复扣减。'},
        'spending':{'categories_measure':'完整月份的月均净消费，不是本月单月金额',
            'classification_evidence':[dict(category=k,scope='business' if k=='经营支出' else 'unclassified',necessity='unknown',adjustable_share=None) for k in category_totals],
            'adjustable_amount_verified':False,
            'has_business_expenses':has_business,
            'scope_note':'月均消费含明确经营支出，不能全部称家庭生活费；家庭与经营周转预留须分别核对。' if has_business else '只代表当前本人账本可见消费，不假定覆盖全家庭。',
            'necessity_verified':False,'rule':'分类大小不证明刚需、浪费、可压缩比例或使用频率；优化建议须由用户核实。'},
        'cashflow':{'future_income_guaranteed':False,'rule':'净消费和对外现金流出是不同口径，不能相加；无收入情景不能继续使用历史正结余。'},
        'goal':{'unknown_cost_is_zero':False,'rule':'用户目标成本、期限、应急预留和未来必付款决定可行性；最多追问当前最影响判断的两项。'}}
    decision_context['liabilities'].update(future_due_amount=obligations['known_unpaid_minor'] if obligations['all_visible_amounts_known'] else None,
        future_due_dates_verified=obligations['all_visible_amounts_known'],scope='可见档案截至日；不保证覆盖未记录义务')
    return {'facts':facts, 'decision_context':decision_context,'upcoming_obligations':obligations,'months':history, 'period_start':periods[0][0], 'period_end':periods[-1][1],
        'dataset_version':setting.dataset_version, 'snapshot_as_of':setting.demo_date,
        'bank_queried_at_utc':datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'coverage':{'status':'complete' if len(complete)==lookback_months else 'incomplete',
                    'requested_months':lookback_months,'complete_months':len(complete)},
        'limitations':['只使用演示日期之前的完整自然月；当月不外推成年/月均。',
          '消费与现金流采用不同口径，信用卡消费和还款不能重复相加。',
          '负债、基金及定期条款为导入快照；未知近期到期账单、家庭支出及未来收入须向用户核对。',
          '没有查询订阅合同或受保护扣费明细；消费分类总额不能推断具体订阅。',
          '历史现金流不是未来收入保证；资金测算还须保留生活费、已知应付款及用户的应急要求。']}
