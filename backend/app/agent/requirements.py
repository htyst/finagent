"""Typed semantic drafts and deterministic business guards.

The language model can propose user-provided facts, never permissions, identities,
or execution. Both live and mock drafts pass the same validation before an owner
scoped directory and a domain service are allowed to create anything.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
MAX_MINOR = 2**63 - 1
SELF = {"我", "本人", "自己", "我自己"}
NUMBER = r"[0-9零〇一二两三四五六七八九十百千万]+(?:[.点][0-9零〇一二两三四五六七八九]+)?"
MONEY_NUMBER = r"(?:[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{1,2})?|" + NUMBER + r")"
MONEY = re.compile(r"(?<![0-9.])(" + MONEY_NUMBER + r")\s*(?:元|块(?:钱)?)")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RequirementIssue(StrictModel):
    code: str
    field: str
    message: str


class RequirementShare(StrictModel):
    participant: str = Field(min_length=1, max_length=80)
    amount_minor: int = Field(ge=0, le=MAX_MINOR)


class ExecutionConstraints(StrictModel):
    # Literal values deliberately reject a model-provided authorization flag.
    requires_confirmation: Literal[True] = True
    execute_once: Literal[True] = True
    not_before_utc: str | None = None
    no_execution_requested: bool = False


class Requirement(StrictModel):
    kind: Literal["schedule_transfer", "aa_collection"]
    source: str | None = Field(default=None, max_length=80)
    target: str | None = Field(default=None, max_length=80)
    target_note: str | None = Field(default=None, max_length=80)
    amount_minor: int | None = Field(default=None, ge=0, le=MAX_MINOR)
    scheduled_at_utc: str | None = None
    schedule_text: str | None = Field(default=None, max_length=1000)
    time_zone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    total_minor: int | None = Field(default=None, ge=0, le=MAX_MINOR)
    participants: list[str] = Field(default_factory=list, max_length=100)
    payer: str | None = Field(default=None, max_length=80)
    people: int | None = Field(default=None, ge=1, le=100)
    collection_account: str | None = Field(default=None, max_length=80)
    share_mode: Literal["equal", "explicit"] | None = None
    shares: list[RequirementShare] = Field(default_factory=list, max_length=100)
    execution_constraints: ExecutionConstraints = Field(default_factory=ExecutionConstraints)
    extraction_issues: list[RequirementIssue] = Field(default_factory=list)


class RequirementValidation(StrictModel):
    ok: bool
    issues: list[RequirementIssue]
    normalized: Requirement


def _issue(code: str, field: str, message: str) -> RequirementIssue:
    return RequirementIssue(code=code, field=field, message=message)


def _now(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError("requirement clock must be timezone aware")
    return value.astimezone(timezone.utc)


def _chinese_integer(text: str) -> int | None:
    if text.isascii() and text.isdigit():
        return int(text)
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    units = {"十": 10, "百": 100, "千": 1000}
    if not text or any(c not in digits and c not in units and c != "万" for c in text):
        return None
    if all(c in digits for c in text):
        return int("".join(str(digits[c]) for c in text))
    total, section, current = 0, 0, 0
    for char in text:
        if char in digits:
            current = digits[char]
        elif char in units:
            section += (current or 1) * units[char]
            current = 0
        else:
            total += (section + current) * 10000
            section, current = 0, 0
    return total + section + current


def yuan_minor(text: str | None) -> int | None:
    """Parse stated decimal/Chinese amounts without binary floats or rounding."""
    if not text:
        return None
    text = text.strip()
    if "," in text:
        if not re.fullmatch(r"[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{1,2})?", text):
            return None
        text = text.replace(",", "")
    if re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", text):
        try:
            value = int(Decimal(text) * 100)
            return value if value <= MAX_MINOR else None
        except (InvalidOperation, ValueError):
            return None
    parts = re.split(r"点|\.", text)
    if len(parts) > 2:
        return None
    if re.search(r"[百千万][一二两三四五六七八九]$", parts[0]):
        return None  # colloquial trailing digits need clarification, never a guessed scale
    integer = _chinese_integer(parts[0])
    if integer is None:
        return None
    fraction = 0
    if len(parts) == 2:
        fractional = parts[1]
        mapping = dict(zip("零〇一二两三四五六七八九", "001223456789"))
        fractional = "".join(mapping.get(c, c) for c in fractional)
        if not re.fullmatch(r"[0-9]{1,2}", fractional):
            return None
        fraction = int(fractional.ljust(2, "0"))
    value = integer * 100 + fraction
    return value if value <= MAX_MINOR else None


def _unsupported(text: str, kind: str) -> list[RequirementIssue]:
    issues = []
    if re.search(r"[-−负]\s*" + NUMBER + r"\s*(?:元|块)", text):
        issues.append(_issue("invalid_amount", "amount_minor" if kind == "schedule_transfer" else "total_minor", "金额必须大于零，请核对正数金额。"))
    for number in re.findall(r"([0-9][0-9,.]*)\s*(?:元|块)", text):
        if "," in number and yuan_minor(number) is None:
            issues.append(_issue("invalid_amount", "amount_minor" if kind == "schedule_transfer" else "total_minor", "金额分组格式不明确，请用完整数字和最多两位小数。"))
    if kind == "schedule_transfer" and re.search(r"每(?:天|日|周|星期|月|年)|循环|重复预约|周期", text):
        issues.append(_issue("unsupported_recurrence", "schedule_text", "目前只支持一次预约，请明确单次日期和时间。"))
    if re.search(r"如果|前提|条件|仅当|只有.*才|若(?:是|有|无|超过|低于)|超过.*才|低于.*才|(?:余额|消费).*(?:大于|小于|高于|低于|超过)|到账后|付款后", text):
        issues.append(_issue("unsupported_condition", "execution_constraints", "预约和AA暂不能叠加条件，请单独说明这项业务。"))
    if kind == "schedule_transfer" and re.search(r"冻结|解冻|两笔|多笔|分别.*转|(?:同时|然后|再).*转", text):
        issues.append(_issue("unsupported_multiple_writes", "kind", "当前预约只支持一笔转账，请分别办理其他操作。"))
    if kind == "aa_collection" and re.search(r"转账|预约|定时|冻结|解冻|从[^。！？]*转|收款后.*转", text):
        issues.append(_issue("mixed_business", "kind", "AA收款不能混入付款、预约或卡片操作，请分别办理。"))
    if re.search(r"(?:美元|美金|欧元|日元|港币|英镑|USD|EUR|JPY|HKD)", text, re.I):
        issues.append(_issue("unsupported_currency", "amount_minor", "当前模拟业务只支持人民币CNY。"))
    return issues


def parse_schedule(text: str, *, now: datetime | None = None) -> tuple[str | None, list[RequirementIssue]]:
    """Resolve explicit calendar + clock against the real clock, never demo_date.

    Bare clock values deliberately do not imply a calendar day. The supported
    Chinese clock uses 24-hour values, with morning/afternoon/evening modifiers.
    An offset-aware ISO timestamp already supplies an unambiguous instant.
    """
    instant = _now(now)
    today = instant.astimezone(SHANGHAI).date()
    if re.search(r"纽约|伦敦|东京|美国时间|日本时间|时区(?![：: ]*(?:Asia/Shanghai|北京时间|中国标准时间))|GMT|UTC[+-]", text, re.I):
        return None, [_issue("unsupported_timezone", "time_zone", "请使用北京时间（Asia/Shanghai）说明预约时间。")]
    duration = re.findall(r"(" + NUMBER + r")\s*(秒|分钟|小时|天)后", text)
    if duration:
        if len(duration) != 1 or _has_time_words(re.sub(r"(" + NUMBER + r")\s*(?:秒|分钟|小时|天)后", "", text)):
            return None, [_issue("ambiguous_time", "schedule_text", "发现多个时间表达，请明确一个执行时间。")]
        amount = _chinese_integer(duration[0][0])
        if amount is None or not 0 < amount <= 366 * 86400:
            return None, [_issue("invalid_time", "schedule_text", "预约等待时长必须是明确的正整数。")]
        seconds = amount * {"秒": 1, "分钟": 60, "小时": 3600, "天": 86400}[duration[0][1]]
        if seconds > 366 * 86400:
            return None, [_issue("invalid_time", "schedule_text", "预约时间最多支持一年内。")]
        deadline = instant + timedelta(seconds=seconds)
        if deadline.microsecond:
            deadline = deadline.replace(microsecond=0) + timedelta(seconds=1)
        return deadline.isoformat(timespec="seconds"), []
    iso = re.findall(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?", text)
    if iso:
        if len(iso) != 1:
            return None, [_issue("ambiguous_time", "schedule_text", "发现多个预约时间，请只指定一个。")]
        if _has_time_words(text.replace(iso[0], "")):
            return None, [_issue("ambiguous_time", "schedule_text", "完整时间戳之外还有其他时间，请只指定一个。")]
        try:
            value = datetime.fromisoformat(iso[0].replace("Z", "+00:00"))
            if value.tzinfo is None:
                value = value.replace(tzinfo=SHANGHAI)
            return value.astimezone(timezone.utc).isoformat(timespec="seconds"), []
        except ValueError:
            return None, [_issue("invalid_time", "schedule_text", "预约日期或时间不合法，请核对。")]
    days: list[date] = []
    try:
        explicit = re.findall(r"(?<!\d)(\d{4})[-年](\d{1,2})[-月](\d{1,2})(?:日|号)?(?!\d)", text)
        for year, month, day in explicit:
            days.append(date(int(year), int(month), int(day)))
        if not explicit:
            for month, day in re.findall(r"(?<![\d年])(\d{1,2})月(\d{1,2})[日号]", text):
                days.append(date(today.year, int(month), int(day)))
        for word, offset in (("今天", 0), ("明天", 1), ("后天", 2), ("明早", 1), ("今晚", 0), ("今早", 0)):
            if word in text:
                days.append(today + timedelta(days=offset))
    except ValueError:
        return None, [_issue("invalid_date", "schedule_text", "预约日期不合法，请核对年月日。")]
    distinct = list(dict.fromkeys(days))
    if len(distinct) > 1:
        return None, [_issue("ambiguous_date", "schedule_text", "发现多个不同日期，请明确本次预约日期。")]
    clocks: list[tuple[int, int, int]] = []
    for prefix, hour, minute, second in re.findall(r"(上午|早上|早晨|凌晨|中午|下午|晚上|夜里|晚间|今晚|明早)?\s*(\d{1,2})[:：](\d{2})(?::(\d{2}))?", text):
        clocks.append(_clock(prefix, int(hour), int(minute), int(second or 0)))
    for prefix, hour, minute in re.findall(r"(上午|早上|早晨|凌晨|中午|下午|晚上|夜里|晚间|今晚|明早)?\s*([0-9零一二两三四五六七八九十]+)[点时](?:([0-9零一二两三四五六七八九十]+)分?)?", text):
        parsed_hour = _chinese_integer(hour)
        parsed_minute = _chinese_integer(minute) if minute else 0
        if parsed_hour is not None and parsed_minute is not None:
            clocks.append(_clock(prefix, parsed_hour, parsed_minute, 0))
    if re.search(r"[点时]半", text):
        clocks = [(h, 30, s) for h, _, s in clocks]
    elif re.search(r"[点时]一刻", text):
        clocks = [(h, 15, s) for h, _, s in clocks]
    elif re.search(r"[点时]三刻", text):
        clocks = [(h, 45, s) for h, _, s in clocks]
    if re.search(r"左右|大约|大概|前后|差不多", text):
        return None, [_issue("imprecise_time", "schedule_text", "预约需要明确时点，请说明准确日期和钟点。")]
    if len(clocks) > 1:
        return None, [_issue("ambiguous_clock", "schedule_text", "发现多个钟点，请明确本次执行时间。")]
    issues = []
    if not distinct:
        issues.append(_issue("missing_date", "schedule_text", "请补充预约日期，例如明天或2026-10-06。"))
    if not clocks:
        issues.append(_issue("missing_clock", "schedule_text", "请补充具体执行时间，例如上午9点或09:00。"))
    if issues:
        return None, issues
    try:
        value = datetime.combine(distinct[0], datetime.min.time()).replace(
            hour=clocks[0][0], minute=clocks[0][1], second=clocks[0][2], tzinfo=SHANGHAI)
        return value.astimezone(timezone.utc).isoformat(timespec="seconds"), []
    except ValueError:
        return None, [_issue("invalid_clock", "schedule_text", "预约钟点不合法，请核对小时和分钟。")]


def _clock(prefix: str, hour: int, minute: int, second: int) -> tuple[int, int, int]:
    if prefix in {"晚上", "夜里", "晚间", "今晚"} and hour == 12:
        # Midnight belongs to a different day; ask instead of guessing that day.
        hour = 24
    elif prefix in {"下午", "晚上", "夜里", "晚间", "今晚"} and 1 <= hour <= 11:
        hour += 12
    elif prefix == "中午" and 1 <= hour <= 10:
        hour += 12
    elif prefix == "凌晨" and hour == 12:
        hour = 0
    return hour, minute, second


def _normalize_person(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return "我" if value in SELF else value


def equal_shares(total_minor: int, participants: list[str]) -> list[RequirementShare]:
    """Integer cents; the user's own share receives the first remainder cent."""
    if not participants:
        return []
    names = [_normalize_person(item) for item in participants]
    names = (["我"] if "我" in names else []) + [item for item in names if item != "我"]
    base, remainder = divmod(total_minor, len(names))
    return [RequirementShare(participant=name, amount_minor=base + (index < remainder))
            for index, name in enumerate(names)]


def validate_requirement(requirement: Requirement, *, now: datetime | None = None) -> RequirementValidation:
    value = requirement.model_copy(deep=True)
    issues = list(value.extraction_issues)
    if value.kind == "schedule_transfer":
        for field, message in (("source", "请补充付款账户。"), ("amount_minor", "请补充转账金额。")):
            if getattr(value, field) is None:
                issues.append(_issue("missing_field", field, message))
        if not value.target and not value.target_note:
            issues.append(_issue("missing_field", "target", "请补充收款对象的姓名、手机号、尾号或保存的备注。"))
        if value.amount_minor is not None and value.amount_minor <= 0:
            issues.append(_issue("invalid_amount", "amount_minor", "转账金额必须大于零。"))
        if value.amount_minor is not None and value.amount_minor > 500000:
            issues.append(_issue("transfer_limit", "amount_minor", "超出当前模拟单笔转账限额5000元。"))
        if not value.scheduled_at_utc:
            if not any(issue.field == "schedule_text" for issue in issues):
                issues.append(_issue("missing_field", "schedule_text", "请补充预约日期和具体时间。"))
        else:
            try:
                scheduled = datetime.fromisoformat(value.scheduled_at_utc)
                if scheduled.tzinfo is None:
                    raise ValueError("naive datetime")
                if scheduled <= _now(now):
                    issues.append(_issue("past_time", "scheduled_at_utc", "预约时间必须晚于当前真实时间，请重新指定。"))
                value.scheduled_at_utc = scheduled.astimezone(timezone.utc).isoformat(timespec="seconds")
                value.execution_constraints.not_before_utc = value.scheduled_at_utc
            except ValueError:
                issues.append(_issue("invalid_time", "scheduled_at_utc", "预约必须是包含时区的完整时间。"))
        if value.participants or value.total_minor is not None or value.shares:
            issues.append(_issue("mixed_business", "kind", "预约转账不能混入AA收款需求，请分别办理。"))
    else:
        value.payer = _normalize_person(value.payer)
        names = [_normalize_person(item) for item in value.participants]
        if len(set(names)) != len(names):
            issues.append(_issue("duplicate_participant", "participants", "参与人重复，请逐人确认，不能重复收费。"))
        value.participants = (["我"] if "我" in names else []) + [item for item in names if item != "我"]
        if value.payer is None:
            issues.append(_issue("missing_field", "payer", "请明确谁垫付了这笔费用。"))
        elif value.payer != "我":
            issues.append(_issue("role_mismatch", "payer", "当前只能为本人垫付的费用发起AA收款。"))
        if len(value.participants) < 2 or "我" not in value.participants:
            issues.append(_issue("missing_participants", "participants", "请列出全部参与人，包括本人和其他付款成员。"))
        if value.people is not None and value.people != len(value.participants):
            issues.append(_issue("participant_count", "people", "人数和参与人名单不一致，请核对。"))
        if value.total_minor is None:
            issues.append(_issue("missing_field", "total_minor", "请补充垫付总金额。"))
        elif value.total_minor <= 0:
            issues.append(_issue("invalid_amount", "total_minor", "AA总金额必须大于零。"))
        if not value.collection_account:
            issues.append(_issue("missing_field", "collection_account", "请明确AA收款到账账户。"))
        if value.share_mode is None:
            issues.append(_issue("missing_field", "share_mode", "请明确是否由全部参与人平均分摊。"))
        expected = equal_shares(value.total_minor, value.participants) if value.total_minor is not None and value.participants else []
        if value.share_mode == "equal":
            value.shares = expected
        elif value.share_mode == "explicit":
            actual = {item.participant: item.amount_minor for item in value.shares}
            wanted = {item.participant: item.amount_minor for item in expected}
            if len(actual) != len(value.shares) or actual != wanted:
                issues.append(_issue("unsupported_split", "shares", "当前只支持平均分摊；所给成员金额与平均分摊不一致，请核对。"))
            if value.total_minor is not None and sum(item.amount_minor for item in value.shares) != value.total_minor:
                issues.append(_issue("share_total", "shares", "成员分摊金额之和必须等于垫付总金额。"))
        if any(item.amount_minor <= 0 for item in value.shares):
            issues.append(_issue("invalid_share", "shares", "每位参与人的分摊金额必须大于零。"))
        if value.scheduled_at_utc or value.source or value.target or value.target_note or value.amount_minor is not None:
            issues.append(_issue("mixed_business", "kind", "AA收款不能混入付款或预约要求，请分别办理。"))
    unique = list({(issue.code, issue.field, issue.message): issue for issue in issues}.values())
    return RequirementValidation(ok=not unique, issues=unique, normalized=value)


def banking_time_text(text: str) -> str:
    """Birthday dates describe the occasion, unless also attached to a bank action."""
    if re.search(r'生日(?:当天|那天|时)|届时|到时候|那天|当天',text):
        return text
    day=r'(?:大后天|后天|明天|今天|\d{4}-\d{2}-\d{2}|(?:\d{4}年)?\d{1,2}月\d{1,2}[日号])'
    # Never cross a clause boundary, account/action verb or clock expression.
    background=r'[^，,。；;！？?\n从转划拨预约定时点0-9]{0,24}'
    text=re.sub(day+r'(?='+background+r'(?:生日|过生日))','',text)
    return re.sub(r'(生日(?:是|在|为)?\s*)'+day,r'\1',text)


def detect_requirement_kind(text: str, previous_kind: str | None = None) -> str | None:
    text=banking_time_text(text)
    if re.search(r"(?<![A-Za-z0-9])AA(?![A-Za-z0-9])|(?:发起|向[^。！？]*|帮我[^。！？]*)收款|收款请求", text, re.I):
        return "aa_collection"
    if "垫付" in text and re.search(r"平分|均分|平摊|均摊|分摊|平均", text):
        return "aa_collection"
    timed = bool(re.search(r"预约|定时|明天|后天|明早|今晚|今早|下周|每周|每月|生日(?:当天|时)|届时|到时候|那天|"
                          r"[0-9一二两三四五六七八九十]+[点时]|\d{1,2}[:：]\d{2}|"
                          r"[0-9一二两三四五六七八九十]+(?:秒|分钟|小时|天)后", text))
    if not timed:
        calendar = re.findall(r"\d{4}-\d{2}-\d{2}|(?:\d{4}年)?\d{1,2}月\d{1,2}[日号]", text)
        conditioned = bool(re.search(r"如果|若|条件|才转|消费|餐饮|统计|净消费", text))
        timed = len(calendar) == 1 and not conditioned
    writing = bool(re.search(r"转账|转给|转到|转入|(?:从|给|向)[^。！？]*转", text))
    if timed and (writing or previous_kind == "schedule_transfer"):
        return "schedule_transfer"
    return previous_kind if previous_kind in {"schedule_transfer", "aa_collection"} else None


def _account_refs(text: str) -> tuple[str | None, str | None, str | None]:
    source, target, note = None, None, None
    found = re.search(r"从\s*([^，。；;]+?)(?:给|向|转(?:账)?|支付)", text)
    if found:
        source = found.group(1).strip()
    note_match = re.search(r"(?:备注(?:名)?(?:为|是|叫|[:：])?)\s*[‘’“\"]?([^，。；;‘’”\"]+?)(?:[‘’”\"]|的人|的收款人|转|收款|$)", text)
    if note_match:
        note = note_match.group(1).strip()
    found = re.search(r"(?:给|向)\s*([^，。；;]+?)(?:转(?:账)?|支付)", text)
    if found and note is None:
        target = found.group(1).strip().removeprefix("收款人")
    if target is None and note is None:
        found = re.search(r"(?:转(?:账)?(?:" + NUMBER + r"\s*(?:元|块))?\s*(?:到|给|入))\s*([^，。；;]+)", text)
        if found:
            target = re.sub(r"(" + NUMBER + r")\s*(?:元|块(?:钱)?)$", "", found.group(1)).strip()
    return source, target, note


def _has_time_words(text: str) -> bool:
    return bool(re.search(r"今天|明天|后天|明早|今晚|今早|\d{4}-\d{2}-\d{2}|\d{1,2}月\d{1,2}|\d{4}年|[0-9一二两三四五六七八九十]+[点时]|\d{1,2}[:：]\d{2}|下周|下月|每周|每月|[0-9一二两三四五六七八九十]+(?:秒|分钟|小时|天)后", text))


def _merge(value: Requirement, previous: Requirement | dict | None, *, supplied: set[str]) -> Requirement:
    if previous is None:
        return value
    old = previous if isinstance(previous, Requirement) else Requirement.model_validate(previous)
    if old.kind != value.kind:
        return value
    data = old.model_dump()
    latest = value.model_dump()
    for field in supplied:
        data[field] = latest[field]
    if "target_note" in supplied and latest["target_note"]:
        if "target" not in supplied:
            data["target"] = None
    elif "target" in supplied and latest["target"] and not re.fullmatch(r"(?:尾号\s*)?[0-9]{4}", latest["target"].strip()):
        data["target_note"] = None
    # Errors attached to the previous edited fields cannot silently survive a repair.
    data["extraction_issues"] = [issue for issue in data.get("extraction_issues", [])
                                 if issue["field"] not in supplied or issue["code"].startswith("unsupported_")
                                 or issue["code"] == "mixed_business"]
    data["extraction_issues"] += latest["extraction_issues"]
    if "total_minor" in supplied or "participants" in supplied:
        data["shares"] = latest["shares"] if "shares" in supplied else []
    return Requirement.model_validate(data)


def extract_mock(text: str, *, now: datetime | None = None, previous: Requirement | dict | None = None) -> Requirement | None:
    """Deterministic semantic extraction; no directory access and no side effects."""
    previous_kind = previous.kind if isinstance(previous, Requirement) else previous.get("kind") if previous else None
    kind = detect_requirement_kind(text, previous_kind)
    if kind is None:
        return None
    value = Requirement(kind=kind)
    supplied: set[str] = set()
    amounts = [yuan_minor(item) for item in MONEY.findall(text)]
    if amounts:
        amount_field = "amount_minor" if kind == "schedule_transfer" else "total_minor"
        per_member = kind == "aa_collection" and bool(re.search(r"每人|每位|每个成员|各自|(?:应付|承担)\s*" + NUMBER + r"\s*(?:元|块)", text))
        if len(amounts) == 1 and not per_member:
            setattr(value, amount_field, amounts[0])
            supplied.add(amount_field)
        elif len(amounts) == 1:
            pass  # Per-member money never silently overwrites the AA total.

        else:
            total = re.search(r"(?:总(?:额|共|金额)?|合计|聚餐|垫付)\s*(" + NUMBER + r")\s*(?:元|块)", text)
            if kind == "aa_collection" and total:
                value.total_minor = yuan_minor(total.group(1))
                supplied.add("total_minor")
            else:
                value.extraction_issues.append(_issue("ambiguous_amount", amount_field, "发现多个金额，请明确本次总额或转账金额。"))
    if kind == "schedule_transfer":
        source, target, note = _account_refs(text)
        for field, item in (("source", source), ("target", target), ("target_note", note)):
            if item:
                setattr(value, field, item)
                supplied.add(field)
        selected_tail = re.fullmatch(r"(?:选(?:择)?(?:收款人)?)?\s*尾号\s*([0-9]{4})[。！! ]*", text)
        if selected_tail and previous_kind == "schedule_transfer":
            value.target = "尾号" + selected_tail.group(1)
            supplied.add("target")
        if _has_time_words(text):
            value.schedule_text = text
            value.scheduled_at_utc, errors = parse_schedule(text, now=now)
            value.extraction_issues += errors
            supplied.update({"schedule_text", "scheduled_at_utc"})
        value.execution_constraints.no_execution_requested = bool(re.search(r"(?:先|暂时)?(?:不要|别|不)(?:执行|扣款)", text))
        if value.execution_constraints.no_execution_requested:
            supplied.add("execution_constraints")
    else:
        payer = re.search(r"(我自己|本人|自己|我)(?:是)?(?:先)?(?:垫付|付的钱|先付)", text)
        if payer:
            value.payer = "我"
            supplied.add("payer")
        else:
            payer = re.search(r"([^，。；;、 0-9元块]{1,12})(?:先)?垫付", text)
            if payer:
                value.payer = _normalize_person(payer.group(1).removeprefix("是"))
                supplied.add("payer")
        for clause in re.split(r"[，。；;]", text):
            if not re.search(r"[、和与及]", clause):
                continue
            if re.search(r"收款|转账|垫付|到账", clause):
                continue
            raw = re.sub(r"^(?:我们|参与人(?:是|为)?|包括|参加(?:的)?(?:有|是)?)[：:]?", "", clause.strip())
            raw = re.sub(r"[0-9一二两三四五六七八九十百]+(?:个)?人.*$", "", raw)
            raw = re.sub(r"(?:平分|均分|均摊|平均(?:分摊|分|摊)|分摊).*$", "", raw).strip()
            names = [_normalize_person(item.strip()) for item in re.split(r"[、和与及]", raw)]
            if len(names) >= 2 and all(item and len(item) <= 80 for item in names):
                value.participants = names
                supplied.add("participants")
                break
        people = re.search(r"([0-9一二两三四五六七八九十百]+)(?:个)?人(?:平分|均分|均摊|平均|分摊)?", text)
        status_question = bool(re.search(r"付款状态|支付状态|谁.*(?:付|交)|查看|查询|已收|待收", text)
                               and not re.search(r"改|调整|参与人数|总人数", text))
        if people and not status_question:
            value.people = _chinese_integer(people.group(1))
            supplied.add("people")
        account = re.search(r"(?:到账账户|收款账户|到账账号|收款账号)(?:为|是|用|选(?:择)?|设为|[:：])?\s*([^，。；;]+)", text)
        if account:
            ref = account.group(1).strip()
            value.collection_account = "账户" + ref if re.fullmatch(r"[A-Za-z0-9]+", ref) else ref
            supplied.add("collection_account")
        else:
            account = re.search(r"(?:到账|收款)(?:到|入|进)\s*([^，。；;]+)", text)
            if account:
                value.collection_account = account.group(1).strip()
                supplied.add("collection_account")
            else:
                account = re.search(r"(?:用|选择)\s*([^，。；;]+?)\s*(?:收款|到账)", text)
                if account:
                    value.collection_account = account.group(1).strip()
                    supplied.add("collection_account")
                elif previous_kind == "aa_collection" and re.fullmatch(r"(?:账户[A-Za-z0-9]+|活期(?:账户)?|储蓄(?:账户)?)[。！! ]*", text.strip()):
                    value.collection_account = text.strip("。！! ")
                    supplied.add("collection_account")
        if re.search(r"平分|均分|均摊|平均(?:分摊|分|摊)|每人(?:一样|相同)", text):
            value.share_mode = "equal"
            supplied.add("share_mode")
        explicit = re.findall(r"(?:^|[，,、；;])\s*([^，,、；; ]{1,12})(?:承担|分摊|应付|支付)\s*(" + NUMBER + r")\s*(?:元|块)", text)
        if explicit:
            value.share_mode = "explicit"
            value.shares = [RequirementShare(participant=_normalize_person(name), amount_minor=yuan_minor(amount))
                            for name, amount in explicit if yuan_minor(amount) is not None]
            supplied.update({"share_mode", "shares"})
    value.extraction_issues += _unsupported(text, kind)
    return _merge(value, previous, supplied=supplied)


def _grounded_reference(reference: str, text: str) -> bool:
    compact = re.sub(r"\s", "", text)
    ref = re.sub(r"\s", "", reference)
    if ref in compact:
        return True
    short = re.sub(r"^(?:收款人|收款对象|尾号)", "", ref)
    short = re.sub(r"账户$", "", short)
    if short and short in compact:
        return True
    if re.fullmatch(r"账户[A-Za-z0-9]+", ref):
        return re.search(r"(?<![A-Za-z0-9])" + re.escape(ref[2:]) + r"(?![A-Za-z0-9])", compact) is not None
    return False


def from_understanding(decision, *, text: str = "", now: datetime | None = None,
                       previous: Requirement | dict | None = None) -> Requirement:
    """Formalize semantics; raw facts and real time constrain model suggestions.

    Non-null tool fields are not evidence that the user provided them. Missing
    facts remain missing even if a plausible account/name was hallucinated.
    """
    value = Requirement(kind=decision.intent)
    supplied: set[str] = set()
    raw = extract_mock(text, now=now, previous=Requirement(kind=decision.intent))
    if raw and raw.kind != value.kind:
        value.extraction_issues.append(_issue("semantic_kind", "kind", "业务类型与原始要求不一致，请明确预约转账或AA收款。"))
        raw = None

    def assign_ref(field: str):
        item = getattr(decision, field, None)
        stated = getattr(raw, field, None) if raw else None
        if stated:
            setattr(value, field, stated)
            supplied.add(field)
        elif item is not None and _grounded_reference(item, text):
            setattr(value, field, item)
            supplied.add(field)
        # Unsupported guesses are ignored; previous facts are merged separately.

    amount_field = "amount_minor" if value.kind == "schedule_transfer" else "total_minor"
    stated_amount = getattr(raw, amount_field, None) if raw else None
    if stated_amount is not None:
        setattr(value, amount_field, stated_amount)
        supplied.add(amount_field)
        if decision.amount_yuan is not None and yuan_minor(decision.amount_yuan) != stated_amount:
            value.extraction_issues.append(_issue("semantic_amount", amount_field, "模型提取金额与原文不一致，请重新说明金额。"))
    if raw:
        value.extraction_issues += [issue for issue in raw.extraction_issues if issue.field != "schedule_text"]
    if value.kind == "schedule_transfer":
        for field in ("source", "target", "target_note"):
            assign_ref(field)
        if _has_time_words(text):
            value.schedule_text = text
            value.scheduled_at_utc, errors = parse_schedule(text, now=now)
            value.extraction_issues += errors
            supplied.update({"schedule_text", "scheduled_at_utc"})
        elif getattr(decision, "schedule_text", None):
            value.extraction_issues.append(_issue("ungrounded_time", "schedule_text", "未在本轮找到明确预约时间，请直接说明日期和钟点。"))
        if getattr(decision, "time_zone", None) not in {None, "Asia/Shanghai"}:
            value.extraction_issues.append(_issue("unsupported_timezone", "time_zone", "当前预约使用北京时间Asia/Shanghai。"))
        if getattr(decision, "participants", None) or getattr(decision, "shares_yuan", None) or getattr(decision, "collection_account", None):
            value.extraction_issues.append(_issue("mixed_business", "kind", "预约转账不能混入AA收款字段。"))
        value.execution_constraints.no_execution_requested = bool(re.search(r"(?:先|暂时)?(?:不要|别|不)(?:执行|扣款)", text))
        if value.execution_constraints.no_execution_requested:
            supplied.add("execution_constraints")
    else:
        assign_ref("collection_account")
        stated_payer = raw.payer if raw else None
        if stated_payer:
            value.payer = stated_payer
            supplied.add("payer")
        people = getattr(decision, "people", None)
        if raw and raw.people is not None:
            value.people = raw.people
            supplied.add("people")
        elif people is not None and re.search(r"(?<![0-9])" + str(people) + r"(?:个)?人", text):
            value.people = people
            supplied.add("people")
        if raw and raw.participants:
            value.participants = raw.participants
            supplied.add("participants")
        else:
            names = getattr(decision, "participants", None)
            old_names = previous.participants if isinstance(previous, Requirement) else previous.get("participants", []) if previous else []
            if names and not (len(names) == 1 and len(old_names) > 1) and all((_normalize_person(name) == "我" and any(ref in text for ref in SELF))
                             or (_normalize_person(name) != "我" and _grounded_reference(name, text)) for name in names):
                value.participants = names
                supplied.add("participants")
        if raw and raw.share_mode:
            value.share_mode = raw.share_mode
            supplied.add("share_mode")
            if raw.share_mode == "explicit":
                value.shares = raw.shares
                supplied.add("shares")
        if any(getattr(decision, field, None) for field in ("source", "target", "target_note", "schedule_text", "card", "action")):
            value.extraction_issues.append(_issue("mixed_business", "kind", "AA收款不能混入付款、预约或卡片字段。"))
    value.extraction_issues += _unsupported(text, value.kind)
    return _merge(value, previous, supplied=supplied)
