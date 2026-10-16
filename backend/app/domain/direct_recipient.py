"""User-entered one-time receiving details, independent of the saved directory."""
from __future__ import annotations
import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator
from app.domain.errors import DomainError

class ExternalTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["external_account"] = "external_account"
    name: str = Field(min_length=1, max_length=80)
    bank_name: str = Field(min_length=1, max_length=80)
    account_number: str = Field(min_length=6, max_length=50)

    @field_validator("name", "bank_name")
    @classmethod
    def label(cls, value):
        value=value.strip()
        if not value or any(ord(c)<32 for c in value):
            raise ValueError("请填写收款户名和开户银行")
        return value

    @field_validator("account_number")
    @classmethod
    def account(cls, value):
        value=re.sub(r"[\s-]", "", value).upper()
        if not re.fullmatch(r"[A-Z0-9]{6,34}", value):
            raise ValueError("请填写完整收款账号，不能只填尾号")
        return value

def validated_target(value):
    from pydantic import ValidationError
    try:return ExternalTarget.model_validate(value).model_dump()
    except ValidationError as error:
        raise DomainError("INVALID_RECIPIENT_DETAILS", "请填写收款户名、开户银行和完整收款账号；无需预先收录收款人",422) from error

def extract_details(text, previous=None):
    details=dict(previous or {}); explicit=False
    patterns={"name":r"(?:收款人|收款户名|收款姓名)(?:是|为|[:：])?\s*([^，,。；;]{1,80})",
        "bank_name":r"(?:开户银行|收款银行)(?:是|为|[:：])?\s*([^，,。；;]{1,80})|银行[:：]\s*([^，,。；;]{1,80})",
        "account_number":r"(?:收款账号|收款账户|对方账号|银行账号)(?:是|为|[:：])?\s*([^，,。；;]{1,50})"}
    for key,pattern in patterns.items():
        match=re.search(pattern,text)
        if match:
            details[key]=next(v for v in match.groups() if v is not None).strip()
            if key!="name":explicit=True
    return details,explicit
