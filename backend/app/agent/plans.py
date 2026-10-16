from __future__ import annotations
import json
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from app.agent.registry import TOOL_MODELS
from app.domain.errors import DomainError

# Preconditions are semantic: a write must depend on a matching business read.
PRECONDITIONS = {
    'prepare_transfer': {'list_accounts', 'summarize_spending'},
    'prepare_card_action': {'list_cards', 'read_card_management', 'list_transactions'},
    'prepare_wealth': {'read_wealth'}, 'prepare_card_management': {'read_card_management'},
    'prepare_subscription': {'read_subscriptions'}, 'prepare_birthday': {'read_birthdays'},
    'prepare_schedule': {'list_accounts'}, 'prepare_collection': {'list_recipients'},
}
REFERENCE_TYPES = {
    'list_transactions': {'selected.card_id': 'card_id', 'selected.account_id': 'account_id'},
    'summarize_spending': {'condition': 'condition'},
    'list_accounts': {'selected.id': 'account_id'}, 'list_cards': {'selected.id': 'card_id'},
    'list_recipients': {'selected.id': 'recipient_id'},
    'read_wealth': {'selected.product_id': 'product_id'},
    'read_subscriptions': {'selected.id': 'authorization_id'},
    'read_birthdays': {'selected.id': 'plan_id'},
}

class Reference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    step: int
    path: str
    target: str | None = None

class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ordinal: int
    tool_name: str
    depends_on: list[int] = Field(default_factory=list)
    input_refs: list[Reference] = Field(default_factory=list)
    on_failure: Literal["stop", "continue_independent_readonly"] = "stop"
    params: dict | None = None
    goal_id: str | None = None

def validate_plan(plan: list[dict], goals: list[dict] | None = None) -> list[dict]:
    if not isinstance(plan, list) or not 1 <= len(plan) <= 12:
        raise DomainError('INVALID_PLAN', '计划须为1至12步的数组')
    try:
        steps = [PlanStep.model_validate(item) for item in plan]
    except (ValueError, TypeError) as error:
        raise DomainError("INVALID_PLAN", "计划结构无效") from error
    if not 1 <= len(steps) <= 12:
        raise DomainError("INVALID_PLAN", "计划须为 1 至 12 步")
    seen = set()
    writes = 0
    for index, step in enumerate(steps, 1):
        if step.ordinal != index or step.tool_name not in TOOL_MODELS:
            raise DomainError("INVALID_PLAN", "计划含未知工具或无序步骤")
        if any(dep not in seen for dep in step.depends_on):
            raise DomainError("INVALID_PLAN", "依赖只能指向之前的步骤")
        if any(ref.step not in step.depends_on for ref in step.input_refs):
            raise DomainError("INVALID_PLAN", "结果引用缺少有序依赖")
        ancestors, pending = set(), list(step.depends_on)
        while pending:
            dep = pending.pop()
            if dep not in ancestors:
                ancestors.add(dep)
                pending.extend(steps[dep - 1].depends_on)
        required = PRECONDITIONS.get(step.tool_name, set())
        # Legacy conditional templates use an explicit condition/transaction
        # evidence read. Generic model plans must read the actual bank resource.
        if goals is not None:
            required = {'prepare_transfer': {'list_accounts'},
                        'prepare_card_action': {'list_cards', 'read_card_management'}}.get(step.tool_name, required)
        if required and not any(steps[dep - 1].tool_name in required for dep in ancestors):
            raise DomainError('PLAN_PRECONDITION', '写步骤缺少相应业务读取前置条件')
        for ref in step.input_refs:
            source = steps[ref.step - 1].tool_name
            kind = REFERENCE_TYPES.get(source, {}).get(ref.path)
            compatible = {'account_id': {'account_id', 'source_account_id', 'destination_account_id'},
                          'card_id': {'card_id'}, 'recipient_id': {'target.recipient_id'},
                          'condition': {'condition'}, 'product_id': {'product_id'},
                          'authorization_id': {'authorization_id'}, 'plan_id': {'plan_id'}}
            if kind is None or (ref.target and ref.target not in compatible[kind]) or (goals is not None and not ref.target):
                raise DomainError('PLAN_REFERENCE_TYPE', '计划结果引用路径或类型不匹配')
        if step.params is not None:
            try:
                TOOL_MODELS[step.tool_name].model_validate(step.params)
            except ValueError as error:
                raise DomainError('PLAN_PARAMETERS', '计划业务参数无效') from error
        if goals is not None and (step.params is None or step.goal_id is None):
            raise DomainError('PLAN_PARAMETERS', '统一计划必须携带业务参数和目标编号')
        writes += step.tool_name.startswith("prepare_")
        seen.add(index)
    if writes > 1:
        raise DomainError("INVALID_PLAN", "一个计划最多一个写操作")
    if goals is not None:
        expected = {goal['id']: goal for goal in goals}
        if len(expected) != len(goals):
            raise DomainError('PLAN_GOAL_COVERAGE', '目标编号重复')
        covered = set()
        for step in steps:
            goal = expected.get(step.goal_id)
            if goal is None:
                raise DomainError('PLAN_GOAL_COVERAGE', '计划增加了用户未要求的目标')
            if step.tool_name == goal['tool']:
                normalized = TOOL_MODELS[step.tool_name].model_validate(step.params).model_dump()
                verified = TOOL_MODELS[step.tool_name].model_validate(goal['params']).model_dump()
                if normalized != verified:
                    raise DomainError('PLAN_CONSTRAINT_MISMATCH', '计划参数、条件或对象不符原始目标')
                covered.add(step.goal_id)
            elif step.tool_name not in PRECONDITIONS.get(goal['tool'], set()) or step.tool_name.startswith('prepare_'):
                raise DomainError('PLAN_GOAL_COVERAGE', '计划含无关业务步骤')
        if covered != set(expected):
            raise DomainError('PLAN_GOAL_COVERAGE', '计划遗漏了用户目标')
    return [step.model_dump() for step in steps]


def check_references(step, source_results):
    """References can corroborate grounded parameters, never invent new ones."""
    for reference in step.get('input_refs', []):
        expected = step['params']
        try:
            for part in reference['target'].split('.'):
                expected = expected[part]
            result = source_results[reference['step']]
            path = reference['path']
            if path.startswith('selected.'):
                field = path.split('.', 1)[1]
                candidates = result if isinstance(result, list) else result.get('items', [])
                matched = [row[field] for row in candidates if isinstance(row, dict) and row.get(field) == expected]
                if len(matched) != 1:
                    raise KeyError('ambiguous or missing selected evidence')
                actual = matched[0]
            else:
                actual = result
                for part in path.split('.'):
                    actual = actual[part]
            if actual != expected:
                raise KeyError('reference differs from grounded user parameter')
        except (KeyError, TypeError, AttributeError) as error:
            raise DomainError('PLAN_REFERENCE_VALUE', '结果引用缺失、歧义或不符原始参数，已停止') from error
