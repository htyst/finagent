"""Only read and prepare tools exposed to the agent; no commit or token tool."""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy.orm import Session

from app.domain.conditions import SpendingCondition, TransactionCardCondition
from app.domain.errors import DomainError
from app.domain.operations import get_task, prepare_card_action, prepare_transfer

class StrictArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

class Empty(StrictArgs):
    pass

class ServiceRecords(StrictArgs):
    kind: Literal['replace_card','credit_limit','cancel_contract'] | None = None

class CreditCards(StrictArgs):
    card: str | None = Field(default=None,min_length=1,max_length=100)

class WealthProducts(StrictArgs):
    product_id: str | None = Field(default=None,max_length=40)

class PlanningBasis(StrictArgs):
    lookback_months: StrictInt = Field(default=3, ge=1, le=12)

class Recipients(StrictArgs):
    q: str | None = None

class Transactions(StrictArgs):
    start: str | None = None
    end: str | None = None
    account_id: str | None = None
    card_id: str | None = None
    category: str | None = None
    merchant: str | None = Field(default=None,max_length=100)
    type: str | None = None
    direction: Literal['debit','credit'] | None = None
    status: Literal['posted','pending','void'] | None = None
    sort: Literal['date_desc','date_asc','amount_desc','amount_asc'] = 'date_desc'
    limit: StrictInt = Field(default=20,ge=1,le=100)
    offset: StrictInt = Field(default=0,ge=0)

class History(StrictArgs):
    table: Literal['credit_card_statements','liability_events','monthly_financial_snapshots','monthly_summary','investment_valuations','deposits','liabilities','insurance_policies','auto_debit_mandates','subscription_billing_events']
    start: str | None = None
    end: str | None = None
    card_id: str | None = None
    liability_id: str | None = None
    subscription_id: str | None = None
    status: str | None = None
    limit: StrictInt = Field(default=50,ge=1,le=100)
    offset: StrictInt = Field(default=0,ge=0)

class SubscriptionRead(StrictArgs):
    start: str | None = None
    end: str | None = None
    subscription_id: str | None = None
    status: Literal['paid','failed','blocked'] | None = None
    contract_limit: StrictInt = Field(default=50,ge=1,le=100)
    contract_offset: StrictInt = Field(default=0,ge=0)
    limit: StrictInt = Field(default=50,ge=1,le=100)
    offset: StrictInt = Field(default=0,ge=0)

class Analysis(StrictArgs):
    kind: Literal['expense_by_merchant','cashflow','largest_expenses','daily_expenses']
    start: str | None = None
    end: str | None = None
    account_id: str | None = None
    card_id: str | None = None
    category: str | None = None
    merchant: str | None = Field(default=None,max_length=100)
    top_n: StrictInt = Field(default=10,ge=1,le=50)

class Spending(StrictArgs):
    card_id: str | None = None
    merchant: str | None = Field(default=None,max_length=100)
    start: str | None = None
    end: str | None = None
    account_id: str | None = None
    category: str | None = None
    comparison_mode: Literal['none','previous_corresponding','previous_full_month','custom'] | None = None
    compare_start: str | None = None
    compare_end: str | None = None
    evidence_limit: StrictInt = 20
    evidence_offset: StrictInt = 0

class Statement(Spending):
    report_kind: Literal["monthly", "annual"]

class Anomalies(StrictArgs):
    card_id: str | None = None
    start: str | None = None
    end: str | None = None
    account_id: str | None = None
    category: str | None = None

class OwnTarget(StrictArgs):
    type: Literal["own_account"]
    account_id: str

class SavedTarget(StrictArgs):
    type: Literal["saved_recipient"]
    recipient_id: str

from app.domain.direct_recipient import ExternalTarget

class Transfer(StrictArgs):
    source_account_id: str
    target: OwnTarget | SavedTarget | ExternalTarget
    amount_minor: StrictInt
    currency: Literal["CNY"] = "CNY"
    note: str | None = None
    condition: SpendingCondition | None = None
    task_id: str | None = None

class CardAction(StrictArgs):
    card_id: str
    action: Literal["freeze", "unfreeze"]
    condition: TransactionCardCondition | None = None
    task_id: str | None = None

class TaskStatus(StrictArgs):
    task_id: str

class AgentStatus(StrictArgs):
    task_id: str | None = None

class Knowledge(StrictArgs):
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=3, ge=1, le=4)

class WealthTrade(StrictArgs):
    kind: Literal['buy', 'redeem']
    product_id: str
    account_id: str
    amount_minor: StrictInt | None = None
    units_hundredths: StrictInt | None = None

class CardManagement(StrictArgs):
    kind: Literal['apply', 'limit', 'online_block', 'online_unblock', 'loss', 'unloss']
    card_id: str | None = None
    account_id: str | None = None
    limit_minor: StrictInt | None = None
    applicant: str | None = None

class Subscription(StrictArgs):
    kind: Literal['reminder', 'revoke']
    authorization_id: str
    days_before: StrictInt = 2
    hour: StrictInt = 9
    minute: StrictInt = 0

class Birthday(StrictArgs):
    kind: Literal['create', 'change', 'cancel', 'cancel_item', 'retry']
    terms: dict
    plan_id: str | None = None

class Schedule(StrictArgs):
    source_account_id: str
    target: OwnTarget | SavedTarget
    amount_minor: StrictInt
    execute_at_utc: str
    timezone_name: Literal['Asia/Shanghai'] = 'Asia/Shanghai'
    currency: Literal['CNY'] = 'CNY'
    note: str | None = None

class Collection(StrictArgs):
    destination_account_id: str
    total_minor: StrictInt
    recipient_ids: list[str]
    title: str = 'AA收款'

TOOL_MODELS = {
    'read_service_requests':ServiceRecords,
    'read_credit_cards':CreditCards,
    'read_tool_catalog':Empty,'read_wealth_products':WealthProducts,'read_risk_assessment':Empty,
    'read_investment_performance':Empty,'read_birthday_offers':Empty,
    'financial_planning_basis': PlanningBasis,
    'read_financial_profile': Empty, 'read_financial_history': History, 'analyze_finances': Analysis,
    'read_agent_status': AgentStatus, 'list_schedules': Empty, 'list_collections': Empty,
    'search_knowledge': Knowledge,
    "list_accounts": Empty, "list_transactions": Transactions,
    "summarize_spending": Spending, "generate_statement": Statement, "inspect_anomalies": Anomalies, "list_recipients": Recipients,
    "list_cards": Empty, "prepare_transfer": Transfer,
    "prepare_card_action": CardAction, "get_task_status": TaskStatus,
    'read_wealth': Empty, 'prepare_wealth': WealthTrade,
    'read_subscriptions': SubscriptionRead, 'identify_subscriptions': Empty, 'prepare_subscription': Subscription,
    'read_birthdays': Empty, 'prepare_birthday': Birthday,
    'read_card_management': Empty, 'prepare_card_management': CardManagement,
    'prepare_schedule': Schedule, 'prepare_collection': Collection,
}
DESCRIPTIONS = {
    'financial_planning_basis': '本人财务规划底稿：可用活期排除定期、冻结和预留；最近1至12个完整月的全量消费/收入/净现金流与分类习惯。提供facts.key用于工作流计算，不读取受保护订阅合同。默认3个月，不把不完整月算进平均。',
    'read_financial_history': '分页查询本人信用卡账单、负债事件、13个月末资产快照、基金估值、保单等；支持日期与卡筛选。订阅相关表仍需黄色确认。',
    'analyze_finances': '全量服务端计算按商户汇总、每日支出、最大N笔或收入/现金流。不要从列表前几条自行求总和；支持现金与信用卡消费。',
    'read_financial_profile': '只读本人完整金融档案：实时银行存款、导入定期/基金/信用卡/贷款/保险及净资产。非银行余额明确标导入快照，不含订阅（订阅查询需独立黄色确认）。',
    'read_agent_status': '只读当前会话的指定任务、草稿与持久操作单回执，不新建交易。task_id空时当前任务。',
    'list_schedules': '读取本人预约转账状态，不授权或执行。',
    'list_collections': '读取本人AA收款状态，不发起或代他人付款。',
    'search_knowledge': '检索版本化本地模拟项目知识，不能代替实时银行接口。',
    'read_wealth': '读取本人模拟产品、风险评估和持仓。',
    'prepare_wealth': '准备模拟申购或赎回，执行必须由页面授权。',
    'read_subscriptions': '读取本人完整订阅合同、试用/涨价/失败、全年实付及自动续费折月汇总，并分页查扣费证据；须先黄色查询确认。',
    'identify_subscriptions': '识别本人疑似订阅，须先完成黄色查询确认。',
    'prepare_subscription': '准备提醒或撤销代扣，不执行。',
    'read_birthdays': '读取本人的模拟生日安排。',
    'prepare_birthday': '准备固定预算与商品的生日安排，不预留或付款。',
    'read_card_management': '读取本人的卡片限制和办理记录。',
    'prepare_card_management': '准备卡片办理申请，不改变卡状态。',
    'prepare_schedule': '准备一次性固定预约，不授权执行。',
    'prepare_collection': '准备AA收款草稿，不模拟付款。',
    "generate_statement": "只读生成月度或年度账单，包括覆盖情况、分类、逐月汇总和并列排名。",
    "inspect_anomalies": "依据透明规则筛查异常交易线索；不认定盗刷，不修改卡片。",
    "list_accounts": "列出当前演示用户的账户。",
    "list_transactions": "查询当前用户的模拟流水。",
    "summarize_spending": "精确统计当前用户的消费、退款和证据。",
    "list_recipients": "列出当前用户的预置收款人，保留同名对象。",
    "list_cards": "列出当前用户的卡片和状态。",
    "prepare_transfer": "只准备固定模拟转账操作单，不扣款，不确认。",
    "prepare_card_action": "只准备卡片冻解操作单，不改变卡状态。",
    "get_task_status": "读取当前用户已有任务和回执。",
}
from app.agent.tool_catalog import READ_DESCRIPTIONS
DESCRIPTIONS.update(READ_DESCRIPTIONS)

LIVE_WRITE_SCHEMAS = {
    "prepare_transfer": {"type": "object", "additionalProperties": False,
        "properties": {"source_account_id": {"type": "string"},
                       "target_type": {"type": "string", "enum": ["own_account", "saved_recipient"]},
                       "target_id": {"type": "string"}, "amount_minor": {"type": "integer"}},
        "required": ["source_account_id", "target_type", "target_id", "amount_minor"]},
    "prepare_card_action": {"type": "object", "additionalProperties": False,
        "properties": {"card_id": {"type": "string"},
                       "action": {"type": "string", "enum": ["freeze", "unfreeze"]}},
        "required": ["card_id", "action"]},
}

def tool_schemas(names: list[str] | None = None) -> list[dict]:
    names = names or list(TOOL_MODELS)
    if any(name not in TOOL_MODELS for name in names):
        raise ValueError("unknown tool")
    return [{"type": "function", "function": {"name": name, "strict": True,
             "description": DESCRIPTIONS[name], "parameters": LIVE_WRITE_SCHEMAS.get(name, TOOL_MODELS[name].model_json_schema())}}
            for name in names]

def validate(name: str, arguments: dict) -> dict:
    if name not in TOOL_MODELS:
        raise DomainError("INVALID_TOOL", "不允许的工具", 400)
    return TOOL_MODELS[name].model_validate(arguments).model_dump()

def execute(db: Session, *, session_id: str, owner_id: str, name: str, arguments: dict, agent_task_id: str | None = None) -> dict | list:
    from app.domain.operations import _session, _now
    session, _ = _session(db, session_id, _now())
    if owner_id != session.owner_id:
        raise DomainError('RESOURCE_NOT_FOUND', '用户资源不匹配', 404)
    if agent_task_id:
        from app.data.models import AgentTask
        task = db.get(AgentTask, agent_task_id)
        if not task or task.owner_id != owner_id or task.session_id != session_id or task.dataset_version != session.dataset_version:
            raise DomainError('RESOURCE_NOT_FOUND', '任务不属于当前用户会话', 404)
        from app.agent.control import assert_running
        assert_running(db, agent_task_id)
    from app.agent.sandbox import validate_envelope
    validate_envelope(name, arguments)
    args = validate(name, arguments)
    if name in READ_DESCRIPTIONS:
        from app.agent.tool_catalog import read
        return read(db,session_id,name,args)
    # Import here so the HTTP layer and the registry do not form an import cycle.
    from app import main
    if name == 'financial_planning_basis':
        from app.domain.planning_basis import read
        return read(db, owner_id, **args)
    if name == 'read_financial_profile':
        from app.domain.financial_profile import portfolio
        return portfolio(db, owner_id)
    if name == 'search_knowledge':
        from app.agent.knowledge import search
        return search(**args)
    if name == 'read_agent_status':
        from app.agent.service import _task, view
        target = _task(db, session, args['task_id'] or agent_task_id)
        result = view(db, target)
        return {key: result[key] for key in ('id', 'intent', 'status', 'proposal', 'plan_version',
            'schedule', 'collection', 'wealth_order', 'card_request', 'subscription_request', 'birthday_request') if key in result}
    if name == 'list_schedules':
        from app.domain.scheduled_transfers import list_schedules
        return list_schedules(db, session_id=session_id)
    if name == 'list_collections':
        from app.domain.collections import list_collections
        return list_collections(db, session_id=session_id)
    if name == 'read_wealth':
        from app.domain.wealth import portfolio
        from app.domain.financial_profile import rows
        return {**portfolio(db,session_id=session_id),'imported_positions':rows(db,owner_id,'investment_holdings'),'imported_positions_read_only':True}
    if name == 'prepare_wealth':
        from app.domain.wealth import prepare_order
        return prepare_order(db, session_id=session_id, agent_task_id=agent_task_id, **args)
    if name == 'read_financial_history':
        from app.domain.financial_profile import read_history
        return read_history(db,owner_id,session_id=session_id,**args)
    if name == 'analyze_finances':
        from app.domain.financial_analysis import analyze
        return analyze(db,owner_id,**args)
    if name == 'read_subscriptions':
        from app.domain.subscription_read import report
        return report(db,session_id,**args)
    if name == 'identify_subscriptions':
        from app.domain.subscriptions import identify
        from app.domain.subscription_read import report
        return {'candidates':identify(db,session_id),'confirmed':report(db,session_id)}
    if name == 'prepare_subscription':
        from app.domain.subscriptions import prepare
        return prepare(db, session_id=session_id, agent_task_id=agent_task_id, **args)
    if name == 'read_birthdays':
        from app.domain.birthdays import get_portfolio
        return get_portfolio(db, session_id)
    if name == 'prepare_birthday':
        from app.domain.birthdays import prepare
        return prepare(db, session_id=session_id, agent_task_id=agent_task_id, **args)
    if name == 'read_card_management':
        from app.domain.card_management import portfolio
        return portfolio(db, session_id)
    if name == 'prepare_card_management':
        from app.domain.card_management import prepare
        return prepare(db, session_id=session_id, agent_task_id=agent_task_id, **args)
    if name == 'prepare_schedule':
        from app.domain.scheduled_transfers import prepare_schedule
        return prepare_schedule(db, session_id=session_id, agent_task_id=agent_task_id, **args)
    if name == 'prepare_collection':
        from app.domain.collections import prepare_collection
        return prepare_collection(db, session_id=session_id, agent_task_id=agent_task_id, **args)
    if name == "list_accounts":
        return main.accounts(owner_id=owner_id, db=db)["data"]
    if name == "list_cards":
        return main.cards(owner_id=owner_id, db=db)["data"]
    if name == "list_recipients":
        return main.recipients(q=args["q"] or "", owner_id=owner_id, db=db)["data"]
    if name == "list_transactions":
        return main.transactions(**args, owner_id=owner_id, db=db)["data"]
    if name == "summarize_spending":
        return main.spending(**args, owner_id=owner_id, db=db)["data"]
    if name == "generate_statement":
        from app.domain.statement import generate_statement
        return generate_statement(db, owner_id=owner_id, **args)
    if name == "inspect_anomalies":
        from app.domain.statement import inspect_anomalies
        return inspect_anomalies(db, owner_id=owner_id, **args)
    if name == "prepare_transfer":
        return prepare_transfer(db, session_id=session_id, agent_task_id=agent_task_id, **args)
    if name == "prepare_card_action":
        card_id = args.pop("card_id")
        return prepare_card_action(db, session_id=session_id, card_id=card_id, agent_task_id=agent_task_id, **args)
    return get_task(db, session_id=session_id, task_id=args["task_id"])
