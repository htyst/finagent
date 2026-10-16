from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.models import Account, DataCoverage, DemoSetting, Transaction
from app.domain.clock import (DateRange, current_month_range, parse_day, previous_corresponding_range,
                              previous_full_month_range, selected_range)
from app.domain.errors import DomainError

RULES_VERSION = "spending-v1"


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
                      category: str | None, evidence_limit: int, evidence_offset: int) -> dict:
    filters = [Transaction.owner_id == owner_id, Transaction.account_id.in_(account_ids),
               Transaction.status == "posted", Transaction.type.in_(["purchase", "refund"]),
               Transaction.posted_at_utc >= period.start_utc, Transaction.posted_at_utc < period.end_utc]
    if category == "未分类":
        filters.append(Transaction.category.is_(None))
    elif category:
        filters.append(Transaction.category == category)
    rows = db.scalars(select(Transaction).where(*filters).order_by(Transaction.posted_at_utc.desc(), Transaction.id)).all()
    categories: dict[str, dict] = {}
    purchase_minor = refund_minor = purchase_count = refund_count = 0
    for row in rows:
        label = row.category or "未分类"
        item = categories.setdefault(label, {"category": label, "purchase_minor": 0, "refund_minor": 0,
                                             "net_minor": 0, "purchase_count": 0, "refund_count": 0})
        if row.type == "purchase":
            purchase_minor += row.amount_minor
            purchase_count += 1
            item["purchase_minor"] += row.amount_minor
            item["purchase_count"] += 1
            item["net_minor"] += row.amount_minor
        else:
            refund_minor += row.amount_minor
            refund_count += 1
            item["refund_minor"] += row.amount_minor
            item["refund_count"] += 1
            item["net_minor"] -= row.amount_minor
    coverage = _coverage(db, owner_id, account_ids, period)
    items = [{"transaction_id": row.id, "account_id": row.account_id, "type": row.type,
              "amount_minor": row.amount_minor, "signed_minor": row.amount_minor if row.type == "purchase" else -row.amount_minor,
              "category": row.category or "未分类", "merchant": row.merchant,
              "posted_at_utc": row.posted_at_utc, "original_transaction_id": row.original_transaction_id}
             for row in rows[evidence_offset:evidence_offset + evidence_limit]]
    return {"period": period.as_dict(), "coverage": coverage,
            "totals": {"purchase_minor": purchase_minor, "refund_minor": refund_minor,
                       "net_minor": purchase_minor - refund_minor, "purchase_count": purchase_count,
                       "refund_count": refund_count, "is_complete": coverage["status"] == "complete"},
            "categories": [categories[key] for key in sorted(categories)],
            "evidence": {"total": len(rows), "limit": evidence_limit, "offset": evidence_offset, "items": items}}


def summarize_spending(db: Session, *, owner_id: str, account_id: str | None = None,
                       category: str | None = None, start: str | None = None, end: str | None = None,
                       comparison_mode: str | None = None,
                       compare_start: str | None = None, compare_end: str | None = None,
                       evidence_limit: int = 20, evidence_offset: int = 0) -> dict:
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
    category = category if category and category != "全部" else None
    present = _summarize_period(db, owner_id, accounts, current, category, evidence_limit, evidence_offset)
    comparison = None
    if baseline:
        past = _summarize_period(db, owner_id, accounts, baseline, category, evidence_limit, 0)
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
            "comparison": comparison, "rules_version": RULES_VERSION, "dataset_version": setting.dataset_version}
