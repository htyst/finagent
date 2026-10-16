"""Semantic proposals only. None of these fields can grant banking authority."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class SemanticModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Goal(SemanticModel):
    text: str = Field(min_length=1, max_length=1000)
    kind: Literal['read', 'write', 'knowledge', 'unsupported']


class FieldSource(SemanticModel):
    field: Literal['source', 'target', 'target_note', 'amount_yuan', 'card', 'schedule_text',
                   'product', 'units', 'budget_yuan', 'birthday_date', 'collection_account']
    quote: str = Field(max_length=300)
    origin: Literal['current_user', 'prior_draft', 'unique_reference', 'inferred']


class ReadRequest(SemanticModel):
    name: str = Field(min_length=1, max_length=80)
    arguments_json: str = Field(default='{}', max_length=4000)


class DialogueTurn(SemanticModel):
    behavior: Literal['new_task', 'supplement', 'modify', 'insert_query', 'pause_query',
                      'cancel', 'pause', 'result_query', 'switch_topic', 'knowledge',
                      'unsupported', 'confirmation', 'continue']
    goals: list[Goal] = Field(default_factory=list, max_length=8)
    conditions: list[str] = Field(default_factory=list, max_length=8)
    prohibitions: list[str] = Field(default_factory=list, max_length=8)
    time_requirements: list[str] = Field(default_factory=list, max_length=8)
    clear_fields: list[Literal['source', 'target', 'target_note', 'amount_yuan', 'card',
                                'schedule_text', 'product', 'units']] = Field(default_factory=list, max_length=8)
    field_sources: list[FieldSource] = Field(default_factory=list, max_length=16)
    tools: list[ReadRequest] = Field(default_factory=list, max_length=6)
    needs_clarification: bool = False
    clarification: str | None = Field(default=None, max_length=300)
    evidence_refs: list[str] = Field(default_factory=list, max_length=12)
