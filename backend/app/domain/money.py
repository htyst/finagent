from __future__ import annotations

import re
from decimal import Decimal

from app.domain.errors import DomainError

MAX_MINOR = 2**63 - 1
SINGLE_TRANSFER_LIMIT_MINOR = 500_000
DAILY_TRANSFER_LIMIT_MINOR = 1_000_000
_YUAN_PATTERN = re.compile(r"^(0|[1-9][0-9]*)(\.[0-9]{1,2})?$")


def validate_minor(value: object, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainError("INVALID_INPUT", "金额必须是整数分")
    if value < 0 or (value == 0 and not allow_zero) or value > MAX_MINOR:
        raise DomainError("INVALID_INPUT", "金额超出允许范围")
    return value


def yuan_to_minor(value: str | int | Decimal, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or isinstance(value, float):
        raise DomainError("INVALID_INPUT", "请使用精确十进制金额")
    text = str(value)
    if not _YUAN_PATTERN.fullmatch(text):
        raise DomainError("INVALID_INPUT", "金额须为最多两位小数的非负十进制数")
    minor = int(Decimal(text) * 100)
    return validate_minor(minor, allow_zero=allow_zero)


def require_cny(currency: str) -> None:
    if currency != "CNY":
        raise DomainError("UNSUPPORTED_CURRENCY", "仅支持人民币 CNY")


def validate_transfer_amount(amount_minor: object, daily_spent_minor: int = 0) -> int:
    amount = validate_minor(amount_minor)
    if amount > SINGLE_TRANSFER_LIMIT_MINOR or daily_spent_minor + amount > DAILY_TRANSFER_LIMIT_MINOR:
        raise DomainError("LIMIT_EXCEEDED", "超过演示转账额度")
    return amount
