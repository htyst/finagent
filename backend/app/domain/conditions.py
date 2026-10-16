"""Restricted, server-evaluated conditions; never evaluate model expressions."""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy.orm import Session
from app.data.models import Transaction, Card
from app.domain.errors import DomainError
from app.domain.spending import summarize_spending
from app.domain.clock import selected_range

class SpendingCondition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["spending_gt", "spending_lt"]
    start: str
    end: str
    account_id: str | None = None
    category: str
    threshold_minor: StrictInt = Field(ge=0, le=1000000000)

class TransactionCardCondition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["transaction_card"]
    transaction_id: str
    card_id: str
    account_id: str
    card_last4: str = Field(pattern=r"^[0-9]{4}$")
    amount_minor: StrictInt = Field(gt=0)
    posted_at_utc: str

def normalize_condition(value: dict | None, kind: str) -> dict | None:
    if value is None:
        return None
    model = SpendingCondition if kind == "transfer" else TransactionCardCondition
    try:
        result = model.model_validate(value).model_dump()
    except ValueError as error:
        raise DomainError("INVALID_CONDITION", "不支持的条件结构") from error
    if kind == "transfer":
        selected_range(result["start"], result["end"])
    return result

def evaluate_condition(db: Session, owner_id: str, condition: dict) -> dict:
    if condition["type"] in {"spending_gt", "spending_lt"}:
        result = summarize_spending(db, owner_id=owner_id, account_id=condition.get("account_id"),
                                    category=condition["category"], start=condition["start"],
                                    end=condition["end"], comparison_mode="none")
        complete = result["current"]["totals"]["is_complete"]
        net = result["current"]["totals"]["net_minor"]
        met = complete and (net > condition["threshold_minor"] if condition["type"] == "spending_gt"
                            else net < condition["threshold_minor"])
        return {"met": met, "complete": complete,
                "error_code": None if met else "CONDITION_NOT_MET" if complete else "DATA_INCOMPLETE",
                "evidence": result, "condition": condition}
    tx = db.get(Transaction, condition["transaction_id"])
    card = db.get(Card, condition["card_id"])
    met = bool(tx and card and tx.owner_id == owner_id and card.owner_id == owner_id
               and tx.type == "purchase" and tx.status == "posted"
               and tx.card_id == card.id and tx.account_id == card.account_id == condition["account_id"]
               and card.card_last4 == condition["card_last4"]
               and tx.amount_minor == condition["amount_minor"]
               and tx.posted_at_utc == condition["posted_at_utc"])
    return {"met": met, "complete": True, "condition": condition,
            "error_code": None if met else "TRANSACTION_CHANGED",
            "evidence": {"transaction_id": condition["transaction_id"], "association_valid": met}}
