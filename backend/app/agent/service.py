"""Single-scenario conversation, durable steps, and deterministic evidence wording."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.registry import execute, validate
from app.data.atomic import atomic_write, commit
from app.data.models import Account, AgentMessage, AgentStep, AgentTask, Card, DemoSession, DemoSetting, Proposal, Recipient
from app.domain.clock import selected_range, parse_day, previous_full_month_range
from app.domain.errors import DomainError
from app.domain.recipient_directory import PHONE, phone_reference, match_phone, note_reference, match_remark
from app.domain.operations import get_proposal, invalidate_agent_proposal, _session, _now

MAX_MESSAGE_LENGTH = 1000
MAX_STEPS = 12
AMOUNT = re.compile(r"(?<![0-9])(?:¥|￥)?([0-9]+(?:\.[0-9]{1,2})?)\s*元")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")

def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def _task(db: Session, session: DemoSession, task_id: str) -> AgentTask:
    task = db.scalar(select(AgentTask).where(AgentTask.id == task_id, AgentTask.owner_id == session.owner_id,
                                            AgentTask.session_id == session.id,
                                            AgentTask.dataset_version == session.dataset_version))
    if task is None:
        raise DomainError("RESOURCE_NOT_FOUND", "任务不存在", 404)
    return task

def _message(db: Session, task: AgentTask, role: str, content: str, created_at: str | None = None,
             *, already_recorded: bool = False) -> None:
    # A typed Live decision can be handed to a specialized flow after dialogue.send
    # persisted this exact user message. Do not duplicate it in chat or model context.
    if role == 'user' and already_recorded:
        return
    from app.agent.privacy import scrub_auth
    content = scrub_auth(content)
    ordinal = db.query(AgentMessage).filter(AgentMessage.task_id == task.id).count() + 1
    db.add(AgentMessage(id=uuid4().hex, task_id=task.id, ordinal=ordinal, role=role, content=content, created_at_utc=created_at or _stamp()))
    task.updated_at_utc = _stamp()

def _say(db: Session, task: AgentTask, content: str, status: str) -> None:
    _message(db, task, "assistant", content)
    task.status = status
    commit(db)

def view(db: Session, task: AgentTask) -> dict:
    from app.agent.crossflow import INTENTS, recover
    cross = task.intent in INTENTS
    if cross:
        recover(db, task)
    rows = db.scalars(select(AgentMessage).where(AgentMessage.task_id == task.id)
                      .order_by(AgentMessage.ordinal)).all()
    proposal = get_proposal(db, session_id=task.session_id, proposal_id=task.proposal_id) if task.proposal_id else None
    if not cross:
        from app.domain.operations import sync_single_agent_steps
        sync_single_agent_steps(db, task.id)
    from app.agent.business_flow import public_state
    business = public_state(db, task)
    from app.domain.wealth import task_state
    business.update(task_state(db, task))
    from app.domain.card_management import task_state as card_task_state
    business.update(card_task_state(db,task))
    from app.domain.subscriptions import task_state as subscription_task_state
    business.update(subscription_task_state(db,task))
    from app.domain.birthdays import task_state as birthday_task_state
    business.update(birthday_task_state(db,task))
    steps = db.scalars(select(AgentStep).where(AgentStep.task_id == task.id)
                       .order_by(AgentStep.ordinal)).all()
    status = task.status
    birthday_request = business.get("birthday_request")
    if birthday_request and birthday_request["status"] == "pending_confirmation":
        status = "awaiting_confirmation"
    clarification=json.loads(task.context_json).get("clarification")
    if (clarification and not task.stop_requested
            and status not in {"cancelled","expired","failed"}
            and not (clarification.get("scope")=="read" and status in {"awaiting_confirmation","completed"})
            and not (birthday_request and birthday_request["status"]=="pending_confirmation")):
        status = "gathering_info"
    if proposal and not cross:
        status = {"executed": "completed", "cancelled": "cancelled",
                  "expired": "expired", "invalidated": "gathering_info",
                  "failed": "failed"}.get(proposal["status"], status)
    from app.agent.forms import birthday_details,transfer_details
    from app.agent.control import state as control_state
    control = control_state(db, task.id)
    if json.loads(task.context_json).get('unified_plan') and any(
            row.status == 'pending' for row in steps if row.plan_version == task.plan_version):
        status = 'awaiting_confirmation' if any(row.status == 'awaiting_confirmation' for row in steps) else 'gathering_info'
    if control.get('state') in {'paused', 'handoff', 'awaiting_reverification'}:
        status = control['state']
    if control.get('state') == 'terminated':
        status = 'cancelled'
    elif task.stop_requested:
        status = 'cancelled' if task.status == 'cancelled' else status
    return {**business, 'control': control, "birthday_details":birthday_details(db,task),"transfer_details":transfer_details(db,task), "id": task.id, "mode": task.mode, "intent": task.intent, "status": status,
            "proposal": proposal, "messages": [{"role": row.role, "content": row.content,
                                                  "created_at_utc": row.created_at_utc} for row in rows],
            "plan_version": task.plan_version, "plan": json.loads(task.plan_json),
            "requested_outcomes": json.loads(task.requested_outcomes_json),
            "candidates": json.loads(task.context_json).get("candidates", []) if cross else [],
            "query_range": json.loads(task.context_json).get("range") if cross else None,
            "steps": [{"ordinal": row.ordinal, "tool_name": row.tool_name, "status": row.status,
                       "depends_on": json.loads(row.depends_on_json), "on_failure": row.on_failure,
                       "arguments": json.loads(row.args_json),
                       "result": json.loads(row.result_json), "result_ref": row.result_ref}
                      for row in steps if not cross or row.plan_version == task.plan_version]}

def get_agent_task(db: Session, session: DemoSession, task_id: str) -> dict:
    return view(db, _task(db, session, task_id))

def current_agent_task(db: Session, session: DemoSession) -> dict | None:
    task = db.scalar(select(AgentTask).where(AgentTask.owner_id == session.owner_id,
                AgentTask.session_id == session.id, AgentTask.dataset_version == session.dataset_version)
                .order_by(AgentTask.created_at_utc.desc(), AgentTask.id.desc()))
    return view(db, task) if task else None

def _intent(text: str, previous: str | None) -> str | None:
    if any(word in text for word in ("余额", "多少钱可用", "还有多少钱")):
        return "accounts"
    if any(word in text for word in ("冻结", "解冻", "卡片", "银行卡", "卡状态")):
        return "card"
    if any(word in text for word in ("流水", "账单", "交易记录")):
        return "transactions"
    if any(word in text for word in ("消费", "花了", "花销", "花费", "支出", "统计", "餐饮", "购物", "交通")):
        return "spending"
    from app.agent.clarification import explicit_transfer
    if any(word in text for word in ("转账", "转给", "转入", "转到", "给")) or explicit_transfer(text):
        return "transfer"
    return previous

def _minor(text: str) -> int | None:
    # Share the strict decimal/Chinese parser with scheduled transfers. An
    # ambiguous shorthand or negative amount never inherits an older amount.
    from app.agent.requirements import NUMBER, yuan_minor
    found = re.findall(r"([负−-]?\s*" + NUMBER + r")\s*(?:元|块)", text)
    if not found:
        return None
    return yuan_minor(found[-1].strip())


def _transfer_amount_text(text: str) -> str:
    from app.agent.requirements import NUMBER
    # An explicitly labelled gift budget does not state how much to transfer.
    return re.sub(r'(?:礼物)?预算(?:共|是|为|约|大约|改成|改为)?\s*[负−-]?\s*'
                  + NUMBER + r'\s*(?:元|块)', '', text)

def _catalog(db: Session, owner_id: str):
    accounts = db.scalars(select(Account).where(Account.owner_id == owner_id).order_by(Account.id)).all()
    recipients = db.scalars(select(Recipient).where(Recipient.owner_id == owner_id, Recipient.status != "one_time").order_by(Recipient.id)).all()
    cards = db.scalars(select(Card).where(Card.owner_id == owner_id).order_by(Card.id)).all()
    return accounts, recipients, cards

def _resolve(task: AgentTask, text: str, context: dict, db: Session, *, intent_hint=None) -> tuple[str | None, dict, str | None]:
    from app.agent.analytics import INTENTS, explicit_intent, resolve_read
    intent = intent_hint or explicit_intent(text, context.get("last_query", {})) or _intent(text, task.intent)
    if intent in {"spending", "transactions"} and any(word in text for word in ("那笔", "某笔", "陌生")) and not DATE.findall(text) and not AMOUNT.findall(text):
        return None, {}, "请补充这笔交易的日期、金额、商户或卡尾号，以免误认多笔流水。"
    if intent in INTENTS:
        try:
            return resolve_read(db, task, intent, text, context)
        except DomainError as error:
            return None, {}, error.message
    accounts, recipients, cards = _catalog(db, task.owner_id)
    if intent is None:
        return None, {}, "我可以查询消费或流水、准备模拟转账，以及查询或冻解卡片。请说明其中一项需求。"
    if intent in {"spending", "transactions"} and any(word in text for word in ("那笔", "某笔", "陌生")) and not DATE.findall(text) and not AMOUNT.findall(text):
        return None, {}, "请补充这笔交易的日期、金额、商户或卡尾号，以免误认多笔流水。"
    if intent == "accounts":
        return "list_accounts", {}, None
    if intent == "card":
        action = "unfreeze" if "解冻" in text else "freeze" if "冻结" in text else context.get("action")
        if action is None:
            return "list_cards", {}, None
        context["action"] = action
        matched = [card for card in cards if card.alias in text or card.card_last4 in text or card.id in text.split()]
        if matched:
            context["card_id"] = matched[-1].id if len(matched) == 1 else None
        card_id = context.get("card_id")
        if not card_id:
            choices = "、".join(f"{card.alias} 尾号{card.card_last4}" for card in cards)
            return None, {}, f"要{('解冻' if action == 'unfreeze' else '冻结')}哪张卡？可选：{choices}。"
        return "prepare_card_action", {"card_id": card_id, "action": action}, None
    # Transfer: resolve each slot from the newest user instruction. Retain only
    # explicitly established slots across clarifying turns.
    if re.search(r"重新(?:发起|转|办理)|(?:新|另)(?:的)?一笔", text):
        for key in ("source_account_id","target","amount_minor","target_remark",
                    "remark_choices","phone_choices","ambiguous_name","direct_recipient","direct_recipient_active"):
            context.pop(key,None)
    amount_text = _transfer_amount_text(text)
    amount = _minor(amount_text)
    if amount is not None:
        context["amount_minor"] = amount
    elif re.search(r"[0-9零〇一二两三四五六七八九十百千万]\s*(?:元|块)|(?:金额|数额)\s*(?:改|调整|换|设|变)|(?:改成|改为|调整为|换成).*(?:元|块)", amount_text):
        context.pop("amount_minor", None)
    active_accounts = [item for item in accounts if item.status == "active"]
    lookup_text = PHONE.sub("", text)
    mentioned = [item for item in active_accounts if item.alias in lookup_text or item.account_last4 in lookup_text]
    if "从" in text:
        after = lookup_text.split("从", 1)[1]
        source_text = re.split(r"转|给|到|向|划|拨", after, maxsplit=1)[0]
        source = next((item for item in mentioned if item.alias in source_text or item.account_last4 in source_text), None)
        if source:
            context["source_account_id"] = source.id
        else:
            context.pop("source_account_id", None)
    elif len(mentioned) >= 2 and any(word in text for word in ("转", "到")):
        context["source_account_id"] = mentioned[0].id
    source_id = context.get("source_account_id")
    candidates = [item for item in mentioned if item.id != source_id]
    if candidates and any(word in text for word in ("到", "转入", "转给", "改成")):
        context["target"] = {"type": "own_account", "account_id": candidates[-1].id}
    from app.domain.direct_recipient import extract_details,validated_target
    direct,explicit_direct=extract_details(text,context.get("direct_recipient"))
    direct_active=explicit_direct or context.get("target",{}).get("type")=="external_account" or context.get("direct_recipient_active")
    if direct_active:
        context["direct_recipient"]=direct;context["direct_recipient_active"]=True
        context.pop("target",None);context.pop("target_remark",None)
        context.pop("remark_choices",None);context.pop("phone_choices",None);context.pop("ambiguous_name",None)
        if all(direct.get(key) for key in ("name","bank_name","account_number")):
            try:context["target"]=validated_target({"type":"external_account",**direct})
            except DomainError as error:return None,{},error.message
    else:
        remark, remark_error = note_reference(text)
        # A new explicit recipient supersedes the old saved remark, while a bare tail
        # still selects only within the previously presented remark candidates.
        explicit_phone, explicit_phone_error = phone_reference(text)
        explicit_named = any(row.name in lookup_text for row in recipients)
        explicit_own = bool(candidates and any(word in text for word in ("到", "转入", "转给", "改成")))
        if not remark and not remark_error and (explicit_phone or explicit_phone_error or explicit_named or explicit_own):
            context.pop("target_remark", None)
            context.pop("remark_choices", None)
            if not explicit_own:
                context.pop("target", None)
        if remark or remark_error:
            context.pop("target", None)
            context.pop("ambiguous_name", None)
            context.pop("phone_choices", None)
            context.pop("remark_choices", None)
            if remark_error:
                return None, {}, remark_error
            context["target_remark"] = remark
            matches = match_remark(remark, recipients)
            if len(matches) == 1:
                context["target"] = {"type": "saved_recipient", "recipient_id": matches[0].id}
            elif matches:
                context["remark_choices"] = [row.id for row in matches]
                return None, {}, "该备注对应多个收款人，请在这些对象中选择尾号：" + "、".join(row.account_last4 for row in matches) + "。"
            else:
                return None, {}, "未找到这个已保存备注。新收款人无需预先收录，请在下方填写户名、开户银行和完整收款账号。"
        elif context.get("remark_choices"):
            choices = [row for row in recipients if row.id in context["remark_choices"]]
            tail = re.fullmatch(r"(?:选(?:择)?(?:收款人)?)?\s*尾号\s*([0-9]{4})[。！! ]*", text)
            matches = [row for row in choices if tail and row.account_last4 == tail.group(1)]
            if len(matches) != 1:
                return None, {}, "请在该备注的候选收款人中选择尾号：" + "、".join(row.account_last4 for row in choices) + "。"
            context["target"] = {"type": "saved_recipient", "recipient_id": matches[0].id}
            context.pop("remark_choices", None)
        phone, phone_error = phone_reference(text)
        if remark or context.get("target_remark") and not phone and not phone_error:
            pass  # Explicit remark matching never falls through to a guessed recipient name.
        elif phone or phone_error:
            context.pop("target", None)
            context.pop("ambiguous_name", None)
            context.pop("phone_choices", None)
            if phone_error:
                return None, {}, phone_error
            matched = match_phone(phone, recipients)
            if len(matched) == 1:
                context["target"] = {"type": "saved_recipient", "recipient_id": matched[0].id}
            elif matched:
                context["phone_choices"] = [row.id for row in matched]
                return None, {}, "该手机号对应多个收款对象，请选收款账户尾号：" + "、".join(row.account_last4 for row in matched) + "。"
            else:
                return None, {}, "未找到对应的已保存收款人。新收款人无需预先收录，请在下方填写户名、开户银行和完整收款账号。"
        elif context.get("phone_choices"):
            choices = [row for row in recipients if row.id in context["phone_choices"]]
            tail = re.fullmatch(r"(?:选(?:择)?(?:收款人)?)?\s*尾号\s*([0-9]{4})[。！! ]*", text)
            selected = [row for row in choices if tail and row.account_last4 == tail.group(1)]
            if len(selected) == 1:
                context["target"] = {"type": "saved_recipient", "recipient_id": selected[0].id}
                context.pop("phone_choices", None)
            else:
                return None, {}, "请在该手机号的候选收款对象中选择尾号：" + "、".join(row.account_last4 for row in choices) + "。"
        else:
            named = [item for item in recipients if item.name in lookup_text or item.account_last4 in lookup_text or item.id in lookup_text.split()]
            if named:
                with_tail = [item for item in named if item.account_last4 in lookup_text or item.id in lookup_text.split()]
                if len(with_tail) == 1:
                    context["target"] = {"type": "saved_recipient", "recipient_id": with_tail[0].id}
                elif len({item.name for item in named}) == 1 and len(named) == 1:
                    context["target"] = {"type": "saved_recipient", "recipient_id": named[0].id}
                else:
                    context.pop("target", None)
                    context["ambiguous_name"] = named[0].name
            elif context.get("ambiguous_name"):
                matching = [item for item in recipients if item.name == context["ambiguous_name"]
                            and (item.account_last4 in lookup_text or item.id in lookup_text.split())]
                if len(matching) == 1:
                    context["target"] = {"type": "saved_recipient", "recipient_id": matching[0].id}
                    context.pop("ambiguous_name", None)
    missing = []
    if not source_id:
        missing.append("付款账户（" + "、".join(f"{item.alias} 尾号{item.account_last4}" for item in active_accounts) + "）")
    if not context.get("target"):
        if context.get("ambiguous_name"):
            options = [item for item in recipients if item.name == context["ambiguous_name"]]
            missing.append("收款人尾号（" + "、".join(item.account_last4 for item in options) + "）")
        else:
            missing.append("收款户名、开户银行和完整收款账号（新收款人无需预先收录，可在下方输入框填写）")
    if not context.get("amount_minor"):
        missing.append("金额")
    if missing:
        return None, {}, "请补充" + "、".join(missing) + "。"
    return "prepare_transfer", {"source_account_id": source_id, "target": context["target"],
                                "amount_minor": context["amount_minor"], "currency": "CNY"}, None

def _local_reply(text: str) -> str | None:
    if any(word in text for word in ("介绍", "什么功能", "能做什么", "怎么用", "你是谁")):
        return "我可以帮你查余额、分析消费和退款、找交易记录，也能准备模拟转账、冻结或解冻卡片。你可以直接说需求，不用记固定句式；涉及转账或卡片操作时，我会先给你核对，再由你点击确认。你想先看看余额，还是最近的花销？"
    if text.strip("？?。！! ") in {"查询", "查一下", "帮我查查", "查查", "查询一下"}:
        return "你想查账户余额、最近花了多少，还是找某一笔交易？比如可以说‘看看我的余额’。"
    if any(word in text for word in ("建议", "预算", "规划", "怎么省", "如何省", "怎么安排")):
        return "可以先把预算分成交通、住宿、餐饮、游玩购物和应急备用金，再给每天的可变开销设上限。旅行预算要先扣掉往返交通和住宿，剩下的再按天分配；涉及外币时，汇率和手续费需要按实际支付渠道核对。你计划几个人去、玩几天，总预算大约多少？"
    if text.strip("？?。！! ") in {"你好", "您好", "嗨", "hello", "hi"}:
        return "你好，我是 FinAgent。可以帮你看看账户和花销，也可以一起梳理预算。你现在最想解决什么？"
    if text.strip("。！! ") in {"谢谢", "谢谢你", "好的", "明白了"}:
        return "不客气。有想继续了解的，直接接着问就好。"
    return None


def _summary(name: str, result) -> str:
    if name in {"summarize_spending", "generate_statement", "inspect_anomalies"}:
        from app.agent.analytics_summary import summary
        return summary(name, result)
    if name == "list_accounts":
        return "\n".join(f"{item['alias']}（尾号{item['account_mask'][-4:]}）可用余额 {item['available_balance_minor']/100:,.2f} 元。" for item in result)
    if name == "list_transactions":
        return f"查到 {result['total']} 条流水，以下展示前 {len(result['items'])} 条；详情以逐笔证据为准。"
    if name == "list_cards":
        return "；".join(f"{item['alias']} 尾号{item['card_mask'][-4:]}：{'已挂失' if item['status']=='lost' else '已冻结' if item['status']=='frozen' else '正常'}" for item in result)
    if name.startswith("prepare_"):
        if result.get("already_in_state"):
            return "卡片已处于目标状态，无需确认或执行。"
        return "已准备固定操作单。请核对下方摘要并点击“确认执行”；聊天回复“确认”不会执行。"
    return "已读取服务端结果。"

def _record_step(db: Session, task: AgentTask, name: str, args: dict, result) -> None:
    ordinal = (db.query(AgentStep).filter(AgentStep.task_id == task.id).count() or 0) + 1
    if ordinal > MAX_STEPS:
        raise DomainError("TOOL_BUDGET_EXCEEDED", "任务步骤已达上限", 409)
    proposal = result.get('proposal') if name.startswith('prepare_') else None
    db.add(AgentStep(id=uuid4().hex, task_id=task.id, plan_version=task.plan_version, ordinal=ordinal, tool_name=name,
                     args_json=_dump(args), result_json=_dump(result),
                     status='awaiting_confirmation' if proposal else 'succeeded',
                     result_ref=proposal['id'] if proposal else None,
                     created_at_utc=_stamp()))

def create(db: Session, session: DemoSession, mode: str, text: str) -> dict:
    if mode not in {"mock", "live"}:
        raise DomainError("INVALID_INPUT", "无效模型模式")
    previous = db.scalar(select(AgentTask).where(AgentTask.owner_id == session.owner_id,
        AgentTask.session_id == session.id, AgentTask.dataset_version == session.dataset_version)
        .order_by(AgentTask.created_at_utc.desc(), AgentTask.id.desc()))
    inherited = {}
    history = []
    if previous:
        # Carry read filters and read-only task references; never reuse completed write parameters.
        previous_context = json.loads(previous.context_json)
        inherited['last_agent_task_id'] = previous.id
        inherited['reference_fields'] = {key: value for key, value in previous_context.get('dialogue_memory', {}).get('fields', {}).items()
                                         if key in {'source', 'card'}}
        if previous_context.get("birthday_plan_id"):
            inherited["last_birthday_plan_id"] = previous_context["birthday_plan_id"]
        for key in ("last_condition_task_id", "last_schedule_task_id", "last_collection_task_id", "unsupported_write", "last_wealth_query", "last_birthday_plan_id", "last_transfer_proposal_id"):
            if key in previous_context:
                inherited[key] = previous_context[key]
        if previous_context.get("schedule_id"):
            inherited["last_schedule_task_id"] = previous.id
        if previous_context.get("collection_id"):
            inherited["last_collection_task_id"] = previous.id
        if previous.intent == "conditional_transfer":
            inherited["last_condition_task_id"] = previous.id
        last_query = previous_context.get("last_query")
        if last_query:
            inherited["last_query"] = last_query
        budget = json.loads(previous.context_json).get("budget")
        if budget:
            inherited["budget"] = budget
        history = list(reversed(db.scalars(select(AgentMessage).where(AgentMessage.task_id == previous.id)
            .order_by(AgentMessage.ordinal.desc()).limit(16)).all()))
    task = AgentTask(id=uuid4().hex, owner_id=session.owner_id, session_id=session.id,
                     dataset_version=session.dataset_version, mode=mode, status="created",
                     context_json=_dump(inherited), created_at_utc=_stamp(), updated_at_utc=_stamp())
    db.add(task)
    commit(db)
    for item in history:
        _message(db, task, item.role, item.content, item.created_at_utc)
    if previous and previous.proposal_id:
        previous_proposal = get_proposal(db, session_id=session.id, proposal_id=previous.proposal_id)
        if previous_proposal.get('kind') == 'transfer':
            inherited['last_transfer_proposal_id'] = previous.proposal_id
            task.context_json = _dump(inherited)
        receipt = previous_proposal.get("receipt")
        if receipt:
            factual = "上一步操作回执：" + receipt["message"]
            if receipt.get("amount_minor") is not None:
                factual += f"，金额 {receipt['amount_minor']/100:,.2f} 元"
            _message(db, task, "assistant", factual + "。")
    commit(db)
    return send(db, session, task.id, text)

def _failure_reply(db, task, failure):
    """Business facts and recovery options come from the ledger, not chat text."""
    code = failure["error_code"]
    if code == "INSUFFICIENT_FUNDS" and failure.get("source_account_id"):
        from app.domain.ledger import owned_account, available_balance
        account = owned_account(db, task.owner_id, failure["source_account_id"], active=False)
        amount = failure["amount_minor"]
        available = available_balance(db, account)
        gap = amount - available
        text = (f"未办理成功：上一笔转账因账户余额不足未执行。{account.alias}可用余额 "
                f"{available/100:,.2f} 元，申请转账 {amount/100:,.2f} 元。")
        if gap > 0:
            text += (f"还差 {gap/100:,.2f} 元；可以先补足资金，或改用余额足够的本人账户，"
                     "也可以降低金额。修改后会重新准备操作单，仍需你确认，当前没有扣款。")
        else:
            text += "当前余额已足够，但原请求没有执行；如仍需办理，请重新提出转账需求并核对操作单。"
        return text
    return "未办理成功：" + failure["message"] + "。请调整业务条件后重新准备；没有生成成功回执。"


def _remember_failure(db, task, context, error, args):
    failure = {"error_code": error.code, "message": error.message}
    if error.code == "INSUFFICIENT_FUNDS" and args.get("source_account_id") and args.get("amount_minor"):
        failure.update(source_account_id=args["source_account_id"], amount_minor=args["amount_minor"])
    context["last_write_failure"] = failure
    task.context_json = _dump(context)
    return _failure_reply(db, task, failure)


def _apply_live_decision(db, session, task, text, current, decision):
    from app.agent.language import resolve
    context = json.loads(task.context_json)
    if decision.intent == "clarify":
        from app.agent.clarification import apply_decision
        return apply_decision(db,session,task,text,decision)
    if context.get("clarification_transfer_active") and decision.intent != "transfer":
        from app.agent.clarification import explicit_transfer,apply_decision
        if explicit_transfer(text):
            return apply_decision(db,session,task,text,decision)
    if decision.intent == "cancel":
        return cancel(db, session, task.id)
    # The model selects a business flow, never its execution/confirmation endpoint.
    # This runs after the revision check with the write lock held; decision_hint
    # MUST prevent every specialized flow from issuing a second network request.
    from app.agent import wealth_flow, card_flow, subscription_flow, birthday_flow, business_flow
    flow = (wealth_flow if decision.intent in wealth_flow.INTENTS else
            card_flow if decision.intent in {'card_management', 'card_management_status'} else
            subscription_flow if decision.intent in subscription_flow.INTENTS.values() else
            birthday_flow if decision.intent in birthday_flow.INTENTS.values() else
            business_flow if decision.intent in business_flow.BUSINESS_INTENTS else None)
    if flow is not None:
        result = flow.dispatch(db, session, task, text, decision_hint=decision)
        if result is not None:
            return result
    context = json.loads(task.context_json)
    if context.get("clarification",{}).get("scope")=="read":
        context.pop("clarification",None)
        task.context_json=_dump(context)
    pending = current.get("proposal") and current["proposal"]["status"] == "pending_confirmation"
    clarifying_write = task.status == "gathering_info" and context.get("dialogue_intent") in {"transfer", "card_action"}
    try:
        if decision.intent in {"transfer", "card_action"} and any(word in text for word in ("如果", "才", "否则", "超过", "大于", "低于")):
            _say(db, task, "这项操作带有条件，我需要先核对完整条件。你可以说‘如果本月餐饮超过500元，就从活期转100元到储蓄账户’，或先单独查询，再决定是否操作。", task.status if pending else "gathering_info")
            return view(db, task)
        name, raw_args, reply = resolve(db, task, decision, context, text=text)
        # A casual question or a read can coexist with a pending operation.
        # Only another write instruction replaces its fixed proposal.
        writing = decision.intent in {"transfer", "card_action"}
        if pending and writing:
            fixed = json.loads(db.get(Proposal, task.proposal_id).params_json)
            unchanged = (name == "prepare_transfer" and fixed["action"] == "transfer"
                and fixed["source_account_id"] == raw_args.get("source_account_id")
                and fixed["target"] == raw_args.get("target") and fixed["amount_minor"] == raw_args.get("amount_minor")) or (
                name == "prepare_card_action" and fixed["action"] == raw_args.get("action")
                and fixed["card_id"] == raw_args.get("card_id"))
            if unchanged:
                _say(db, task, "这笔操作已经准备好了，还没有执行。核对下方金额和对象后，点击‘确认执行’即可。", "awaiting_confirmation")
                return view(db, task)
            invalidate_agent_proposal(db, session_id=session.id, proposal_id=task.proposal_id)
            task = _task(db, session, task.id)
            task.proposal_id = None
            pending = False
        if writing or (not pending and not clarifying_write and decision.intent != "reply"):
            task.intent = {"card_action": "card", "cards": "card"}.get(decision.intent, decision.intent)
        task.context_json = _dump(context)
        commit(db)
        if reply:
            status = "awaiting_confirmation" if pending else "gathering_info" if writing or (task.status == "gathering_info" and context.get("dialogue_slots")) else "completed"
            _say(db, task, reply, status)
            return view(db, task)
        args = validate(name, raw_args)
        if db.query(AgentStep).filter(AgentStep.task_id == task.id).count() >= MAX_STEPS:
            raise DomainError("TOOL_BUDGET_EXCEEDED", "本次任务步骤已达上限，请取消后重新提出需求", 409)
        result = execute(db, session_id=session.id, owner_id=session.owner_id, name=name, arguments=args, agent_task_id=task.id)
        task = _task(db, session, task.id)
        _record_step(db, task, name, args, result)
        if name.startswith("prepare_"):
            context.pop("last_write_failure", None)
            task.context_json = _dump(context)
        if name.startswith("prepare_") and result.get("proposal"):
            task.proposal_id = result["proposal"]["id"]
            status = "awaiting_confirmation"
        else:
            status = "awaiting_confirmation" if pending else "gathering_info" if clarifying_write and not writing else "completed"
        _say(db, task, _summary(name, result), status)
    except DomainError as error:
        task = _task(db, session, task.id)
        # Keep pending proposals and clarification slots usable after model outages.
        if not error.code.startswith("MODEL_") and decision.intent in {"transfer", "card_action"}:
            reply = _remember_failure(db, task, context, error, locals().get("raw_args", {}))
        else:
            reply = error.message + "。"
        _say(db, task, reply, "awaiting_confirmation" if pending else "gathering_info")
    return view(db, task)


def _cancel_request(text: str) -> bool:
    # Match explicit instructions about the current operation, not questions,
    # quoted examples, negations, or cancellation of a different banking service.
    target = r"(?:(?:刚才|当前|这个|刚才这个|刚才那笔|这笔|那笔|整个|该)(?:这个)?(?:任务|操作单|操作|转账|业务)?|任务|操作单|转账)"
    action = r"(?:算了|不转了|取消(?:" + target + r")?|(?:这笔|这个任务)?(?:先)?(?:不要|别)(?:再)?继续(?:办理|执行))"
    suffix = r"(?:[，,；;]\s*(?:不要|别)(?:再)?继续(?:办理|执行))?"
    return re.fullmatch(r"(?:请|帮我|麻烦)?\s*" + action + suffix + r"[。！!\s]*", text) is not None


def _require_supported_write(db, session, task, text, blocked):
    kind, reply = blocked
    with atomic_write(db):
        session, _ = _session(db, session.id, _now())
        task = _task(db, session, task.id)
        current = view(db, task)
        if current["status"] in {"completed", "cancelled", "expired", "failed", "partially_completed"}:
            raise DomainError("INVALID_TASK_STATE", "任务已结束，请创建新任务", 409)
        _message(db, task, "user", text)
        if task.proposal_id:
            invalidate_agent_proposal(db, session_id=session.id, proposal_id=task.proposal_id)
            task.proposal_id = None
            reply += "原待确认操作单已作废，原确认凭据不能执行。"
        for step in db.scalars(select(AgentStep).where(AgentStep.task_id == task.id)).all():
            if step.status in {"pending", "running", "awaiting_confirmation"}:
                step.status = "cancelled"
                step.result_json = _dump({"reason": "新的请求包含暂不支持的执行要求，未执行。"})
        context = json.loads(task.context_json)
        task.context_json = _dump({**{key: context[key] for key in ("last_query", "budget") if key in context},
                                  "unsupported_write": kind})
        task.intent = None
        task.plan_version += 1
        task.plan_json = "[]"
        task.requested_outcomes_json = "[]"
        _say(db, task, reply, "gathering_info")
        return view(db, task)


def _answer_condition_status(db, session, task, text):
    from app.agent.crossflow import condition_status_question, condition_status_reply
    if not condition_status_question(text):
        return None
    context = json.loads(task.context_json)
    reference = task.id if task.intent == "conditional_transfer" else context.get("last_condition_task_id")
    if not reference:
        return None
    with atomic_write(db):
        session, _ = _session(db, session.id, _now())
        task = _task(db, session, task.id)
        source = _task(db, session, reference)
        current = view(db, source)
        reply = condition_status_reply(db, source, current)
        _message(db, task, "user", text)
        _say(db, task, reply, current["status"] if task.id == source.id else "completed")
        return view(db, task)


def _answer_transfer_status(db, session, task, text):
    # A read-only reference to the previous transfer must not become a new write
    # just because the model repeats its old intent or mistakes 刚才 for a condition.
    if not (re.search(r'转账|汇款', text) and re.search(r'查询|查看|结果|成功了吗|办成了吗|执行了吗', text)):
        return None
    if re.search(r'重新|改成|改为|再转|再汇|如果|超过|低于|明天|预约', text):
        return None
    context = json.loads(task.context_json)
    reference = task.proposal_id or context.get('last_transfer_proposal_id')
    if not reference:
        return None
    proposal = get_proposal(db, session_id=session.id, proposal_id=reference)
    if proposal.get('kind') != 'transfer':
        return None
    _message(db, task, 'user', text)
    labels = {'executed':'已完成', 'pending_confirmation':'待确认，未扣款', 'failed':'失败',
              'cancelled':'已取消', 'invalidated':'已失效', 'expired':'已过期'}
    summary = proposal['summary']
    reply = (f"转账结果：{summary['source']['alias']} 向 {summary['target']['alias']}"
             f"（{summary['target']['mask']}）{summary['amount_minor']/100:,.2f} 元；"
             f"{labels.get(proposal['status'], proposal['status'])}。")
    if proposal.get('receipt'):
        reply += '\n' + proposal['receipt']['message']
    _record_step(db, task, 'read_transfer_status', {'proposal_id':reference}, proposal)
    if '余额' in text:
        result = execute(db, session_id=session.id, owner_id=session.owner_id, name='list_accounts', arguments={})
        _record_step(db, task, 'list_accounts', {}, result)
        reply += '\n' + _summary('list_accounts', result)
    # Querying an outstanding draft retains its active authorization state.
    _say(db, task, reply, 'awaiting_confirmation' if task.proposal_id and proposal['status']=='pending_confirmation' else 'completed')
    return view(db, task)


def send(db: Session, session: DemoSession, task_id: str, text: str) -> dict:
    text = text.strip()
    if not text or len(text) > MAX_MESSAGE_LENGTH:
        raise DomainError("INVALID_INPUT", "消息须为 1 至 1000 字")
    task = _task(db, session, task_id)
    from app.agent.control import assert_running
    assert_running(db, task.id)
    from app.agent.privacy import AUTH_TEXT
    if re.fullmatch(r'\d{6}', text) or AUTH_TEXT.search(text):
        _say(db, task, '认证信息请只输入独立安全弹窗。此消息未发送给模型，也未作为业务授权。', task.status)
        return view(db, task)
    # Live has ONE semantic entry before any lexical business router. Fixed
    # forms, authorization endpoints and background workers never enter here.
    if task.mode == 'live':
        if text.strip('。！! ') in {'紧急停止', '停止任务'}:
            return cancel(db, session, task_id, text=text)
        from app.agent.dialogue import send as send_dialogue
        return send_dialogue(db, session, task, text)
    from app.agent.orchestrator import dispatch as dispatch_composite
    from app.agent.control import begin_request
    request_id = begin_request(db, task.id)
    composite = dispatch_composite(db, session, task, text, request_id=request_id)
    if composite is not None:
        return composite
    from app.agent.clarification import dispatch as dispatch_clarification
    clarification = dispatch_clarification(db,session,task,text)
    if clarification is not None:
        return clarification
    from app.agent.birthday_flow import dispatch as dispatch_birthday
    birthday = dispatch_birthday(db,session,task,text)
    if birthday is not None:
        return birthday
    if re.search(r"转账|转给|申购|赎回|冻结|解冻|AA收款|预约|申请.*卡|单笔.*限额|挂失|订阅|续费|代扣",text):
        from app.domain.birthdays import invalidate_task as invalidate_birthday
        invalidate_birthday(db,task)
        commit(db)
    if _cancel_request(text):
        return cancel(db, session, task_id, text=text)
    from app.agent.subscription_flow import dispatch as dispatch_subscription
    subscription=dispatch_subscription(db,session,task,text)
    if subscription is not None:
        return subscription
    if re.search(r"转账|转给|申购|赎回|冻结|解冻|AA收款|预约|申请.*卡|单笔.*限额|禁止.*线上|解除.*线上|挂失",text):
        from app.domain.subscriptions import invalidate_task as invalidate_subscription
        invalidate_subscription(db,task)
        commit(db)
    from app.agent.card_flow import dispatch as dispatch_card
    card=dispatch_card(db,session,task,text)
    if card is not None:
        return card
    from app.domain.card_management import invalidate_task
    if any(word in text for word in ('转账','转给','申购','赎回','冻结','解冻','AA收款','预约')):
        invalidate_task(db,task)
        commit(db)
    from app.agent.wealth_flow import dispatch as dispatch_wealth
    wealth = dispatch_wealth(db, session, task, text)
    if wealth is not None:
        return wealth
    from app.agent.business_flow import dispatch, answer_referenced_status
    answer = answer_referenced_status(db, session, task, text)
    if answer is not None:
        return answer
    business = dispatch(db, session, task, text)
    if business is not None:
        return business
    from app.agent.constraints import unsupported_write, fresh_write_request
    blocked = unsupported_write(text, task.intent, json.loads(task.context_json))
    if blocked:
        return _require_supported_write(db, session, task, text, blocked)
    answer = _answer_condition_status(db, session, task, text)
    if answer is not None:
        return answer
    answer = _answer_transfer_status(db, session, task, text)
    if answer is not None:
        return answer
    # A different request must not later answer about an older conditional transfer.
    context = json.loads(task.context_json)
    changed = context.pop("last_condition_task_id", None) is not None
    if fresh_write_request(text):
        changed = context.pop("unsupported_write", None) is not None or changed
    if changed:
        task.context_json = _dump(context)
        commit(db)
    from app.agent.crossflow import INTENTS, detect, send as send_cross
    cross_intent = task.intent if task.intent in INTENTS else detect(text)
    if task.intent == "transfer" and any(word in text for word in ("超过", "大于", "低于", "小于", "阈值")):
        cross_intent = "conditional_transfer"
    if cross_intent:
        if task.intent == "transfer" and cross_intent == "conditional_transfer":
            from app.agent.crossflow import promote_transfer
            task = promote_transfer(db, session, task)
        elif task.intent not in {None, cross_intent}:
            raise DomainError("INVALID_TASK_STATE", "请先取消当前任务，再提出新的需求", 409)
        if task.status in {"completed", "cancelled", "expired", "partially_completed", "failed"}:
            raise DomainError("INVALID_TASK_STATE", "任务已结束，请创建新任务", 409)
        task.intent = cross_intent
        commit(db)
        send_cross(db, session, task, text)
        return view(db, task)
    current = view(db, task)
    if current["status"] in {"completed", "cancelled", "expired"}:
        raise DomainError("INVALID_TASK_STATE", "任务已结束，请创建新任务", 409)
    if re.fullmatch(r"(?:我)?确认[。！!\s]*", text):
        _message(db, task, "user", text)
        _say(db, task, "聊天确认不执行操作。请核对操作单并点击页面上的“确认执行”按钮。", task.status)
        return view(db, task)
    _message(db, task, "user", text)
    context = json.loads(task.context_json)
    if context.get("last_write_failure") and re.search(r"办成了吗|成功了吗|接下来怎么处理|下一步怎么办|现在.*状态", text):
        _say(db, task, _failure_reply(db, task, context["last_write_failure"]), "gathering_info")
        return view(db, task)
    from app.agent.clarification import transfer_choice
    reply = None if transfer_choice(text) else _local_reply(text)
    if reply:
        _say(db, task, reply, "awaiting_confirmation" if current.get("proposal") and current["proposal"]["status"] == "pending_confirmation" else "completed")
        return view(db, task)
    if task.intent is None and any(word in text for word in ("那", "呢")):
        task.intent = context.get("last_query", {}).get("intent")
    name, raw_args, question = _resolve(task, text, context, db)
    pending_read = bool(current.get('proposal') and current['proposal']['status'] == 'pending_confirmation'
                        and name and not name.startswith('prepare_'))
    if not pending_read:
        task.intent = _intent(text, task.intent)
    task.context_json = _dump(context)
    commit(db)
    if task.proposal_id and not pending_read:
        proposal = get_proposal(db, session_id=session.id, proposal_id=task.proposal_id)
        if proposal["status"] == "pending_confirmation":
            fixed = json.loads(db.get(Proposal, task.proposal_id).params_json)
            unchanged = (name == "prepare_transfer" and fixed["action"] == "transfer"
                         and fixed["source_account_id"] == raw_args.get("source_account_id")
                         and fixed["target"] == raw_args.get("target")
                         and fixed["amount_minor"] == raw_args.get("amount_minor")) or (
                             name == "prepare_card_action" and fixed["action"] == raw_args.get("action")
                             and fixed["card_id"] == raw_args.get("card_id"))
            if unchanged:
                _say(db, task, "原操作单仍待确认；请核对摘要并点击“确认执行”。", "awaiting_confirmation")
                return view(db, task)
            invalidate_agent_proposal(db, session_id=session.id, proposal_id=task.proposal_id)
            task = _task(db, session, task_id)
            task.proposal_id = None
            task.status = "gathering_info"
            commit(db)
    if question:
        _say(db, task, question, "gathering_info")
        return view(db, task)
    if name is None:
        _say(db, task, "无法识别这项单场景需求。", "gathering_info")
        return view(db, task)
    try:
        args = validate(name, raw_args)
        if db.query(AgentStep).filter(AgentStep.task_id == task.id).count() >= MAX_STEPS:
            raise DomainError("TOOL_BUDGET_EXCEEDED", "本次任务步骤已达上限，请取消后重新提出需求", 409)
        result = execute(db, session_id=session.id, owner_id=session.owner_id, name=name, arguments=args, agent_task_id=task.id)
        task = _task(db, session, task_id)
        _record_step(db, task, name, args, result)
        if name.startswith("prepare_"):
            context.pop("last_write_failure", None)
            task.context_json = _dump(context)
        if name.startswith("prepare_") and result.get("proposal"):
            task.proposal_id = result["proposal"]["id"]
            status = "awaiting_confirmation"
        else:
            status = "awaiting_confirmation" if pending_read else "completed"
        _say(db, task, _summary(name, result), status)
    except DomainError as error:
        task = _task(db, session, task_id)
        reply = (_remember_failure(db, task, context, error, raw_args)
                 if name.startswith("prepare_") and not error.code.startswith("MODEL_")
                 else f"{error.message}（{error.code}）。")
        _say(db, task, reply, "failed" if error.code.startswith("MODEL_") else "gathering_info")
    return view(db, task)

def cancel(db: Session, session: DemoSession, task_id: str, *, text: str | None = None) -> dict:
    from app.agent.business_flow import cancel_business
    from app.agent.birthday_flow import cancel_birthday
    birthday = cancel_birthday(db,session,_task(db,session,task_id))
    if birthday is not None:
        return birthday
    task = _task(db, session, task_id)
    if text:
        context = json.loads(task.context_json)
        if context.get('schedule_id') or context.get('collection_id'):
            _message(db, task, 'user', text)
    from app.agent.subscription_flow import cancel_subscription
    subscription=cancel_subscription(db,session,task)
    if subscription is not None:
        return subscription
    from app.agent.card_flow import cancel_card
    card=cancel_card(db,session,task)
    if card is not None:
        return card
    from app.agent.wealth_flow import cancel_wealth
    wealth = cancel_wealth(db, session, task)
    if wealth is not None:
        return wealth
    business = cancel_business(db, session, task)
    if business is not None:
        return business
    session_id = session.id
    with atomic_write(db):
        session, _ = _session(db, session_id, _now())
        task = _task(db, session, task_id)
        current = view(db, task)
        if current["status"] in {"completed", "cancelled"}:
            return current
        if text:
            _message(db, task, "user", text)
        from app.agent.crossflow import INTENTS, cancel_task
        if task.intent in INTENTS:
            cancel_task(db, session, task)
            return view(db, task)
        task.stop_requested = True
        if task.proposal_id and current["proposal"]["status"] == "pending_confirmation":
            from app.domain.operations import cancel_proposal
            cancel_proposal(db, session_id=session.id, proposal_id=task.proposal_id)
        _say(db, task, "任务已取消；已执行业务不会撤销。", "cancelled")
        return view(db, task)
