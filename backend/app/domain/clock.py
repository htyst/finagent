from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from app.domain.errors import DomainError

SHANGHAI = ZoneInfo("Asia/Shanghai")
_DATE_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


@dataclass(frozen=True)
class Clock:
    demo_date: date
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    def system_now(self) -> datetime:
        instant = self.now()
        if instant.tzinfo is None:
            raise ValueError("system clock must be timezone-aware")
        return instant.astimezone(timezone.utc)

    def business_now(self, hour: int = 12, minute: int = 0) -> datetime:
        return datetime.combine(self.demo_date, time(hour, minute), SHANGHAI)


@dataclass(frozen=True)
class DateRange:
    start: date
    end_exclusive: date

    @property
    def start_utc(self) -> str:
        return local_midnight_utc(self.start)

    @property
    def end_utc(self) -> str:
        return local_midnight_utc(self.end_exclusive)

    def as_dict(self) -> dict:
        return {"start_date": self.start.isoformat(), "end_exclusive_date": self.end_exclusive.isoformat(),
                "start_utc": self.start_utc, "end_utc": self.end_utc, "timezone": "Asia/Shanghai"}


def local_midnight_utc(day: date) -> str:
    try:
        return datetime.combine(day, time.min, SHANGHAI).astimezone(timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, ValueError) as exc:
        raise DomainError("INVALID_INPUT", "日期超出支持范围") from exc


def parse_day(value: str) -> date:
    if not _DATE_PATTERN.fullmatch(value):
        raise DomainError("INVALID_INPUT", "日期格式须为 YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise DomainError("INVALID_INPUT", "无效日期") from exc


def selected_range(start: str, end_inclusive: str) -> DateRange:
    first, last = parse_day(start), parse_day(end_inclusive)
    if first > last:
        raise DomainError("INVALID_INPUT", "开始日期不能晚于结束日期")
    try:
        return DateRange(first, last + timedelta(days=1))
    except OverflowError as exc:
        raise DomainError("INVALID_INPUT", "结束日期超出支持范围") from exc


def current_month_range(demo_date: date) -> DateRange:
    return DateRange(demo_date.replace(day=1), demo_date + timedelta(days=1))


def shifted_month(day: date, months: int, desired_day: int | None = None) -> date:
    month_index = day.year * 12 + day.month - 1 + months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    return date(year, month, min(desired_day or day.day, calendar.monthrange(year, month)[1]))


def previous_corresponding_range(demo_date: date) -> DateRange:
    previous = shifted_month(demo_date, -1)
    return DateRange(previous.replace(day=1), previous + timedelta(days=1))


def previous_full_month_range(demo_date: date) -> DateRange:
    this_month = demo_date.replace(day=1)
    previous = shifted_month(this_month, -1, 1)
    return DateRange(previous, this_month)


def last_night_range(demo_date: date) -> tuple[str, str]:
    previous = demo_date - timedelta(days=1)
    start = datetime.combine(previous, time(18, 0), SHANGHAI)
    end = datetime.combine(demo_date, time(6, 0), SHANGHAI)
    return (start.astimezone(timezone.utc).isoformat(timespec="seconds"),
            end.astimezone(timezone.utc).isoformat(timespec="seconds"))
