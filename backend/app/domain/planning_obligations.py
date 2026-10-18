"""Read-only, owner-scoped dues. Snapshots never grant payment authority."""
from calendar import monthrange
from datetime import date, datetime
from zoneinfo import ZoneInfo
from sqlalchemy import select, func, case
from app.data.models import Transaction
from app.domain.clock import selected_range
from app.domain.financial_profile import rows


def amount(row,key):
    value=row.get(key)
    try:
        if value is None or value=='':return None
        result=int(value)
        return result if result>=0 and str(result)==str(value).strip() else None
    except (ValueError,TypeError):return None


def due_window(today):
    year,month=(today.year+1,1) if today.month==12 else (today.year,today.month+1)
    return date(year,month,monthrange(year,month)[1])


def read(db,owner,today):
    debts=rows(db,owner,'liabilities')
    by_id={r['liability_id']:r for r in debts}
    end=due_window(today);items=[];unknown=[];seen=set()
    latest={}
    for bill in rows(db,owner,'credit_card_statements'):
        if str(bill.get('statement_date',''))>today.isoformat():continue
        key=bill.get('liability_id') or bill.get('card_id')
        if not key:unknown.append('statement_owner_reference_missing');continue
        if key not in latest or bill.get('statement_date','')>latest[key].get('statement_date',''):latest[key]=bill
    paid_ids=set()
    for bill in latest.values():
        due=bill.get('due_date')
        try:date.fromisoformat(due)
        except (ValueError,TypeError):unknown.append('statement_due_date_unknown');continue
        if due>end.isoformat():continue
        key=(bill.get('liability_id'),bill.get('statement_date'))
        if key in seen:unknown.append('duplicate_statement');continue
        seen.add(key)
        balance=amount(bill,'statement_balance_minor');paid=amount(bill,'payment_minor')
        liability=by_id.get(bill.get('liability_id'),{})
        currency=liability.get('currency')
        txid=bill.get('payment_transaction_id')
        tx=db.get(Transaction,txid) if txid else None
        linked=bool(tx and tx.owner_id==owner and tx.status=='posted' and tx.direction=='debit'
            and tx.type=='credit_card_repayment' and tx.currency=='CNY' and datetime.fromisoformat(tx.posted_at_utc).astimezone(ZoneInfo('Asia/Shanghai')).date()<=today)
        if currency!='CNY' or balance is None or paid is None or (paid>0 and not linked):
            unknown.append('unverified_statement_payment');continue
        # Ledger evidence must support the snapshot payment, not just a status word.
        if paid>0 and (tx.amount_minor!=paid or txid in paid_ids):
            unknown.append('payment_amount_mismatch');continue
        if paid>0:paid_ids.add(txid)
        remaining=max(0,balance-paid)
        if bill.get('status')=='paid' and remaining:
            unknown.append('inconsistent_paid_statement');continue
        items.append(dict(kind='credit_card_statement',reference=bill['statement_id'],due_date=due,
            liability_id=bill.get('liability_id'),remaining_minor=remaining,state='paid' if remaining==0 else 'overdue' if due<today.isoformat() else 'unpaid',
            historical_payment_minor=paid,source='financial_archive+cash_ledger' if linked else 'financial_archive',
            add_to_historical_expense=False))
    for loan in debts:
        if loan.get('liability_type')=='credit_card' or loan.get('linked_card_id'):continue
        if loan.get('status') in {'closed','repaid','paid'}:continue
        due=loan.get('next_due_date')
        try:date.fromisoformat(due)
        except (ValueError,TypeError):
            unknown.append('loan_due_date_unknown');continue
        if due>end.isoformat():continue
        principal=amount(loan,'next_principal_minor');interest=amount(loan,'next_interest_minor')
        if principal is None or interest is None or loan.get('currency')!='CNY':
            unknown.append('loan_installment_unknown');continue
        paid=0
        if due<=today.isoformat():
            ids={e.get('cash_transaction_id') for e in rows(db,owner,'liability_events')
                if e.get('liability_id')==loan['liability_id'] and e.get('event_type')=='loan_payment'
                and str(e.get('occurred_at_local',''))[:10]==due and e.get('cash_transaction_id')}
            for txid in ids:
                tx=db.get(Transaction,txid)
                if tx and tx.owner_id==owner and tx.status=='posted' and tx.type=='debt_repayment' and tx.direction=='debit' and tx.currency=='CNY' and datetime.fromisoformat(tx.posted_at_utc).astimezone(ZoneInfo('Asia/Shanghai')).date()<=today:
                    paid+=tx.amount_minor
                else:unknown.append('loan_payment_link_unverified')
            if not ids and due<today.isoformat() and not int(loan.get('overdue_days') or 0) and loan.get('status')!='overdue':
                unknown.append('past_due_loan_payment_unknown');continue
        remaining=max(0,principal+interest-paid)
        items.append(dict(kind='loan_installment',reference=loan['liability_id'],due_date=due,
            remaining_minor=remaining,state='paid' if remaining==0 else 'overdue' if due<today.isoformat() else 'scheduled_snapshot',
            principal_minor=principal,interest_minor=interest,source='financial_archive',
            add_to_historical_expense=False))
    # A debt stock with no statement is unknown, never an assumed zero bill.
    for debt in debts:
        if debt.get('liability_type')=='credit_card' and (amount(debt,'outstanding_balance_minor') or 0)>0:
            if not any(i.get('liability_id')==debt.get('liability_id') for i in items):
                unknown.append('credit_due_not_in_visible_window')
    if not debts and not rows(db,owner,'users'):unknown.append('no_financial_archive')
    return dict(as_of=today.isoformat(),through=end.isoformat(),items=items[:40],
        known_unpaid_minor=sum(i['remaining_minor'] for i in items),unknown_reasons=sorted(set(unknown)),
        coverage='visible_contracts_only',statement_basis='每张卡只取最新已出账快照，跨月账单余额不相加',all_visible_amounts_known=not unknown,
        rules=['已支付账单不再扣减；消费与还款不相加。','只列可见账单/下一期本金加利息，不使用贷款余额充当应付。',
               '未来计划是情景，未记为历史支出；可用活期已扣资金预留，不重复扣预留。',
               '档案截至日之后的新付款或未记录义务仍须核对，非实时征信或完整家庭负债。'])


def cash_expenses(db,owner,start,end):
    period=selected_range(start,end)
    filters=[Transaction.owner_id==owner,Transaction.status=='posted',
        Transaction.posted_at_utc>=period.start_utc,Transaction.posted_at_utc<period.end_utc]
    nondebt=func.sum(case((Transaction.type=='purchase',Transaction.amount_minor),
        (Transaction.type=='refund',-Transaction.amount_minor),else_=0))
    debt=func.sum(case((Transaction.type.in_(['credit_card_repayment','debt_repayment']),Transaction.amount_minor),else_=0))
    a,b=db.execute(select(nondebt,debt).where(*filters)).one()
    return dict(direct_non_debt_expense_minor=int(a or 0),historical_debt_payment_minor=int(b or 0))
