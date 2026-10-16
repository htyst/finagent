"""Versioned evaluation input schema, with disjoint 30/10 splits."""
from __future__ import annotations
import json
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from app.config import ROOT

class EvaluationCase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^[A-Z][0-9]{2}$")
    user: Literal["u1", "u2", "u3"]
    scenario: Literal["billing", "transfer", "card", "crossflow", "boundary"]
    initial_state: dict[str, str]
    button_events: list[str] = Field(max_length=0)
    expected_business_results: Literal[0]
    forbidden: list[str]
    messages: list[str] = Field(min_length=1)
    expected_status: str
    tool: str | None
    contains: str
    amount_minor: int | None = None
    target_mask: str | None = None
    intent: str | None = None
    step_statuses: list[str] | None = None

def load_cases(split):
    groups = {name: [EvaluationCase.model_validate(json.loads(line)).model_dump(exclude_none=True)
                    for line in (ROOT / "evals" / f"tasks.{name}.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
              for name in ("dev", "holdout")}
    assert len(groups["dev"]) == 30 and len(groups["holdout"]) == 10, "Expected 30 dev + 10 holdout cases"
    all_cases = groups["dev"] + groups["holdout"]
    assert len({case["id"] for case in all_cases}) == 40, "Duplicate case IDs"
    assert len({(case["user"], tuple(case["messages"])) for case in all_cases}) == 40, "Duplicate conversations"
    from collections import Counter
    assert Counter(case["scenario"] for case in all_cases) == {"billing":8, "transfer":10, "card":8, "crossflow":10, "boundary":4}
    return groups[split]
