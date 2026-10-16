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
    start, end = args.get("start"), args.get("end")
    if not start or not end or report_kind not in {"monthly", "annual"}:
        raise DomainError("INVALID_INPUT", "报告需要明确起止日期和月度或年度类型")
    first, last = parse_day(start), parse_day(end)
    if report_kind == "annual":
        valid = first.month == first.day == 1 and last == date(first.year, 12, 31)
    else:
        valid = first.day == 1 and first.year == last.year and first.month == last.month and last.day == calendar.monthrange(first.year, first.month)[1]
    if not valid:
        raise DomainError("INVALID_INPUT", "年度或月度报告必须覆盖完整日历年或月；数据缺失会另行注明")
    result = summarize_spending(db, owner_id=owner_id, **args)
    result["report_kind"] = report_kind
    result["title"] = f"{first.year}年年度账单报告" if report_kind == "annual" else f"{first.year}年{first.month}月月度账单报告"
    if report_kind == "annual":
        months = []
        for month in range(1, 13):
            month_args = dict(args, start=date(first.year, month, 1).isoformat(),
                              end=date(first.year, month, calendar.monthrange(first.year, month)[1]).isoformat(),
                              comparison_mode="none", compare_start=None, compare_end=None, evidence_offset=0)
            current = summarize_spending(db, owner_id=owner_id, **month_args)["current"]
            complete = current["totals"]["is_complete"]
            months.append({"month": f"{first.year}-{month:02d}", "period": current["period"],
                "coverage": current["coverage"], "status": "complete" if complete else "incomplete",
                "totals": current["totals"] if complete else None,
                "observed_totals": current["totals"], "categories": current["categories"],
                "evidence": current["evidence"]})
        eligible = [row for row in months if row["totals"] is not None]
        maximum = max((row["totals"]["net_minor"] for row in eligible), default=None)
        result.update(months=months, complete_months=[row["month"] for row in eligible],
            missing_months=[row["month"] for row in months if row["totals"] is None],
            ranking_scope="full_year" if len(eligible) == 12 else "complete_months_only",
            highest_months=[{"month": row["month"], "net_minor": maximum} for row in eligible if row["totals"]["net_minor"] == maximum])
    return result


def inspect_anomalies(db: Session, *, owner_id: str, start: str | None = None, end: str | None = None,
                      account_id: str | None = None, category: str | None = None) -> dict:
    # Reuse ownership/date/coverage validation, but scan all posted purchases,
    # not just the paginated evidence. Refunds and transfers are not candidates.
    summary = summarize_spending(db, owner_id=owner_id, start=start, end=end,
        account_id=account_id, category=category, comparison_mode="none")
    period = summary["current"]["period"]
    query = select(Transaction).where(Transaction.owner_id == owner_id,
        Transaction.account_id.in_(summary["account_ids"]), Transaction.status == "posted",
        Transaction.type == "purchase", Transaction.posted_at_utc >= period["start_utc"],
        Transaction.posted_at_utc < period["end_utc"])
    if category == "未分类":
        query = query.where(Transaction.category.is_(None))
    elif category and category != "全部":
        query = query.where(Transaction.category == category)
    rows = db.scalars(query.order_by(Transaction.posted_at_utc, Transaction.id)).all()
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
