from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select,func,case
from sqlalchemy.orm import Session

from app.data.models import Account, DataCoverage, DemoSetting, Transaction
from app.domain.clock import (DateRange, current_month_range, parse_day, previous_corresponding_range,
                              previous_full_month_range, selected_range)
from app.domain.errors import DomainError

RULES_VERSION = "spending-v2-cash-and-credit"


def _coverage(db: Session, owner_id: str, account_ids: list[str], period: DateRange) -> dict:
    rows = db.scalars(select(DataCoverage).where(
        DataCoverage.owner_id == owner_id, DataCoverage.account_id.in_(account_ids),
        DataCoverage.end_utc > period.start_utc, DataCoverage.start_utc < period.end_utc,
    ).order_by(DataCoverage.account_id, DataCoverage.start_utc)).all()
    by_account = defaultdict(list)
    for row in rows:
        by_account[row.account_id].append(row)
    missing: list[str] = []
    for account_id in account_ids:
        cursor = period.start_utc
        for row in by_account[account_id]:
            if row.completeness != "complete" or row.start_utc > cursor:
                continue
            if row.end_utc > cursor:
                cursor = row.end_utc
            if cursor >= period.end_utc:
                break
        if cursor < period.end_utc:
            missing.append(account_id)
    return {"status": "incomplete" if missing else "complete", "missing_account_ids": missing,
            "segments": [{"account_id": row.account_id, "start_utc": row.start_utc,
                          "end_utc": row.end_utc, "completeness": row.completeness} for row in rows]}


def _summarize_period(db: Session, owner_id: str, account_ids: list[str], period: DateRange,
                      category: str | None, evidence_limit: int, evidence_offset: int,
                      card_id=None,account_filter=False,merchant=None) -> dict:
    from app.domain.financial_queries import fact_query,date_filtered
    from app.domain.financial_profile import rows as archive_rows, dataset_manifest
    facts=fact_query(db,owner_id,account_ids if account_filter else None,card_id,expense_only=True)
    filters=date_filtered(facts,period,category,merchant)
    purchase=case((facts.c.type=='purchase',facts.c.amount_minor),else_=0)
    refund=case((facts.c.type=='refund',facts.c.amount_minor),else_=0)
    groups=db.execute(select(facts.c.category,func.sum(purchase),func.sum(refund),
        func.sum(case((facts.c.type=='purchase',1),else_=0)),func.sum(case((facts.c.type=='refund',1),else_=0))).where(*filters).group_by(facts.c.category).order_by(facts.c.category)).all()
    categories=[dict(category=c,purchase_minor=int(p or 0),refund_minor=int(r or 0),net_minor=int((p or 0)-(r or 0)),purchase_count=int(pc or 0),refund_count=int(rc or 0)) for c,p,r,pc,rc in groups]
    purchase_minor=sum(c['purchase_minor'] for c in categories);refund_minor=sum(c['refund_minor'] for c in categories)
    purchase_count=sum(c['purchase_count'] for c in categories);refund_count=sum(c['refund_count'] for c in categories)
    coverage = _coverage(db, owner_id, account_ids, period)
    credit_cards=[r for r in archive_rows(db,owner_id,'cards') if r['card_type']=='credit' and (not card_id or r['id']==card_id)]
    include_credit=bool(credit_cards) and not account_filter
    manifest=dataset_manifest(db)
    source_coverage={'cash_ledger':coverage['status']}
    if card_id and credit_cards:
        coverage={'status':'complete','missing_account_ids':[],'segments':[]};source_coverage={}
    if include_credit:
        if manifest:
            covered=selected_range(manifest['period_start'],manifest['period_end'])
            credit_complete=covered.start<=period.start and covered.end_exclusive>=period.end_exclusive
        else:credit_complete=False
        source_coverage['credit_card_ledger']='complete' if credit_complete else 'incomplete'
        if not credit_complete:coverage['status']='incomplete'
    coverage.update(included_sources=list(source_coverage),source_coverage=source_coverage,
        excluded_sources=[] if include_credit or not credit_cards else ['credit_card_ledger'],
        scope='selected_deposit_account' if account_filter else 'selected_card' if card_id else 'all_owned_expense_sources')
    selected=db.execute(select(facts).where(*filters).order_by(facts.c.posted_at_utc.desc(),facts.c.id).offset(evidence_offset).limit(evidence_limit)).mappings()
    items=[{**dict(row),'transaction_id':row['id'],'signed_minor':row['amount_minor'] if row['type']=='purchase' else -row['amount_minor']} for row in selected]
    total=purchase_count+refund_count
    return {"period": period.as_dict(), "coverage": coverage,
            "totals": {"purchase_minor": purchase_minor, "refund_minor": refund_minor,
                       "net_minor": purchase_minor - refund_minor, "purchase_count": purchase_count,
                       "refund_count": refund_count, "is_complete": coverage["status"] == "complete"},
            "categories": categories,
            "evidence": {"total":total, "limit": evidence_limit, "offset": evidence_offset, "items": items,'has_more':evidence_offset+len(items)<total}}


def summarize_spending(db: Session, *, owner_id: str, account_id: str | None = None,
                       category: str | None = None, start: str | None = None, end: str | None = None,
                       comparison_mode: str | None = None,
                       compare_start: str | None = None, compare_end: str | None = None,
                       evidence_limit: int = 20, evidence_offset: int = 0,card_id: str | None = None,merchant: str | None = None) -> dict:
    """Compute from all matching posted rows, then page only the evidence."""
    setting = db.get(DemoSetting, 1)
    if setting is None:
        raise DomainError("DATA_UNAVAILABLE", "演示数据尚未初始化", 503)
    if (start is None) != (end is None):
        raise DomainError("INVALID_INPUT", "开始和结束日期必须同时提供")
    if evidence_limit < 1 or evidence_limit > 100 or evidence_offset < 0:
        raise DomainError("INVALID_INPUT", "证据分页参数无效")
    demo_date = parse_day(setting.demo_date)
    current = selected_range(start, end) if start is not None else current_month_range(demo_date)
    mode = comparison_mode or ("none" if start is not None else "previous_corresponding")
    if mode not in {"none", "previous_corresponding", "previous_full_month", "custom"}:
        raise DomainError("INVALID_INPUT", "无效比较方式")
    if mode in {"previous_corresponding", "previous_full_month"} and start is not None:
        raise DomainError("INVALID_INPUT", "自定义主区间请使用自定义比较区间或不比较")
    if mode == "custom":
        if compare_start is None or compare_end is None:
            raise DomainError("INVALID_INPUT", "自定义比较区间必须提供起止日期")
        baseline = selected_range(compare_start, compare_end)
    elif compare_start is not None or compare_end is not None:
        raise DomainError("INVALID_INPUT", "比较日期只适用于自定义比较")
    elif mode == "previous_corresponding":
        baseline = previous_corresponding_range(demo_date)
    elif mode == "previous_full_month":
        baseline = previous_full_month_range(demo_date)
    else:
        baseline = None
    if account_id:
        own = db.scalar(select(Account.id).where(Account.id == account_id, Account.owner_id == owner_id))
        if own is None:
            raise DomainError("RESOURCE_NOT_FOUND", "资源不存在", 404)
        accounts = [account_id]
    else:
        accounts = db.scalars(select(Account.id).where(Account.owner_id == owner_id).order_by(Account.id)).all()
    if not accounts:
        raise DomainError("RESOURCE_NOT_FOUND", "没有可查询的账户", 404)
    if card_id:
        from app.domain.financial_queries import require_card
        card=require_card(db,owner_id,card_id)
        if account_id and card.get('account_id')!=account_id:raise DomainError('INVALID_INPUT','银行卡不属于所选账户')
        if not account_id:accounts=[card['account_id']] if card.get('account_id') else []
    from app.domain.categories import query_category
    category=query_category(category)
    present = _summarize_period(db, owner_id, accounts, current, category, evidence_limit, evidence_offset,card_id,bool(account_id),merchant)
    comparison = None
    if baseline:
        past = _summarize_period(db, owner_id, accounts, baseline, category, evidence_limit, 0,card_id,bool(account_id),merchant)
        reason = None
        delta = percent = None
        if not present["totals"]["is_complete"] or not past["totals"]["is_complete"]:
            reason = "DATA_INCOMPLETE"
        else:
            delta = present["totals"]["net_minor"] - past["totals"]["net_minor"]
            base_net = past["totals"]["net_minor"]
            if base_net <= 0:
                reason = "BASELINE_NONPOSITIVE"
            else:
                percent = str((Decimal(delta) * Decimal(100) / Decimal(base_net)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
        comparison = {"mode": mode, "period": past["period"], "coverage": past["coverage"],
                      "totals": past["totals"], "delta_minor": delta,
                      "percent_change": percent, "percent_change_reason": reason}
    return {"account_ids": accounts, "category": category, "current": present,
            "comparison": comparison, "rules_version": RULES_VERSION, "dataset_version": setting.dataset_version,
            'card_id':card_id,'merchant':merchant,'included_sources':present['coverage']['included_sources'],
            'excluded_sources':present['coverage']['excluded_sources'],'expense_basis':'已入账现金消费及信用卡消费减退款；还款、本金调拨、未付利息不重复计消费'}
