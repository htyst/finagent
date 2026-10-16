"""Two bounded stories. Recovery never asks a model to authorize or repeat a write."""
from __future__ import annotations
import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.agent.plans import validate_plan
from app.data.atomic import atomic_write, commit
from app.agent.provider import select_live_tool
from app.agent.registry import execute, validate
from app.data.models import Account, AgentStep, AgentTask, Card, DemoSetting, Proposal, Recipient, Transaction
from app.domain.clock import last_night_range, parse_day, previous_full_month_range, selected_range
from app.domain.conditions import evaluate_condition
from app.domain.errors import DomainError
from app.domain.ledger import owned_account
from app.domain.operations import (_end, _now, _proposal, _receipt, _session, _write,
                                   invalidate_agent_proposal)

INTENTS = {"investigate_card", "conditional_transfer"}
TERMINAL = {"completed", "partially_completed", "cancelled", "failed", "expired"}

def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def detect(text):
    if any(word in text for word in ("超过", "大于", "低于", "小于")) and "转" in text:
        return "conditional_transfer"
    if "昨晚" in text and "冻结" in text and any(word in text for word in ("余额", "陌生", "不认识")):
        return "investigate_card"
    return None

def rows(db, task):
    return db.scalars(select(AgentStep).where(AgentStep.task_id == task.id,
                      AgentStep.plan_version == task.plan_version).order_by(AgentStep.ordinal)).all()

def write_step(db, task):
    return next((row for row in rows(db, task) if row.tool_name.startswith("prepare_")), None)

def install_plan(db, task):
    if task.intent == "investigate_card":
        raw = [{"ordinal": 1, "tool_name": "list_transactions"},
               {"ordinal": 2, "tool_name": "prepare_card_action", "depends_on": [1],
                "input_refs": [{"step": 1, "path": "selected.card_id"}],
                "on_failure": "continue_independent_readonly"},
               {"ordinal": 3, "tool_name": "list_accounts", "depends_on": [1],
                "input_refs": [{"step": 1, "path": "selected.account_id"}]}]
        outcomes = ["identify_transaction", "freeze_if_tail_matches", "available_balance"]
    else:
        raw = [{"ordinal": 1, "tool_name": "summarize_spending"},
               {"ordinal": 2, "tool_name": "prepare_transfer", "depends_on": [1],
                "input_refs": [{"step": 1, "path": "condition"}]}]
        outcomes = ["transfer_if_spending_condition_met"]
        if json.loads(task.context_json).get("balance_account_id"):
            raw.append({"ordinal": 3, "tool_name": "list_accounts", "depends_on": [1, 2],
                        "on_failure": "continue_independent_readonly"})
            outcomes.append("available_balance_after_transfer_decision")
    plan = validate_plan(raw)
    task.plan_json = dump(plan)
    task.requested_outcomes_json = dump(outcomes)
    for item in plan:
        db.add(AgentStep(id=uuid4().hex, task_id=task.id, plan_version=task.plan_version,
                        ordinal=item["ordinal"], tool_name=item["tool_name"],
                        depends_on_json=dump(item["depends_on"]), on_failure=item["on_failure"],
                        args_json="{}", result_json="{}", status="pending",
                        created_at_utc=datetime.now(timezone.utc).isoformat()))
    commit(db)

def model_check(task, text, name, args):
    from app.agent.model_budget import SEMANTIC_DISPATCH
    if SEMANTIC_DISPATCH.get():
        validate(name, args)  # Already understood; deterministic grounding still mandatory.
        return
    if task.mode == "live":
        # Conditions are server-owned fixed structures, never model-authored expressions.
        expected = {key: value for key, value in args.items() if key != "condition"}
        select_live_tool(user_text=text, tool_name=name, expected_args=validate(name, expected))

def _balance(db, owner_id, account_id):
    from app.domain.ledger import available_balance
    account = owned_account(db, owner_id, account_id, active=False)
    return {"account_id": account.id, "alias": account.alias, "mask": "**"+account.account_last4,
            "available_balance_minor": available_balance(db, account), "currency": "CNY"}

def _apply_prepared(db, task_id, result):
    task = db.get(AgentTask, task_id)
    step = write_step(db, task)
    if result.get("proposal"):
        # The proposal/task linkage was committed in the preparation transaction.
        task.status = "awaiting_confirmation"
    elif result.get("already_in_state"):
        step.status = "succeeded"
        step.result_json = dump({"already_in_state": True, "message": "卡片已处于目标状态"})
    elif result.get("condition_met") is False:
        check = result["validation"]
        step.status = "skipped" if check["complete"] else "failed"
        step.result_json = dump({"validation": check, "error_code": check["error_code"]})
    commit(db)

def prepare(db, task, text, args):
    task_id = task.id
    step = write_step(db, task)
    step.args_json = dump(args)
    step.status = "running"
    task.status = "running_readonly"
    commit(db)
    try:
        model_check(task, text, step.tool_name, args)
        result = execute(db, session_id=task.session_id, owner_id=task.owner_id,
                         name=step.tool_name, arguments=args, agent_task_id=task.id)
        _apply_prepared(db, task_id, result)
    except DomainError as error:
        db.rollback()
        task = db.get(AgentTask, task_id)
        step = write_step(db, task)
        step.status = "failed"
        step.result_json = dump({"error_code": error.code, "message": error.message})
        commit(db)

def preparation_reply(db, task, pending_text):
    step = write_step(db, task)
    result = json.loads(step.result_json)
    if step.status == "failed":
        return "未能准备操作：" + result.get("message", result.get("error_code", "请查看步骤结果"))
    if step.status == "skipped":
        return "准备时复核条件不成立，已跳过写操作。"
    if step.status == "succeeded":
        return "卡片已处于目标状态，无需再次冻结；继续查询账户余额。"
    return pending_text


def parse_condition_update(db, task, text, previous):
    """Recognize the entire supported instruction; unknown clauses cannot be dropped."""
    context = previous.copy()
    remaining = text
    recognized = False
    number = r"([0-9]+(?:\.[0-9]{1,2})?)\s*元"
    def consume(pattern, apply):
        nonlocal remaining, recognized
        matches = list(re.finditer(pattern, remaining))
        if len(matches) > 1:
            raise DomainError("INVALID_CONDITION", "一次请只指定一个条件、金额或账户")
        if matches:
            apply(matches[0])
            remaining = re.sub(pattern, "", remaining)
            recognized = True
    def threshold(m):
        op = m.group(1)
        context["condition_type"] = ("spending_lt" if op in {"低于", "小于"} else
                                     context.get("condition_type", "spending_gt") if op.startswith("阈值") else "spending_gt")
        context["threshold_minor"] = int(Decimal(m.group(2)) * 100)
    consume(r"(?:严格)?(超过|大于|低于|小于|阈值改为|阈值改成)\s*" + number + r"(?:时)?", threshold)
    consume(r"(?:转账金额改成|转账金额改为|金额改成|金额改为|转账|转)\s*" + number,
            lambda m: context.update(amount_minor=int(Decimal(m.group(1)) * 100)))
    consume(r"餐饮|购物|交通|未分类", lambda m: context.update(category=m.group()))
    demo = parse_day(db.get(DemoSetting, 1).demo_date)
    def period(m):
        if m.group() in {"上个月", "上月"}:
            span = previous_full_month_range(demo)
            context.update(start=span.start.isoformat(), end=(span.end_exclusive-timedelta(days=1)).isoformat())
        else:
            context.update(start=demo.replace(day=1).isoformat(), end=demo.isoformat())
    consume(r"这个月|本月|上个月|上月", period)
    def dates(m):
        selected_range(m.group(1), m.group(2))
        context.update(start=m.group(1), end=m.group(2))
    consume(r"(\d{4}-\d{2}-\d{2})\s*(?:到|至)\s*(\d{4}-\d{2}-\d{2})(?:的)?", dates)
    def local_dates(m):
        year = int(m.group(1) or demo.year)
        month = int(m.group(2))
        try:
            start = date(year, month, int(m.group(3))).isoformat()
            end = date(year, int(m.group(4) or month), int(m.group(5))).isoformat()
        except ValueError as error:
            raise DomainError("INVALID_CONDITION", "无效日期，请说明真实的开始和结束日期") from error
        selected_range(start, end)
        context.update(start=start, end=end)
    consume(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})[日号]\s*(?:到|至)\s*(?:(\d{1,2})月)?(\d{1,2})[日号](?:的)?", local_dates)
    accounts = db.scalars(select(Account).where(Account.owner_id == task.owner_id)).all()
    recipients = db.scalars(select(Recipient).where(Recipient.owner_id == task.owner_id, Recipient.status != "one_time")).all()
    from app.agent.language import _match
    def names_for(items):
        labels = {label for row in items for label in
                  (getattr(row, "alias", None) or row.name,
                   (getattr(row, "alias", None) or row.name).removesuffix("账户"), row.account_last4)}
        return "|".join(re.escape(k) for k in sorted(labels, key=len, reverse=True) if k)
    names = names_for(accounts)
    def account(reference):
        matches = _match(reference, accounts)
        if len(matches) != 1:
            raise DomainError("INVALID_CONDITION", "账户不能唯一定位，请补充账户尾号")
        return matches[0]
    if names:
        consume(r"(?:最后|然后|再)(?:告诉我|查询|查一下)?\s*(" + names + r")\s*(?:当前)?(?:可用余额|余额|还剩多少)(?:元)?",
                lambda m: context.update(balance_account_id=account(m.group(1)).id))
        consume(r"从\s*(" + names + r")", lambda m: context.update(source_account_id=account(m.group(1)).id))
    def target(m):
        own = _match(m.group(1), accounts)
        saved = _match(m.group(1), recipients)
        matches = own + saved
        if len(matches) != 1:
            context.pop("target", None)
            context["target_choices"] = [row.account_last4 for row in matches]
        else:
            context.pop("target_choices", None)
            context["target"] = ({"type": "own_account", "account_id": own[0].id} if own else
                                 {"type": "saved_recipient", "recipient_id": saved[0].id})
    target_names = names_for(accounts + recipients)
    if target_names:
        consume(r"(?:转入|到|给|向|选尾号)\s*(" + target_names + r")", target)
    # An execution hold is a complete clause, not permission to discard a negated instruction.
    remaining = re.sub(r"(?:^|[，,。；;])\s*(?:先|暂时)?(?:不要|不|别)(?:执行|扣款)(?=$|[，,。；;！？!?])", "", remaining)
    remaining = re.sub(r"先算|先查询|先统计|算一下|统计|查询|日期是|类别是|净消费|如果|那么|只有|条件|增加|添加|加个|改成|改为|调整为|修改为|帮我|请|就|才|转账|转|当|消费|可以吗", "", remaining)
    remaining = re.sub(r"[\s，,。.!！：:；;？?]", "", remaining)
    if remaining or not recognized:
        raise DomainError("INVALID_CONDITION", "这次修改中有尚不支持或未说明清楚的条件。请重新说明日期、消费类别，以及严格大于或小于多少元；暂不支持等于、不超过、定时或多重条件")
    return context


def condition_status_question(text):
    # State queries must not consume a revision or invent a new transfer.
    return bool(re.search(r"当前|现在|刚才|这笔", text)
                and re.search(r"条件|金额", text)
                and re.search(r"什么|多少|解释|说明", text)
                and not re.search(r"改|换|增加|添加|超过|大于|低于|小于|\d+\s*元", text))


def condition_status_reply(db, task, current):
    context = json.loads(task.context_json)
    condition = context.get("condition")
    if not condition or "amount_minor" not in context:
        return "当前转账条件尚未说明完整，请重新说明日期、消费类别、阈值和金额；未执行转账。"
    comparison = "低于" if condition["type"] == "spending_lt" else "超过"
    reply = (f"当前条件：{condition['start']} 至 {condition['end']}，{condition['category']}净消费"
             f"严格{comparison} {condition['threshold_minor']/100:,.2f} 元；"
             f"转账金额 {context['amount_minor']/100:,.2f} 元。")
    proposal = current.get("proposal")
    if proposal and proposal["status"] == "executed" and proposal.get("receipt"):
        return reply + "该笔转账已执行，结果以回执为准。"
    if proposal and proposal["status"] == "pending_confirmation":
        return reply + "尚未执行；请核对操作单后点击确认，确认时会再次检查条件。"
    step = write_step(db, task)
    result = json.loads(step.result_json) if step else {}
    check = result.get("validation", {})
    if step and step.status == "skipped" and check.get("complete") and check.get("met") is False:
        return reply + "上次核验条件不满足，未执行转账。"
    return reply + "未执行转账；请查看任务状态，继续办理需要重新准备并核对操作单。"


def promote_transfer(db, session, task):
    from app.agent.service import _task, view, _stamp
    from app.agent.language import _match
    from app.data.models import Recipient
    task_id, session_id = task.id, session.id
    with atomic_write(db):
        session, _ = _session(db, session_id, _now())
        task = _task(db, session, task_id)
        current = view(db, task)
        if task.stop_requested or current["status"] in TERMINAL:
            raise DomainError("INVALID_TASK_STATE", "任务已结束，请创建新任务", 409)
        context = json.loads(task.context_json)
        fixed = json.loads(db.get(Proposal, task.proposal_id).params_json) if task.proposal_id else context
        inherited = {key: fixed[key] for key in ("source_account_id", "target", "amount_minor") if key in fixed}
        slots = context.get("dialogue_slots", {})
        accounts = db.scalars(select(Account).where(Account.owner_id == task.owner_id)).all()
        source = _match(slots.get("source"), accounts)
        own = _match(slots.get("target"), accounts)
        other = _match(slots.get("target"), db.scalars(select(Recipient).where(Recipient.owner_id == task.owner_id, Recipient.status != "one_time")).all())
        if len(source) == 1:
            inherited.setdefault("source_account_id", source[0].id)
        if len(own) + len(other) == 1:
            inherited.setdefault("target", {"type": "own_account", "account_id": own[0].id} if own else {"type": "saved_recipient", "recipient_id": other[0].id})
        if slots.get("amount_yuan"):
            inherited.setdefault("amount_minor", int(Decimal(slots["amount_yuan"]) * 100))
        if task.proposal_id:
            invalidate_agent_proposal(db, session_id=session.id, proposal_id=task.proposal_id)
        task.proposal_id = None
        task.context_json = dump(inherited)
        task.intent = "conditional_transfer"
        task.status = "gathering_info"
        task.plan_version += 1
        task.plan_json = "[]"
        task.updated_at_utc = _stamp()
    return task


def _conditional(db, task, text, context):
    # This context has passed full-instruction validation before the old proposal
    # is replaced. Missing values are never guessed from an unrelated query.
    if "target" not in context and context.get("target_account_id"):
        context["target"] = {"type": "own_account", "account_id": context["target_account_id"]}
    required = ["start", "end", "category", "threshold_minor", "amount_minor", "source_account_id", "target"]
    task.context_json = dump(context)
    commit(db)
    if context.get("target_choices"):
        return "找到多个同名收款对象，请选尾号：" + "、".join(context["target_choices"]) + "。"
    if any(key not in context for key in required):
        return "请说明消费日期和类别、严格大于或小于多少元、付款账户、转账金额和收款账户。"
    condition = {"type": context.get("condition_type", "spending_gt"), "start": context["start"], "end": context["end"],
                 "category": context["category"], "account_id": None, "threshold_minor": context["threshold_minor"]}
    read = rows(db, task)[0]
    query = {"start": condition["start"], "end": condition["end"], "category": condition["category"], "comparison_mode": "none"}
    model_check(task, text, "summarize_spending", query)
    evaluation = evaluate_condition(db, task.owner_id, condition)
    read.args_json = dump(query)
    read.status = "succeeded"
    read.result_json = dump(evaluation)
    context["condition"] = condition
    task.context_json = dump(context)
    commit(db)
    if not evaluation["met"]:
        step = write_step(db, task)
        step.status = "skipped" if evaluation["complete"] else "failed"
        step.result_json = dump({"validation": evaluation, "error_code": evaluation["error_code"]})
        commit(db)
        return "条件不满足，无需转账。" if evaluation["complete"] else "消费数据不完整，不能准备转账。"
    args = {"source_account_id": context["source_account_id"],
            "target": context["target"],
            "amount_minor": context["amount_minor"], "currency": "CNY", "condition": condition}
    prepare(db, task, text, args)
    return preparation_reply(db, task, "条件当前成立；请核对固定区间和操作单。确认时会在事务内重算消费。")

def _investigate(db, task, text, context):
    tail = re.search(r"尾号\s*(\d{4})", text)
    if tail:
        context["tail"] = tail.group(1)
    if "tail" not in context:
        task.context_json = dump(context)
        return "请说明允许冻结的卡片尾号；我会先查昨晚消费，再核对关联卡片。"
    query = rows(db, task)[0]
    if not context.get("range"):
        context["range"] = list(last_night_range(parse_day(db.get(DemoSetting, 1).demo_date)))
    if query.status != "succeeded":
        start, end = context["range"]
        model_check(task, text, "list_transactions", {"start": start[:10], "end": end[:10], "limit": 100})
        candidates = db.scalars(select(Transaction).where(Transaction.owner_id == task.owner_id,
            Transaction.type == "purchase", Transaction.status == "posted",
            Transaction.posted_at_utc >= start, Transaction.posted_at_utc < end).order_by(Transaction.posted_at_utc, Transaction.id)).all()
        options = []
        for tx in candidates:
            card = db.get(Card, tx.card_id) if tx.card_id else None
            options.append({"id": tx.id, "merchant": tx.merchant, "amount_minor": tx.amount_minor,
                            "posted_at_utc": tx.posted_at_utc, "card_id": tx.card_id,
                            "card_last4": card.card_last4 if card else None, "account_id": tx.account_id})
        context["candidates"] = options
        query.status = "succeeded"
        query.args_json = dump({"start_utc": start, "end_utc": end})
        query.result_json = dump({"range": context["range"], "candidates": options})
        commit(db)
    candidates = context.get("candidates", [])
    if len(candidates) == 1:
        context.setdefault("selected_id", candidates[0]["id"])
    if text.startswith("选择交易 "):
        selection = text.removeprefix("选择交易 ").strip()
        if selection not in {item["id"] for item in candidates}:
            raise DomainError("RESOURCE_NOT_FOUND", "交易不在当前候选范围", 404)
        context["selected_id"] = selection
    selected = next((item for item in candidates if item["id"] == context.get("selected_id")), None)
    task.context_json = dump(context)
    commit(db)
    if not candidates:
        for step in rows(db, task)[1:]:
            step.status = "skipped"
            step.result_json = dump({"reason": "没有符合昨晚范围的已入账消费"})
        commit(db)
        return "昨晚范围内没有已入账消费，无需冻结。"
    if selected is None:
        return "昨晚有多笔候选消费，请选择要核对的交易；我不会自动判定哪笔陌生或属于盗刷。"
    query.result_json = dump({"range": context["range"], "candidates": candidates, "selected": selected})
    step = write_step(db, task)
    if selected["card_last4"] != context["tail"]:
        step.status = "skipped"
        step.result_json = dump({"reason": "所选交易关联卡尾号不满足条件", "selected": selected})
        commit(db)
        return "卡尾号不满足条件，跳过冻结；继续查询关联账户可用余额。"
    condition = {"type": "transaction_card", "transaction_id": selected["id"],
                 "card_id": selected["card_id"], "account_id": selected["account_id"],
                 "card_last4": context["tail"], "amount_minor": selected["amount_minor"],
                 "posted_at_utc": selected["posted_at_utc"]}
    commit(db)
    prepare(db, task, text, {"card_id": selected["card_id"], "action": "freeze", "condition": condition})
    return preparation_reply(db, task, "已按所选交易关联卡片准备冻结操作单。确认或拒绝该操作后，会继续查询账户余额。")

def send(db, session, task, text, *, already_recorded=False):
    from app.agent.service import _message, _say, _summary, _local_reply
    recover(db, task)
    if task.status in TERMINAL:
        raise DomainError("INVALID_TASK_STATE", "任务已结束，请创建新任务", 409)
    _message(db, task, "user", text, already_recorded=already_recorded)
    commit(db)
    if re.fullmatch(r"(?:我)?确认[。！!\s]*", text):
        _say(db, task, "聊天确认不执行操作，请核对操作单后点击确认按钮。", task.status)
        return
    context = json.loads(task.context_json)
    # An independent balance question does not revise a prepared transaction.
    if "余额" in text and not re.search(r"转|冻结|改|如果|才|超过|低于|小于|大于|取消", text):
        result = execute(db, session_id=session.id, owner_id=session.owner_id, name="list_accounts", arguments={})
        _say(db, task, _summary("list_accounts", result), task.status)
        return
    revising = bool(re.search(r"改|换|增加|添加|加个|超过|大于|低于|小于|阈值|如果|才|\d+\s*元", text))
    smalltalk = None if revising else _local_reply(text)
    if smalltalk:
        _say(db, task, smalltalk, task.status)
        return
    if task.proposal_id and re.search(r"多少|什么|哪|解释|吗", text) and not revising:
        from app.domain.operations import get_proposal
        summary = get_proposal(db, session_id=session.id, proposal_id=task.proposal_id)["summary"]
        _say(db, task, "当前操作仍未执行，请核对操作单中的金额、对象与执行条件。" +
             (f"金额 {summary['amount_minor']/100:,.2f} 元。" if "amount_minor" in summary else ""), task.status)
        return
    clarification = None
    if task.intent == "conditional_transfer":
        try:
            context = parse_condition_update(db, task, text, context)
        except DomainError as error:
            clarification = error.message
            # An unrecognized revision must make the former conditions unusable.
            context = {key: value for key, value in context.items()
                       if key in {"source_account_id", "target", "target_account_id", "amount_minor"}}
    if task.proposal_id:
        invalidate_agent_proposal(db, session_id=session.id, proposal_id=task.proposal_id)
        task = db.get(AgentTask, task.id)
        task.proposal_id = None
        task.plan_version += 1
        task.plan_json = "[]"
    task.context_json = dump(context)
    plan = json.loads(task.plan_json)
    if task.intent == "conditional_transfer" and plan and (
            bool(context.get("balance_account_id")) != any(item["tool_name"] == "list_accounts" for item in plan)):
        task.plan_version += 1
        task.plan_json = "[]"
    if not json.loads(task.plan_json):
        install_plan(db, task)
    task.status = "gathering_info"
    commit(db)
    if clarification:
        _say(db, task, clarification + "。旧操作单已停止，补充清楚后再准备。", "gathering_info")
        return
    try:
        reply = (_conditional if task.intent == "conditional_transfer" else _investigate)(db, task, text, context)
        task = db.get(AgentTask, task.id)
        _say(db, task, reply, task.status)
    except DomainError as error:
        db.rollback()
        task = db.get(AgentTask, task.id)
        step = next((item for item in rows(db, task) if item.status == "pending"), None)
        if step:
            step.status = "failed"
            step.result_json = dump({"error_code": error.code, "message": error.message})
        _say(db, task, error.message + "（"+error.code+"）", "failed")
    recover(db, task)

def recover(db, task, *, retry_read=False, balance_reader=None):
    from app.agent.control import state as control_state
    if control_state(db, task.id).get('state') in {'paused', 'handoff', 'awaiting_reverification', 'terminated'}:
        return
    if task.intent not in INTENTS or not json.loads(task.plan_json):
        return
    task_id = task.id
    # An interrupted preparation can be replayed because linking the proposal is atomic.
    step = write_step(db, task)
    if not task.stop_requested and not task.proposal_id and step and step.status == "running":
        try:
            result = execute(db, session_id=task.session_id, owner_id=task.owner_id,
                             name=step.tool_name, arguments=json.loads(step.args_json), agent_task_id=task.id)
            _apply_prepared(db, task_id, result)
        except DomainError as error:
            db.rollback()
            task = db.get(AgentTask, task_id)
            step = write_step(db, task)
            step.status = "failed"
            step.result_json = dump({"error_code": error.code, "message": error.message})
            commit(db)
    def work():
        task = db.get(AgentTask, task_id)
        _session(db, task.session_id, _now())
        steps = rows(db, task)
        step = write_step(db, task)
        if task.proposal_id:
            proposal = db.get(Proposal, task.proposal_id)
            if proposal.status == "pending_confirmation" and datetime.fromisoformat(proposal.expires_at_utc) <= _now():
                _end(db, proposal, "expired", _now(), "PROPOSAL_EXPIRED")
            if proposal.status == "pending_confirmation":
                task.status = "awaiting_confirmation"
                return
            if proposal.status == "executed":
                step.status = "succeeded"
                step.result_json = dump({"proposal_id": proposal.id, "receipt": _receipt(db, proposal)})
            elif proposal.status == "cancelled":
                step.status = "cancelled"
            elif proposal.validation_json and json.loads(proposal.validation_json).get("error_code") == "CONDITION_NOT_MET":
                step.status = "skipped"
                step.result_json = dump({"validation": json.loads(proposal.validation_json), "reason": "确认时条件不满足"})
            else:
                step.status = "failed"
        context = json.loads(task.context_json)
        if steps[0].status == "failed":
            for dependent in steps[1:]:
                if dependent.status == "pending":
                    dependent.status = "skipped"
                    dependent.result_json = dump({"reason": "前置查询失败，未执行"})
        if task.stop_requested:
            for item in steps:
                if item.status in {"pending", "running"}:
                    item.status = "cancelled"
            task.status = "partially_completed" if step.status == "succeeded" else "cancelled"
            return
        if task.intent == "investigate_card" and step.status in {"succeeded", "skipped", "failed", "cancelled"}:
            balance = steps[-1]
            if retry_read and balance.status == "failed":
                balance.status = "pending"
            selected = next((item for item in context.get("candidates", []) if item["id"] == context.get("selected_id")), None)
            if selected and balance.status in {"pending", "running"}:
                try:
                    data = (balance_reader or _balance)(db, task.owner_id, selected["account_id"])
                    balance.args_json = dump({"account_id": selected["account_id"]})
                    balance.result_json = dump(data)
                    balance.status = "succeeded"
                except Exception:
                    balance.status = "failed"
                    balance.result_json = dump({"error_code": "READ_FAILED", "message": "余额读取失败；已执行业务回执保留"})
        if (task.intent == "conditional_transfer" and context.get("balance_account_id")
                and steps[0].status == "succeeded"
                and step.status in {"succeeded", "skipped", "failed", "cancelled"}):
            balance = steps[-1]
            if retry_read and balance.status == "failed":
                balance.status = "pending"
            if balance.status in {"pending", "running"}:
                try:
                    data = (balance_reader or _balance)(db, task.owner_id, context["balance_account_id"])
                    balance.args_json = dump({"account_id": context["balance_account_id"]})
                    balance.result_json = dump(data)
                    balance.status = "succeeded"
                    from app.agent.service import _message
                    outcome = "模拟转账已完成" if step.status == "succeeded" else "未执行转账"
                    _message(db, task, "assistant", f"{outcome}；{data['alias']}（尾号{data['mask'][-4:]}）"
                             f"可用余额 {data['available_balance_minor']/100:,.2f} 元。")
                except Exception:
                    balance.status = "failed"
                    balance.result_json = dump({"error_code": "READ_FAILED", "message": "余额读取失败；转账实际结果保留，可重试查询"})
        statuses = [item.status for item in steps]
        if any(status in {"pending", "running", "awaiting_confirmation"} for status in statuses):
            task.status = "gathering_info"
        elif all(status in {"succeeded", "skipped"} for status in statuses):
            task.status = "completed"
        elif (task.intent == "conditional_transfer" and steps[-1].tool_name == "list_accounts"
              and steps[-1].status == "failed" and step.status in {"succeeded", "skipped"}):
            task.status = "partially_completed"
        elif task.intent == "conditional_transfer":
            task.status = "cancelled" if step.status == "cancelled" else "expired" if task.proposal_id and db.get(Proposal, task.proposal_id).status == "expired" else "failed"
        else:
            task.status = "partially_completed" if any(status == "succeeded" for status in statuses) else "failed"
    _write(db, work)

def cancel_task(db, session, task, *, only_operation=False):
    task_id = task.id
    def work():
        _session(db, session.id, _now())
        task = db.get(AgentTask, task_id)
        if task.session_id != session.id:
            raise DomainError("RESOURCE_NOT_FOUND", "任务不存在", 404)
        if task.status in {"completed", "cancelled"}:
            return
        step = write_step(db, task)
        if task.proposal_id:
            proposal = _proposal(db, session, task.proposal_id)
            if proposal.status == "pending_confirmation":
                _end(db, proposal, "cancelled", _now(), "USER_REJECTED" if only_operation else "TASK_CANCELLED")
        elif step and step.status in {"pending", "running"}:
            step.status = "cancelled"
        if not only_operation:
            task.stop_requested = True
            if not json.loads(task.plan_json):
                task.status = "cancelled"
    _write(db, work)
    recover(db, db.get(AgentTask, task_id))
