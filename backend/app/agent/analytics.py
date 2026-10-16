"""Ground explicit dates and read-query references in both model modes."""
from __future__ import annotations
import calendar
import re
from datetime import date, timedelta
from app.data.models import DemoSetting
from app.domain.clock import parse_day, selected_range, previous_full_month_range
from app.domain.errors import DomainError

INTENTS = {"spending", "transactions", "statement", "anomalies"}
FOLLOW = ("那", "呢", "同一个", "这次", "把它", "全年", "哪几个", "凭什么", "盗刷", "依据")


def explicit_intent(text: str, previous: dict) -> str | None:
    if re.search(r"(?:生成|出具|给我|做).*(?:月度|年度|月报|年报).*?(?:报告|账单|月报|年报)|(?:月度|年度)账单报告", text):
        return "statement"
    if re.search(r"异常交易|异常.*流水|检查.*异常", text):
        return "anomalies"
    if previous.get("intent") == "anomalies" and re.search(r"凭什么|盗刷|依据|为什么.*异常", text):
        return "anomalies"
    if previous.get("intent") == "statement" and re.search(r"哪几个月|全年净消费", text):
        return "statement"
    if "比较" in text and previous.get("args", {}).get("start") and any(word in text for word in ("把它", "刚才", "上面", "这份")):
        return "spending"
    return None


def _explicit_span(text: str, demo: date, previous: dict) -> tuple[str, str] | None:
    iso = re.findall(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text)
    if iso:
        if len(iso) != 2:
            raise DomainError("INVALID_INPUT", "请同时给出开始和结束日期，格式为 YYYY-MM-DD")
        selected_range(*iso)
        return tuple(iso)
    base_year = parse_day(previous["start"]).year if previous.get("start") else demo.year
    match = re.search(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日(?:至|到|—|-)(?:(?:(\d{4})年)?(\d{1,2})月)?(\d{1,2})日", text)
    try:
        if match:
            year, month, first, end_year, end_month, last = match.groups()
            year, month = int(year or base_year), int(month)
            return date(year, month, int(first)).isoformat(), date(int(end_year or year), int(end_month or month), int(last)).isoformat()
        match = re.search(r"(?:(\d{4})年)?(\d{1,2})月", text)
        if match:
            year, month = int(match[1] or base_year), int(match[2])
            return date(year, month, 1).isoformat(), date(year, month, calendar.monthrange(year, month)[1]).isoformat()
        match = re.search(r"(\d{4})年", text)
        if match and re.search(r"年度|全年|年报", text):
            year = int(match[1])
            return date(year, 1, 1).isoformat(), date(year, 12, 31).isoformat()
    except (ValueError, OverflowError) as exc:
        raise DomainError("INVALID_INPUT", "无效日期") from exc
    return None


def resolve_read(db, task, intent: str, text: str, context: dict, decision=None):
    previous_query = context.get("last_query", {})
    previous = previous_query.get("args", {})
    follow = (decision.follow_up if decision else False) or any(word in text for word in FOLLOW)
    allowed = {"start", "end", "account_id", "category"}
    args = {key: value for key, value in previous.items() if key in allowed} if follow else {}
    category = next((c for c in ("餐饮", "购物", "交通", "未分类") if c in text), None)
    category = category or (decision.category if decision else None)
    if category:
        args["category"] = None if category == "全部" else category
    if "各类" in text or "全部" in text or intent == "statement" and not follow:
        args["category"] = None
    demo = parse_day(db.get(DemoSetting, 1).demo_date)
    span = _explicit_span(text, demo, previous)
    period = decision.period if decision else None
    if not span and ("上个月" in text or "上月" in text or period == "last_month"):
        value = previous_full_month_range(demo)
        span = value.start.isoformat(), (value.end_exclusive - timedelta(days=1)).isoformat()
    if not span and any(word in text for word in ("本月", "这个月")) or not span and period == "this_month":
        args.pop("start", None); args.pop("end", None)
    elif not span and (period in {"today", "yesterday", "last_7_days"} or "昨天" in text or "今天" in text):
        end = demo - timedelta(days=1) if period == "yesterday" or "昨天" in text else demo
        span = (end - timedelta(days=6) if period == "last_7_days" else end).isoformat(), end.isoformat()
    elif not span and decision and (decision.start or decision.end):
        if not decision.start or not decision.end:
            return None, {}, "请提供完整的开始和结束日期。"
        span = decision.start, decision.end
    if span:
        selected_range(*span)
        args.update(start=span[0], end=span[1])
    compare = "比较" in text and follow and previous.get("start") and span
    if intent == "spending":
        args["comparison_mode"] = "none" if args.get("start") else "previous_corresponding"
        if compare:
            args.update(comparison_mode="custom", compare_start=previous["start"], compare_end=previous["end"])
        elif decision and decision.compare_start and decision.compare_end:
            args.update(comparison_mode="custom", compare_start=decision.compare_start, compare_end=decision.compare_end)
        name = "summarize_spending"
    elif intent == "transactions":
        name = "list_transactions"
    elif intent == "anomalies":
        name = "inspect_anomalies"
    else:
        kind = "annual" if re.search(r"年度|全年|年报", text) else "monthly" if re.search(r"月度|月报", text) else previous.get("report_kind") or (decision.report_kind if decision else None)
        if not args.get("start") or not kind:
            return None, {}, "请说明要生成哪一年或哪一月的账单报告。"
        args.update(report_kind=kind, comparison_mode="none")
        name = "generate_statement"
    if decision and decision.source:
        from app.agent.language import _match
        from app.data.models import Account
        from sqlalchemy import select
        rows = db.scalars(select(Account).where(Account.owner_id == task.owner_id)).all()
        matched = _match(decision.source, rows)
        if len(matched) != 1:
            return None, {}, "要查哪个账户？可选：" + "、".join(row.alias for row in rows) + "。"
        args["account_id"] = matched[0].id
    if intent in {"spending", "statement"}:
        args["evidence_limit"] = 100
    context["last_query"] = {"intent": intent, "args": args.copy()}
    return name, args, None
