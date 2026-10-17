"""User-entered synthetic cards and saved recipients; never a real bank binding."""
import hashlib
import re
from datetime import timedelta
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from sqlalchemy import select

from app.data.atomic import atomic_write
from app.data.card_models import CardControl
from app.data.models import Account, AuditEvent, Card, DataCoverage, Recipient
from app.domain.clock import local_midnight_utc, parse_day
from app.domain.errors import DomainError
from app.domain.ledger import owned_account
from app.domain.operations import _now, _session, _stamp
from app.domain.recipient_directory import mask_phone, normalize_phone


class DirectoryInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    @field_validator('*', mode='before')
    @classmethod
    def clean_text(cls, value):
        if isinstance(value, str):
            value = value.strip()
            if not value or any(ord(char) < 32 for char in value):
                raise ValueError('请填写有效资料')
        return value


class AddCard(DirectoryInput):
    alias: str = Field(min_length=1, max_length=80)
    bank_name: str = Field(min_length=1, max_length=80)
    card_number: str = Field(min_length=12, max_length=40)
    account_id: str | None = Field(default=None, min_length=1, max_length=100)
    account_alias: str | None = Field(default=None, min_length=1, max_length=80)
    opening_balance_minor: StrictInt = Field(default=0, ge=0, le=100000000000)

    @field_validator('card_number')
    @classmethod
    def card_digits(cls, value):
        value = re.sub(r'[ -]', '', value)
        if not re.fullmatch(r'[0-9]{12,19}', value):
            raise ValueError('请填写12至19位模拟卡号')
        return value


class AddRecipient(DirectoryInput):
    name: str = Field(min_length=1, max_length=80)
    bank_name: str = Field(min_length=1, max_length=80)
    account_number: str = Field(min_length=6, max_length=50)
    phone: str | None = Field(default=None, max_length=30)
    remark: str | None = Field(default=None, max_length=80)

    @field_validator('account_number')
    @classmethod
    def account_number_format(cls, value):
        value = re.sub(r'[ -]', '', value).upper()
        if not re.fullmatch(r'[A-Z0-9]{6,34}', value):
            raise ValueError('请填写完整的模拟收款账号')
        return value

    @field_validator('phone')
    @classmethod
    def phone_format(cls, value):
        if value is None:
            return None
        normalized = normalize_phone(value)
        if normalized is None:
            raise ValueError('手机号格式无效')
        return normalized


def fingerprint(owner, number):
    return hashlib.sha256((owner + ':' + number).encode()).hexdigest()


def recipient_view(row):
    return dict(id=row.id, name=row.name, bank_name=row.bank_name,
                account_mask='**' + row.account_last4, phone_mask=mask_phone(row.phone),
                remark=row.remark, status=row.status, version=row.version)


def add_card(db, session_id, payload: AddCard):
    with atomic_write(db):
        session, setting = _session(db, session_id, _now())
        digest = fingerprint(session.owner_id, payload.card_number)
        if db.scalar(select(Card.id).where(Card.owner_id == session.owner_id,
                                          Card.number_fingerprint == digest)):
            raise DomainError('CARD_ALREADY_EXISTS', '这张银行卡已添加，请刷新卡片列表', 409)
        if payload.account_id:
            if payload.opening_balance_minor or payload.account_alias:
                raise DomainError('INVALID_INPUT', '关联现有账户时不能修改账户名称或初始余额', 422)
            account = owned_account(db, session.owner_id, payload.account_id)
        else:
            alias = payload.account_alias or payload.alias + '账户'
            if db.scalar(select(Account.id).where(Account.owner_id == session.owner_id, Account.alias == alias)):
                raise DomainError('ACCOUNT_ALIAS_EXISTS', '账户名称已存在，请换一个名称或关联现有账户', 409)
            account = Account(id=uuid4().hex, owner_id=session.owner_id, alias=alias,
                account_last4=payload.card_number[-4:], currency='CNY', status='active', version=1,
                opening_balance_minor=payload.opening_balance_minor, balance_minor=payload.opening_balance_minor)
            db.add(account)
            db.flush()
            day = parse_day(setting.demo_date)
            db.add(DataCoverage(owner_id=session.owner_id, account_id=account.id,
                start_utc=local_midnight_utc(day), end_utc=local_midnight_utc(day + timedelta(days=1)),
                completeness='complete'))
        card = Card(id=uuid4().hex, owner_id=session.owner_id, account_id=account.id,
            alias=payload.alias, brand=payload.bank_name, card_last4=payload.card_number[-4:],
            number_fingerprint=digest, status='active', version=1)
        db.add(card)
        db.flush()
        db.add(CardControl(card_id=card.id, limit_minor=100000, online_enabled=1, lost=0, version=1))
        db.add(AuditEvent(id=uuid4().hex, owner_id=session.owner_id, event='demo_card_added', created_at_utc=_stamp(_now())))
        return dict(id=card.id, alias=card.alias, brand=card.brand, card_mask='**' + card.card_last4,
                    account_id=account.id, status=card.status, simulated=True)


def add_recipient(db, session_id, payload: AddRecipient):
    with atomic_write(db):
        session, _ = _session(db, session_id, _now())
        digest = fingerprint(session.owner_id, payload.account_number)
        if db.scalar(select(Recipient.id).where(Recipient.owner_id == session.owner_id,
                                               Recipient.number_fingerprint == digest)):
            raise DomainError('RECIPIENT_ALREADY_EXISTS', '该收款账号已保存，请查看收款人列表', 409)
        row = Recipient(id=uuid4().hex, owner_id=session.owner_id, name=payload.name,
            bank_name=payload.bank_name, account_last4=payload.account_number[-4:],
            number_fingerprint=digest, phone=payload.phone, remark=payload.remark, status='active', version=1)
        db.add(row)
        db.add(AuditEvent(id=uuid4().hex, owner_id=session.owner_id, event='recipient_added', created_at_utc=_stamp(_now())))
        return recipient_view(row)
