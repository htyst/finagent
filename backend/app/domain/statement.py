"""Read-only statement and anomaly facts; no model-generated financial numbers."""
from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date, datetime, timedelta
from statistics import median
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.data.models import Transaction
from app.domain.clock import SHANGHAI, parse_day, selected_range
from app.domain.errors import DomainError
from app.domain.spending import summarize_spending


def generate_statement(db: Session, *, owner_id: str, report_kind: str, **args) -> dict:
    start,end=args.get('start'),args.get('end')
    if not start or not end or report_kind not in {'monthly','annual'}:
        raise DomainError('INVALID_INPUT','报告需要明确起止日期和月度/年度类型')
    period=selected_range(start,end);first,last=period.start,period.end_exclusive-timedelta(days=1)
    calendar_year=first.month==first.day==1 and last==date(first.year,12,31)
    if report_kind=='monthly' and not (first.day==1 and first.year==last.year and first.month==last.month and last.day==calendar.monthrange(last.year,last.month)[1]):
        raise DomainError('INVALID_INPUT','月报需要完整日历月；数据缺失另行注明')
    if report_kind=='annual' and not 365<=(period.end_exclusive-first).days<=366:
        raise DomainError('INVALID_INPUT','年度报告支持完整自然年或连续365/366天；较短区间请查询消费统计')
    result=summarize_spending(db,owner_id=owner_id,**args)
    result.update(report_kind=report_kind,report_scope='calendar_year' if calendar_year else 'rolling_year' if report_kind=='annual' else 'calendar_month',
        title=f'{first.year}年年度账单报告' if calendar_year else f'{first.isoformat()}至{last.isoformat()}连续一年账单报告' if report_kind=='annual' else f'{first.year}年{first.month}月月度账单报告')
    if report_kind=='annual':
        months=[];cursor=first.replace(day=1)
        while cursor<=last:
            month_last=date(cursor.year,cursor.month,calendar.monthrange(cursor.year,cursor.month)[1])
            begin,finish=max(cursor,first),min(month_last,last)
            month_args=dict(args,start=begin.isoformat(),end=finish.isoformat(),comparison_mode='none',compare_start=None,compare_end=None,evidence_limit=1,evidence_offset=0)
            current=summarize_spending(db,owner_id=owner_id,**month_args)['current']
            complete=current['totals']['is_complete'];full=begin==cursor and finish==month_last
            months.append(dict(month=cursor.strftime('%Y-%m'),period=current['period'],coverage=current['coverage'],status='complete' if complete else 'incomplete',
                full_calendar_month=full,totals=current['totals'] if complete else None,observed_totals=current['totals'],categories=current['categories'],
                evidence={'total':current['evidence']['total'],'limit':0,'offset':0,'items':[],'detail_query':dict(start=begin.isoformat(),end=finish.isoformat(),category=result['category'],card_id=args.get('card_id'),account_id=args.get('account_id'))}))
            cursor=date(cursor.year+1,1,1) if cursor.month==12 else date(cursor.year,cursor.month+1,1)
        eligible=[m for m in months if m['totals'] is not None and m['full_calendar_month']]
        maximum=max((m['totals']['net_minor'] for m in eligible),default=None)
        result.update(months=months,complete_months=[m['month'] for m in eligible],missing_months=[m['month'] for m in months if m['totals'] is None],
            partial_boundary_months=[m['month'] for m in months if not m['full_calendar_month']],
            ranking_scope='full_year' if calendar_year and len(eligible)==12 else 'complete_months_only',
            ranking_note='最高月份仅比较数据完整的整日历月；首尾不足整月的区间不参与排名',
            highest_months=[dict(month=m['month'],net_minor=maximum) for m in eligible if m['totals']['net_minor']==maximum])
    return result


def inspect_anomalies(db: Session, *, owner_id: str, start: str | None = None, end: str | None = None,
                      account_id: str | None = None, category: str | None = None,card_id: str | None = None) -> dict:
    # Reuse ownership/date/coverage validation, but scan all posted purchases,
    # not just the paginated evidence. Refunds and transfers are not candidates.
    summary = summarize_spending(db, owner_id=owner_id, start=start, end=end,
        account_id=account_id, card_id=card_id,category=category, comparison_mode="none")
    period = summary["current"]["period"]
    from app.domain.financial_queries import fact_query,date_filtered
    from app.domain.categories import query_category
    f=fact_query(db,owner_id,[account_id] if account_id else None,card_id,expense_only=True)
    filters=date_filtered(f,selected_range(period['start_date'],(parse_day(period['end_exclusive_date'])-timedelta(days=1)).isoformat()),query_category(category))
    filters.append(f.c.type=='purchase')
    rows=db.execute(select(f).where(*filters).order_by(f.c.posted_at_utc,f.c.id)).all()
    middle = median([row.amount_minor for row in rows]) if rows else 0
    # Robust median, enough observations, absolute floor. A late timestamp is
    # contextual evidence, not by itself a fraud flag.
    threshold = max(100_000, middle * 10)
    hits = defaultdict(list)
    if len(rows) >= 5:
        for row in rows:
            if row.amount_minor >= threshold:
                hits[row.id].append({"rule": "large_amount", "reason": f"金额 {row.amount_minor/100:,.2f} 元达到阈值 {threshold/100:,.2f} 元（至少1000元且至少本范围消费中位数的10倍）", "related_transaction_ids": []})
    groups = defaultdict(list)
    for row in rows:
        if row.merchant and row.merchant.strip():
            groups[(row.account_id, row.merchant.strip().casefold(), row.amount_minor)].append(row)
    for group in groups.values():
        for index, row in enumerate(group):
            related = [other.id for other in group if other.id != row.id and
                       abs((datetime.fromisoformat(row.posted_at_utc) - datetime.fromisoformat(other.posted_at_utc)).total_seconds()) <= 120]
            if related:
                hits[row.id].append({"rule": "near_duplicate", "reason": "同一账户、同商户、同金额在120秒内出现多笔，需核实是否重复扣款", "related_transaction_ids": related})
    candidates = [{"transaction_id": row.id, "account_id": row.account_id, "merchant": row.merchant,
        "amount_minor": row.amount_minor, "category": row.category or "未分类",
        "posted_at_utc": row.posted_at_utc, "posted_at_local": datetime.fromisoformat(row.posted_at_utc).astimezone(SHANGHAI).isoformat(),
        "reasons": hits[row.id]} for row in rows if row.id in hits]
    return {"report_kind": "anomalies", "period": period, "coverage": summary["current"]["coverage"],
        "scanned_count": len(rows), "candidate_count": len(candidates), "candidates": candidates,
        "unflagged_count": len(rows) - len(candidates), "rules_version": "anomalies-v1",
        "rules": {"large_amount": {"minimum_observations": 5, "median_minor": middle,
            "multiplier": 10, "floor_minor": 100000, "threshold_minor": threshold, "enabled": len(rows) >= 5},
            "near_duplicate": {"window_seconds": 120, "same_fields": ["account", "merchant", "amount"]}},
        "conclusion": "以上仅是规则筛查的待核实线索，尚未确定盗刷或重复扣款；未标记也不代表绝对安全。请核对本人及授权消费、商户票据，必要时联系发卡机构。",
        "dataset_version": summary["dataset_version"]}
