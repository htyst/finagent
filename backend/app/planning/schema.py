"""Model plan asset: references are semantic, all dates/amounts resolved by server."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from app.agent.registry import TOOL_MODELS
from app.agent.plans import validate_plan
from app.domain.errors import DomainError

class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
class TimeExpression(Strict):
    type: Literal['offset', 'period', 'recurring']
    unit: Literal['day', 'month'] | None = None
    n: StrictInt | None = Field(default=None, ge=-365, le=365)
    period: Literal['this_month', 'last_month', 'today', 'yesterday'] | None = None
    freq: Literal['monthly'] | None = None
    day: StrictInt | None = Field(default=None, ge=1, le=31)
class Emotion(Strict):
    state: Literal['neutral', 'urgent', 'frustrated', 'distressed'] = 'neutral'
    confidence: float = Field(default=1.0, ge=0, le=1)
    evidence: str = Field(default='', max_length=200)
class Params(Strict):
    source: str | None = None
    target: str | None = None
    amount_yuan: str | None = Field(default=None, pattern=r'^[0-9]{1,10}(\.[0-9]{1,2})?$')
    category: Literal['餐饮', '购物', '交通', '未分类', '全部'] | None = None
    account: str | None = None
    card: str | None = None
    action: Literal['freeze', 'unfreeze'] | None = None
    time: TimeExpression | None = None
    q: str | None = None
    task_id: str | None = Field(default=None, max_length=120)
class Condition(Strict):
    step_ref: str
    field: Literal['net_minor']
    op: Literal['gt', 'lt']
    value: str = Field(pattern=r'^[0-9]{1,10}(\.[0-9]{1,2})?$')
class Step(Strict):
    id: str = Field(min_length=1, max_length=60)
    tool: Literal['list_accounts', 'list_transactions', 'summarize_spending', 'list_recipients',
                  'list_cards', 'prepare_transfer', 'prepare_card_action', 'get_task_status']
    params: Params = Field(default_factory=Params)
    depends_on: list[str] = Field(default_factory=list)
    condition: Condition | None = None
    recurrence: TimeExpression | None = None
    on_failure: Literal['stop', 'continue_independent_readonly'] = 'stop'
class Plan(Strict):
    steps: list[Step] = Field(default_factory=list, max_length=12)
    recurrence: TimeExpression | None = None
    emotion: Emotion = Field(default_factory=Emotion)
    clarification: str | None = Field(default=None, max_length=1000)

def violation(rule, field, observed, expected, hint):
    return dict(rule_id=rule, field=field, observed=observed, expected=expected, hint=hint)
class PlanInvalid(Exception):
    def __init__(self, violations):
        self.violations = violations
        super().__init__('计划校验失败')

def validate_asset(raw):
    try:
        plan = Plan.model_validate(raw)
    except ValueError as error:
        errors = error.errors() if hasattr(error, 'errors') else []
        raise PlanInvalid([violation('P_SCHEMA', '.'.join(map(str,e['loc'])), e['type'],
                                    'Plan JSON Schema', '按字段类型与白名单修复。') for e in errors]) from error
    if not plan.steps:
        # A clarification is dialogue, not an executable plan. Do not force a
        # model to invent a read/write step just to ask for missing information.
        if plan.clarification and plan.clarification.strip():
            return plan
        raise PlanInvalid([violation('P_SCHEMA','steps',[],
                           '至少一个步骤或明确追问','空计划必须包含有意义的澄清问题。')])
    ids = [s.id for s in plan.steps]
    errors = []
    if len(set(ids)) != len(ids):
        errors.append(violation('P_DAG', 'steps.id', ids, '唯一ID', '每一步使用不同ID。'))
    by_id = {s.id:s for s in plan.steps}
    ordered, visiting, visited = [], set(), set()
    def visit(s):
        if s.id in visiting:
            raise ValueError('循环依赖')
        if s.id in visited:
            return
        visiting.add(s.id)
        for dep in s.depends_on:
            if dep not in by_id:
                raise ValueError('未知依赖')
            visit(by_id[dep])
        visiting.remove(s.id)
        visited.add(s.id)
        ordered.append(s)
    try:
        for s in plan.steps:
            visit(s)
    except ValueError as error:
        errors.append(violation('P_DAG','steps.depends_on',str(error),'无环已知依赖','修复依赖边。'))
    if errors:
        raise PlanInvalid(errors)
    ancestors = {}
    for s in ordered:
        ancestors[s.id] = set(s.depends_on)
        for dep in s.depends_on:
            ancestors[s.id].update(ancestors[dep])
        if s.tool.startswith('prepare_') and not any(not by_id[d].tool.startswith('prepare_') for d in ancestors[s.id]):
            errors.append(violation('P_READ_BEFORE_WRITE',f'steps.{s.id}.depends_on',s.depends_on,
                                    '至少一个前置读步骤','先查询相关账户或卡片再准备写操作。'))
        if s.condition and (s.tool != 'prepare_transfer' or s.condition.step_ref not in ancestors[s.id]
                            or by_id[s.condition.step_ref].tool != 'summarize_spending'):
            errors.append(violation('P_CONDITION',f'steps.{s.id}.condition',s.condition.model_dump(),
                                    '依赖消费统计结果','条件必须引用前置消费统计步骤。'))
        for expression in (s.params.time, s.recurrence, plan.recurrence):
            if not expression:
                continue
            fields = expression.model_dump(exclude_none=True)
            valid = ((expression.type == 'offset' and expression.unit in {'day','month'} and expression.n is not None
                      and set(fields) <= {'type','unit','n'}) or
                     (expression.type == 'period' and expression.period is not None and set(fields) <= {'type','period'}) or
                     (expression.type == 'recurring' and expression.freq == 'monthly' and set(fields) <= {'type','freq','day'}))
            if not valid or ((s.recurrence or plan.recurrence) and expression == (s.recurrence or plan.recurrence) and expression.type != 'recurring'):
                errors.append(violation('P_TIME',f'steps.{s.id}.time',fields,'结构化时间表达式','时间字段类型与参数必须一致。'))
        if s.recurrence and s.tool != 'prepare_transfer':
            errors.append(violation('P_RECURRENCE',f'steps.{s.id}.recurrence',s.tool,
                                    'prepare_transfer','周期字段只能附在转账写步骤或计划顶层。'))
    if (plan.recurrence or any(s.recurrence for s in ordered)) and sum(s.tool.startswith('prepare_') for s in ordered) != 1 and not plan.clarification:
        errors.append(violation('P_RECURRENCE','recurrence','多个或零写步骤','唯一周期转账','周期计划一次只登记一笔转账。'))
    if errors:
        raise PlanInvalid(errors)
    # Legacy whitelist/structure validation remains the common lower-level gate.
    ordinals = {s.id:i for i,s in enumerate(ordered,1)}
    validate_plan([{'ordinal':i,'tool_name':s.tool,'depends_on':[ordinals[d] for d in s.depends_on],
                    'on_failure':s.on_failure} for i,s in enumerate(ordered,1)], max_writes=12)
    plan.steps = ordered
    return plan
