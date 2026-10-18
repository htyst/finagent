"""Unified Live semantics, bounded read loop and durable, non-authorizing memory.

The LLM never receives a commit tool. All writes still pass the existing domain
flows and fixed forms. No network call runs under a database write lock.
"""
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
import re
from time import monotonic

from sqlalchemy import select
from fastapi import HTTPException
from app.agent import registry
from app.agent.model_budget import budget, SEMANTIC_DISPATCH
from app.agent.privacy import model_payload
from app.data.atomic import atomic_write, commit
from app.data.models import Account, AgentMessage, AgentStep, Card, Recipient
from app.domain.errors import DomainError
from app.agent.read_evidence import memory_reference

READ_TOOLS = frozenset({'read_service_requests','read_credit_cards','read_tool_catalog','read_wealth_products','read_risk_assessment','read_investment_performance','read_birthday_offers','financial_planning_basis', 'list_accounts', 'list_cards', 'list_recipients', 'list_transactions',
    'read_financial_profile', 'read_financial_history', 'analyze_finances',
    'summarize_spending', 'generate_statement', 'inspect_anomalies', 'read_wealth',
    'read_card_management', 'read_subscriptions', 'identify_subscriptions', 'read_birthdays',
    'read_agent_status', 'list_schedules', 'list_collections', 'search_knowledge'})
WRITE_INTENTS = frozenset({'transfer', 'card_action', 'schedule_transfer', 'aa_collection',
    'wealth_buy', 'wealth_redeem', 'card_management', 'subscription_reminder', 'subscription_revoke',
    'birthday_create', 'birthday_change', 'birthday_cancel', 'birthday_cancel_item', 'birthday_retry',
    'conditional_transfer', 'investigate_card'})
CRITICAL = ('source', 'target', 'target_note', 'amount_yuan', 'card', 'schedule_text',
            'product', 'units', 'budget_yuan', 'birthday_date', 'collection_account')


def limits():
    def value(name, default, maximum):
        try:
            return max(1, min(maximum, int(os.getenv(name, str(default)))))
        except ValueError:
            return default
    return {'model_calls': value('DIALOGUE_MAX_MODEL_CALLS', 4, 6),
            'tools': value('DIALOGUE_MAX_TOOLS', 6, 8),
            'seconds': value('DIALOGUE_TIMEOUT_SECONDS', 55, 90)}


def compact(value, *, depth=0):
    """Bound data, not authority; explicitly describe truncation in the envelope."""
    if depth > 7:
        return '[嵌套内容已省略，以服务端原始证据为准]'
    if isinstance(value, dict):
        return {k: (v if k=='allowed_read_tools' else [compact(x,depth=depth+1) for x in v[:50]] if k in {'months','history','categories','contracts'} and isinstance(v,list) else compact(v, depth=depth+1)) for k, v in value.items() if k not in {'confirmation_token', 'csrf_token'}}
    if isinstance(value, list):
        return [compact(v, depth=depth+1) for v in value[:12]]
    if isinstance(value, str):
        return value[:800]
    return value


def context_for(db, task, current):
    context = json.loads(task.context_json)
    memory = context.get('dialogue_memory', {})
    rows = db.scalars(select(AgentMessage).where(AgentMessage.task_id == task.id)
        .order_by(AgentMessage.ordinal.desc()).limit(28)).all()
    recent = list(reversed(rows[:12]))
    older = list(reversed(rows[12:]))
    # Extractive summaries are labelled untrusted history. Structured facts below
    # remain authoritative; no model-generated narrative is used as an approval.
    earlier = [{'role': row.role, 'excerpt': row.content[:160]} for row in older]
    drafts = {key: context[key] for key in ('dialogue_slots', 'requirement', 'wealth_slots',
        'card_slots', 'birthday_slots', 'subscription_slots', 'service_draft', 'clarification', 'last_write_failure') if key in context}
    resources = {key: {k: value[k] for k in ('id', 'status', 'summary', 'execute_at_utc') if k in value}
        for key in ('proposal', 'wealth_order', 'card_request', 'service_request', 'subscription_request', 'birthday_request', 'schedule', 'collection')
        if isinstance((value := current.get(key)), dict)}
    # Safe references are scoped on every actual tool call. Never send a proof.
    result = {'task': {'id': task.id, 'intent': task.intent, 'status': current['status'],
                          'plan_version': task.plan_version, 'control': current['control'].get('state')},
        'drafts': drafts, 'fields': memory.get('fields', {}),
        'last_query': context.get('last_query'),
        'missing_fields': memory.get('missing_fields', []), 'goals': memory.get('goals', []),
        'conditions': memory.get('conditions', []), 'prohibitions': memory.get('prohibitions', []),
        'time_requirements': memory.get('time_requirements', []), 'hold_draft': memory.get('hold_draft', False),
        'original_user_request': memory.get('original_user_request'), 'recent_changes': memory.get('recent_changes', []),
        'active_consultation': memory.get('active_consultation'),
        'earlier_summary': {'type': 'extractive_untrusted_not_authority', 'items': earlier},
        'resources': resources, 'recent_evidence': [memory_reference(e) for e in memory.get('evidence', [])[-4:]],
        'previous_task_id': context.get('last_agent_task_id'),
        'reference_fields': context.get('reference_fields', {}),
        'allowed_actions': ['answer', 'clarify', 'read', 'prepare_fixed_form', 'modify_draft', 'cancel_unexecuted', 'pause'],
        'allowed_read_tools': [{'name': name, 'description': registry.DESCRIPTIONS[name],
            'parameters': registry.TOOL_MODELS[name].model_json_schema()} for name in sorted(READ_TOOLS)],
        'messages': [{'role': row.role, 'content': row.content} for row in recent]}
    bounded = compact(result)
    bounded['goals'] = memory.get('goals', [])  # Never silently trim business goals.
    bounded['messages'] = [{'role':row.role,'content':row.content if row.role=='user' else row.content[:1500]} for row in recent]
    return model_payload(bounded)


def envelope(name, args, data, ref, error=None):
    from app.agent.read_evidence import project
    safe = model_payload(data)
    reduced = compact(model_payload(project(name,data)))
    return {'tool': name, 'query': model_payload(args), 'source_ref': ref,
        'queried_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'scope': 'authenticated_owner_and_session', 'data': reduced,
        'coverage': {'status': 'error' if error else 'bounded_excerpt' if reduced != safe else 'as_returned_by_domain',
                     'domain': safe.get('coverage') if isinstance(safe, dict) else None,
                     'note': '摘要来自全量服务端聚合；逐笔仅节选，禁止对节选自行求总额；months/history保留完整当前样本，明细按工具参数分页续取'},
        'error': error, 'allowed_next_actions': ['clarify', 'retry_read', 'use_fixed_authorization_panel'] if error else ['explain', 'clarify', 'propose_fixed_form']}


def validate_reads(requests, tools_left):
    if len(requests) > tools_left:
        raise DomainError('TOOL_BUDGET_EXCEEDED', '本轮读取工具次数达到上限，未准备写操作', 409)
    checked = []
    # Validate the WHOLE batch before running anything. Unknown/write tools are
    # rejected, not repaired into a seemingly harmless operation.
    for request in requests:
        if request.name not in READ_TOOLS:
            raise DomainError('INVALID_TOOL', '模型请求了未允许的读取工具；未执行或准备操作', 422)
        try:
            args = json.loads(request.arguments_json)
            if not isinstance(args, dict):
                raise ValueError('object required')
            args = registry.validate(request.name, args)
        except (ValueError, TypeError) as error:
            failure=DomainError('MODEL_OUTPUT_MISMATCH', '读取工具参数未通过严格校验，未执行或准备操作', 422)
            failure.feedback={'tool':request.name,'argument_issues':[
                {'field':'.'.join(map(str,item['loc'])),'type':item['type']}
                for item in error.errors(include_input=False,include_context=False,include_url=False)][:8]
                if hasattr(error,'errors') else [{'type':'strict_JSON_object_required'}],
                'allowed_parameters':registry.TOOL_MODELS[request.name].model_json_schema(),
                'instruction':'只使用allowed_parameters中的属性和类型；日期/period等语义字段填在Understanding，工具参数不能混入其他工具属性。'}
            raise failure from error
        # Scope/ranges are validated by the same domain API. Bound pagination
        # additionally so a language request cannot exhaust the local service.
        if request.name == 'list_transactions' and not (1 <= args['limit'] <= 100 and 0 <= args['offset'] <= 10000):
            raise DomainError('MODEL_OUTPUT_MISMATCH', '流水查询分页超出受限范围', 422)
        checked.append((request.name, args))
    return checked


def normalize_reads(db, task, text, decision, checked):
    """Date/identifier grounding after semantic intent selection. Never replaces
    a user's goal with a lexical intent. Relative bill dates use the demo clock;
    live appointment dates still use their existing real-time domain clock.
    """
    from app.agent.analytics import INTENTS, resolve_read
    normalized = []
    for name,args in checked:
        if name in {'summarize_spending','generate_statement','inspect_anomalies','list_transactions','analyze_finances'} and (decision.intent in INTENTS or decision.intent=='financial_analysis'):
            context = json.loads(task.context_json)
            grounded_decision = decision.model_copy()
            if decision.source and decision.source not in text:
                # Generic "本人全部账户" is not an account identifier. A read
                # follows stored filters or the domain's explicit all-owner default.
                grounded_decision.source = None
            tool_intent={'summarize_spending':'spending','generate_statement':'statement','inspect_anomalies':'anomalies','list_transactions':'transactions','analyze_finances':'spending'}[name]
            expected, grounded, question = resolve_read(db, task, tool_intent, text, context, grounded_decision)
            if question or expected != ('summarize_spending' if name=='analyze_finances' else name):
                message = '读取工具和意图不一致，请重新选择所需业务和查询范围；账单相对日期按演示日期核对'
                failure = DomainError('MODEL_TOOL_INTENT_MISMATCH', message, 422)
                failure.feedback = {'intent':decision.intent,'rejected_tool':name,'expected_tool':expected,
                    'missing':question,'instruction':'普通消费统计intent=spending，tools选择summarize_spending；未明确本人单个账户时source=null；补齐理解后重新返回完整understand_request'}
                raise failure
            # Preserve model-selected pagination, not model-invented date scope.
            pagination = {key:args[key] for key in ('limit','offset','evidence_limit','evidence_offset') if key in args}
            # Explicitly grounded read filters survive normalization; unknown
            # or ambiguous cards are rejected instead of broadening the scope.
            for field in ('card_id','merchant','type','direction','status','sort','category'):
                if field in args and args[field] is not None:
                    if field=='category' and grounded.get('category'):continue
                    if field=='card_id':
                        from app.domain.financial_queries import resolve_card_reference
                        selected=resolve_card_reference(db,task.owner_id,args[field])
                        args[field]=selected['id']
                        previous=context.get('last_query',{}).get('args',{}).get('card_id')
                        from app.domain.financial_queries import card_directory
                        unique_credit='信用卡' in text and selected.get('card_type')=='credit' and sum(c.get('card_type')=='credit' for c in card_directory(db,task.owner_id))==1
                        if not unique_credit and not any(str(v) in text for v in (selected['id'],selected['alias'],selected['card_last4'])) and previous!=args[field]:
                            raise DomainError('MODEL_OUTPUT_MISMATCH','银行卡筛选未在用户请求或唯一上下文中明确，请核对卡尾号',422)
                    grounded[field]=args[field]
            if name=='analyze_finances':
                grounded={k:v for k,v in grounded.items() if k in registry.Analysis.model_fields}
                grounded.update(kind=args['kind'],top_n=args['top_n'])
            allowed=registry.TOOL_MODELS[name].model_fields
            unsupported={k:v for k,v in grounded.items() if k not in allowed and v is not None}
            if unsupported:raise DomainError('MODEL_TOOL_INTENT_MISMATCH','当前查询条件不适用于这项分析，请明确查询范围；没有扩大原条件',422)
            grounded={k:v for k,v in grounded.items() if k in allowed}
            try:args = registry.validate(name, dict(grounded, **pagination))
            except (ValueError,TypeError) as error:raise DomainError('MODEL_OUTPUT_MISMATCH','分析参数未通过校验，请核对查询范围',422) from error
        normalized.append((name,args))
    return normalized


def invalidate(db, session, task):
    """Revoke old fixed forms/proofs, preserving local draft facts and receipts."""
    from app.domain.operations import invalidate_agent_proposal
    from app.domain.wealth import invalidate_task_order
    from app.domain.card_management import invalidate_task
    from app.domain.subscriptions import invalidate_task as invalidate_subscription
    from app.domain.birthdays import invalidate_task as invalidate_birthday
    if task.proposal_id:
        from app.domain.operations import get_proposal
        proposal = get_proposal(db, session_id=session.id, proposal_id=task.proposal_id)
        if proposal['status'] == 'pending_confirmation':
            invalidate_agent_proposal(db, session_id=session.id, proposal_id=task.proposal_id)
            task.proposal_id = None
    invalidate_task_order(db, task)
    invalidate_task(db, task)
    invalidate_subscription(db, task)
    invalidate_birthday(db, task)
    from app.domain.servicing import invalidate_task as invalidate_service
    invalidate_service(db,task)
    from app.agent.business_flow import cancel_business
    context = json.loads(task.context_json)
    # Already-authorized background plans are not silently cancelled by a draft
    # edit. Their own fixed change/cancel flow is mandatory.
    if context.get('schedule_id') or context.get('collection_id'):
        from app.agent.business_flow import public_state
        state = public_state(db, task)
        record = state.get('schedule') or state.get('collection')
        if record and record['status'] == 'pending_confirmation':
            cancel_business(db, session, task)
            context = json.loads(task.context_json)
            context.pop('schedule_id', None); context.pop('collection_id', None)
            task.context_json = json.dumps(context, ensure_ascii=False)
    task.plan_version += 1
    task.status = 'gathering_info'


def previous_fields(context):
    values = dict(context.get('dialogue_slots', {}))
    values.update({k: v.get('value') for k, v in context.get('dialogue_memory', {}).get('fields', {}).items()
                   if v.get('origin') != 'inferred'})
    return values


def ground_fields(db, task, text, decision, context):
    """Model guesses never become confirmed facts. Exact identifiers/amounts are
    checked against the latest user's words or a single established draft.
    Field provenance does NOT count as user approval of a fixed form.
    """
    from app.agent.requirements import MONEY, yuan_minor
    fields = context.setdefault('dialogue_memory', {}).setdefault('fields', {})
    prior = previous_fields(context)
    sources = {s.field: s for s in decision.dialogue.field_sources} if decision.dialogue else {}
    labels = db.scalars(select(Account).where(Account.owner_id == task.owner_id)).all()
    cards = db.scalars(select(Card).where(Card.owner_id == task.owner_id)).all()
    recipients = db.scalars(select(Recipient).where(Recipient.owner_id == task.owner_id)).all()
    if decision.intent == 'transfer':
        from app.domain.recipient_directory import note_reference
        explicit_note, note_error = note_reference(text)
        if not explicit_note and not note_error:
            # A name/tail is not a saved remark. Do not let an optional model
            # slot reinterpret the user's identifier into an unrelated directory.
            decision.target_note = prior.get('target_note')
        tail = re.fullmatch(r'(?:选(?:择)?(?:收款人)?|换成|改成|改为)?\s*尾号\s*([0-9]{4})[。！! ]*', text)
        if tail:
            matches = [row for row in labels + recipients if row.account_last4 == tail[1]]
            if len(matches) == 1:
                # User-selected identifier, grounded locally after DeepSeek chose
                # transfer. It is not an inferred recipient or authorization.
                decision.target = '尾号' + tail[1]
                sources.pop('target', None)
    unresolved = []
    for key in CRITICAL:
        value = getattr(decision, key)
        if value is None:
            continue
        source = sources.get(key)
        inherited = prior.get(key) == value and (decision.follow_up or decision.dialogue and decision.dialogue.behavior in {'supplement', 'modify', 'continue'})
        if (key in {'source', 'card'} and source and source.origin == 'unique_reference'
                and source.quote and source.quote in text
                and context.get('reference_fields', {}).get(key, {}).get('value') == value):
            inherited = True
        explicit = str(value) in text
        if key in {'amount_yuan', 'budget_yuan', 'units'}:
            amount = int(Decimal(value) * 100) if key != 'units' else int(Decimal(value) * 100)
            parsed = [yuan_minor(m.group(1)) for m in MONEY.finditer(text)]
            if source and source.quote and source.quote in text:
                quote = source.quote.strip().removesuffix('份').removesuffix('元').removesuffix('块')
                parsed.append(yuan_minor(quote))
            # Bare numeric corrections are valid explicit amounts, not inferred.
            if str(value) in re.findall(r'(?<![0-9])([0-9]+(?:\.[0-9]{1,2})?)(?![0-9])', text):
                parsed.append(amount)
            explicit = amount in parsed
            correction = re.search(r'(?:改成|改为|改到|调整为|调整到)\s*([0-9零〇一二两三四五六七八九十百千万]+(?:[.点][0-9零〇一二两三四五六七八九]+)?)', text)
            if correction and key == 'amount_yuan':
                if yuan_minor(correction[1]) == amount:
                    explicit = True
                else:
                    explicit = inherited = False
        elif key in {'source', 'collection_account', 'card', 'target'}:
            rows = cards if key == 'card' else labels + recipients if key == 'target' else labels
            from app.agent.language import _match
            matches = _match(value, rows, card=key == 'card')
            previous_matches = _match(prior.get(key), rows, card=key == 'card')
            if decision.follow_up and len(matches)==len(previous_matches)==1 and matches[0].id==previous_matches[0].id:
                inherited = True
            explicit = explicit or bool(len(matches) == 1 and any(
                ref and ref in text for ref in (getattr(matches[0], 'alias', None), getattr(matches[0], 'name', None),
                    getattr(matches[0], 'account_last4', None), getattr(matches[0], 'card_last4', None))))
        if source and source.origin == 'inferred':
            explicit = inherited = False
        if source and source.origin == 'current_user' and (not source.quote or source.quote not in text):
            explicit = False
        if not explicit and not inherited:
            unresolved.append(key)
            setattr(decision, key, None)
            continue
        fields[key] = {'value': value, 'origin': 'current_user' if explicit else 'prior_draft',
            'source_message': task.updated_at_utc, 'explicit_selection': bool(explicit),
            'approved_fixed_form': False}
    return unresolved


def clear_fields(context, keys):
    for key in keys:
        context.get('dialogue_memory', {}).get('fields', {}).pop(key, None)
        context.get('dialogue_slots', {}).pop(key, None)
        context.get('requirement', {}).pop(key, None)
        if key == 'target':
            for name in ('target', 'target_remark', 'direct_recipient', 'direct_recipient_active',
                         'phone_choices', 'remark_choices', 'ambiguous_name'):
                context.pop(name, None)
            context.get('dialogue_slots', {}).pop('target_note', None)
            context.get('requirement', {}).pop('target_note', None)
        if key == 'source': context.pop('source_account_id', None)
        if key == 'amount_yuan':
            context.pop('amount_minor', None)
            context.get('requirement', {}).pop('amount_minor', None)
        # Dedicated flows retain their own authoritative typed draft. A rejected
        # field must be removed there too, never revived by inheritance.
        mapping = {'source': ('account', 'account'), 'card': ('card', 'card'),
                   'amount_yuan': ('amount_yuan', 'amount'), 'product': ('product', 'product'), 'units': ('units', 'units')}
        if key in mapping:
            wealth, card = mapping[key]
            context.get('wealth_slots', {}).pop(wealth, None)
            context.get('card_slots', {}).pop(card, None)


def remember(task, text, decision):
    context = json.loads(task.context_json)
    memory = context.setdefault('dialogue_memory', {})
    turn = decision.dialogue
    memory['version'] = 1
    memory['last_behavior'] = turn.behavior if turn else 'legacy_semantic'
    memory['last_semantic_intent'] = decision.intent
    if decision.intent=='wealth_guidance':
        memory['active_consultation']={'kind':'wealth_guidance','latest_question':text,'authority':'read_only'}
    elif decision.intent in WRITE_INTENTS:
        memory.pop('active_consultation',None)
    memory['missing_fields'] = decision.missing_fields or []
    # Keep original instructions even when a model summary omits a clause.
    goals = memory.setdefault('goals', [])
    if turn and turn.behavior in {'new_task', 'switch_topic'} and decision.intent in WRITE_INTENTS:
        goals = []
    from app.agent.privacy import scrub_auth
    incoming = scrub_auth([g.model_dump() for g in turn.goals]) if turn else []
    if not incoming: incoming = [{'text': text, 'kind': 'write' if decision.intent in WRITE_INTENTS else 'read'}]
    if turn and turn.behavior in {'modify', 'supplement'} and len([g for g in goals if g['kind']=='write'])==1 and len([g for g in incoming if g['kind']=='write'])==1:
        goals = [g for g in goals if g['kind']!='write']
    for goal in incoming:
        if goal not in goals: goals.append(goal)
    if len(goals) > 32:
        raise DomainError('GOAL_BUDGET_EXCEEDED', '当前对话目标达到上限，请先处理或取消已有目标后新建任务', 409)
    memory['goals'] = goals
    if decision.intent in WRITE_INTENTS:
        memory.setdefault('original_user_request', scrub_auth(text))
        memory['recent_changes'] = (memory.get('recent_changes', []) + [scrub_auth(text)])[-4:]
    memory['last_instruction'] = scrub_auth(text)
    for key in ('conditions', 'prohibitions', 'time_requirements'):
        values = getattr(turn, key) if turn else []
        if values:
            memory[key] = list(dict.fromkeys(memory.get(key, []) + values))[-16:]
    task.requested_outcomes_json = json.dumps(goals, ensure_ascii=False)
    task.context_json = json.dumps(context, ensure_ascii=False)
    return context


def safe_explanation(decision, evidence):
    """Conservative rendering guard; structured financial cards stay authoritative.

    Free text never becomes a transaction parameter. Reject unfounded numeric
    statements and strong success/fraud claims, even if the JSON schema passed.
    """
    text = decision.reply
    if not text:
        return None
    # Ordered list markers are presentation, not unsupported financial amounts.
    text = re.sub(r'(?m)^\s*[0-9]{1,2}[.、]\s+', '• ', text)
    turn = decision.dialogue
    from app.agent.read_evidence import QUANTITATIVE
    if any(e['tool'] in QUANTITATIVE for e in evidence):
        return None  # trusted domain prose owns all financial amounts, scope and completion
    if evidence:
        refs = {e['source_ref'] for e in evidence}
        refs.update(item['source_ref'] for e in evidence if e['tool'] == 'search_knowledge'
                    for item in (e.get('data') or {}).get('items', []))
        if not turn or not turn.evidence_refs or not set(turn.evidence_refs) <= refs:
            return None
        supported = json.dumps(evidence, ensure_ascii=False)
        # Include canonical yuan representations computed from server minor units.
        def numbers(item):
            result = []
            if isinstance(item, dict):
                for key, value in item.items():
                    if isinstance(value, int) and (key.endswith('_minor') or key.endswith('_hundredths')):
                        result += [f'{value/100:.2f}', f'{value/100:g}', f'{value/100:,.2f}']
                    result += numbers(value)
            elif isinstance(item, list):
                for value in item: result += numbers(value)
            return result
        supported += ' ' + ' '.join(numbers(evidence))
        if decision.intent=='wealth_guidance':
            # Dates, URLs and tool IDs are not evidence for an allocation,
            # return or a suggested number of months in financial guidance.
            supported=' '.join(row['text'] for e in evidence if e['tool']=='search_knowledge'
                for row in (e.get('data') or {}).get('items',[]))
        if any(n.replace(',', '') not in supported.replace(',', '') for n in re.findall(r'\d[\d,]*(?:\.\d+)?', text)):
            return None
    elif re.search(r'\d|余额.*(?:元|万)|已经?(?:转账|扣款|挂失|冻结)|成功|回执', text):
        return None
    if re.search(r'(?:确认|确定|就是|确实).*盗刷|(?:已|已经)(?:成功)?(?:转账|扣款|冻结|解冻|挂失|撤销|取消)|保证收益|拆(?:成|分).*转|降低.*(?:验证|认证)', text):
        return None
    return text


def evidence_reply(decision, evidence):
    from app.agent.service import _summary
    explanation = safe_explanation(decision, evidence)
    factual = []
    seen=set()
    for item in sorted(evidence,key=lambda e:0 if e['tool'] in {'summarize_spending','generate_statement'} else 1):
        key=(item['tool'],json.dumps({k:v for k,v in item.get('query',{}).items() if k not in {'evidence_limit','evidence_offset','limit','offset'}},sort_keys=True))
        if key in seen:continue
        seen.add(key)
        if item.get('error'):
            factual.append(item['error']['message'] + '；当前没有获得该项完整数据。')
            continue
        if item['tool'] in {'list_accounts', 'list_cards', 'list_transactions', 'summarize_spending', 'generate_statement', 'inspect_anomalies'}:
            if item['tool']=='summarize_spending' and decision.dialogue and decision.dialogue.presentation!='detailed':
                from app.agent.conversation_reads import brief_spending
                factual.append(brief_spending(item['raw_data']))
            elif item['tool']=='inspect_anomalies' and decision.dialogue and decision.dialogue.presentation!='detailed':
                from app.agent.conversation_reads import brief_anomalies
                factual.append(brief_anomalies(item['raw_data']))
            else:factual.append(_summary(item['tool'], item['raw_data']))
        elif item['tool'] == 'read_financial_profile':
            data = item['raw_data']
            if not data.get('available'):
                factual.append('当前用户尚未导入完整金融档案。')
            else:
                b = data['balances']
                factual.append('本人金融档案：活期存款 {:.2f} 元，可用活期 {:.2f} 元，定期本金 {:.2f} 元；基金市值 {:.2f} 元，房车估值 {:.2f} 元，贷款及信用卡欠款 {:.2f} 元，净资产 {:.2f} 元。'.format(
                    b['demand_minor']/100,b['available_minor']/100,b['term_minor']/100,
                    (b['investment_snapshot_minor']+b['new_app_wealth_minor'])/100,
                    b['physical_minor']/100,b['liabilities_snapshot_minor']/100,b['net_worth_minor']/100))
                factual.append(data['snapshot_note'])
        elif item['tool'] in {'read_subscriptions','identify_subscriptions'}:
            from app.domain.subscription_read import summary_text
            data=item['raw_data']
            if isinstance(data,dict) and ('summary' in data or 'confirmed' in data):
                factual.append(summary_text(data.get('confirmed',data)))
                if item['tool']=='identify_subscriptions':
                    factual.append(f"未知合同的规律候选{len(data.get('candidates',[]))}项；疑似规律不等于确认合同。")
            else:factual.append('已查询疑似规律，合同与金额以订阅档案及明确授权为准。')
        elif item['tool'] == 'analyze_finances':
            from app.agent.conversation_reads import brief_analysis
            factual.append(brief_analysis(item['raw_data']))
        elif item['tool'] == 'read_financial_history':
            from app.agent.read_evidence import history_text
            factual.append(history_text(item['raw_data']))
        elif item['tool'] == 'read_wealth':
            data=item['raw_data']
            factual.append(f"本系统新交易持仓{len(data.get('positions',[]))}项；导入快照基金{len(data.get('imported_positions',[]))}项。导入持仓不能直接通过本系统赎回。")
        elif item['tool'] == 'search_knowledge':
            if not explanation:
                factual += [f"{row['title']}：{row['text']}〔{row['source_ref']}〕" for row in item['data']['items'][:2]]
        elif item['tool'] == 'read_agent_status':
            data = item['data']; proposal = data.get('proposal')
            if proposal:
                receipt = proposal.get('receipt')
                factual.append('原操作单状态：' + proposal['status'] + '。' +
                    (receipt['message'] + '。回执编号：' + str(receipt.get('order_id', proposal['id'])) if receipt else '未取得成功回执，不重新发起交易。'))
            else:
                factual.append('服务端任务状态：' + data['status'] + '，未重新发起交易。')
        elif not explanation:
            factual.append('已读取' + registry.DESCRIPTIONS[item['tool']] + '详情以服务端业务区域为准。')
    text = '\n'.join(factual)
    if explanation:
        text += ('\n' if text else '') + explanation
        if decision.dialogue and decision.dialogue.evidence_refs and not all(ref in explanation for ref in decision.dialogue.evidence_refs):
            text += '\n依据：' + '、'.join(decision.dialogue.evidence_refs)
    return text or '暂时没有足够证据形成结论，请补充要查询的范围或对象。'


def apply(db, session, task, text, current, decision, evidence):
    from app.agent.service import _say, view, _apply_live_decision, cancel
    turn = decision.dialogue
    context = remember(task, text, decision)
    behavior = turn.behavior if turn else None
    memory = context['dialogue_memory']
    from app.agent.conversation_reads import stage_subscription_access
    if stage_subscription_access(task,text,evidence,decision):
        if task.intent is None:task.intent=decision.intent
        _say(db,task,'可以先帮你查看订阅合同、自动续费和历史扣费。请在下面确认查询范围，确认后我会直接继续；这一步只查询，不会扣款或撤销代扣。','gathering_info')
        return view(db,task)
    if behavior == 'confirmation':
        _say(db, task, '聊天确认不执行操作。请核对固定操作单，使用页面确认及独立验证。', task.status)
        return view(db, task)
    active = [key for key in ('proposal', 'wealth_order', 'card_request', 'subscription_request', 'birthday_request', 'schedule', 'collection')
              if isinstance(current.get(key), dict) and current[key]['status'] in {'pending_confirmation', 'scheduled', 'collecting'}]
    if behavior == 'cancel' or decision.intent == 'cancel':
        if len(active) > 1 or turn and turn.needs_clarification:
            _say(db, task, '当前有多项可取消对象，请明确要取消哪一笔；已执行结果保持原记录。', 'gathering_info')
            return view(db, task)
        result = cancel(db, session, task.id)
        return result
    if behavior in {'pause', 'pause_query'}:
        invalidate(db, session, task)
        context = json.loads(task.context_json)
        context.setdefault('dialogue_memory', {}).update(hold_draft=True)
        task.context_json = json.dumps(context, ensure_ascii=False)
        if behavior == 'pause':
            from app.agent.control import change
            change(db, task, 'pause', '用户自然语言要求暂停')
            _say(db, task, '任务已暂停，保留草稿；旧操作单失效，没有自动执行。可使用恢复检查点按钮。', 'paused')
            return view(db, task)
    if behavior == 'result_query':
        # The model may select the read but cannot substitute a new write.
        if not evidence:
            from app.agent.service import _record_step, MAX_STEPS
            if db.query(AgentStep).filter(AgentStep.task_id==task.id).count()>=MAX_STEPS:
                raise DomainError('TOOL_BUDGET_EXCEEDED','任务读取步骤已达上限，请从页面读取原回执',409)
            args = {'task_id': context.get('last_agent_task_id') or task.id}
            data = registry.execute(db, session_id=session.id, owner_id=session.owner_id,
                name='read_agent_status', arguments=args, agent_task_id=task.id)
            evidence = [dict(envelope('read_agent_status', args, data, 'task:' + data['id']), raw_data=data)]
            _record_step(db, task, 'read_agent_status', args, data)
        _say(db, task, evidence_reply(decision, evidence), 'gathering_info' if active else 'completed')
        return view(db, task)
    if turn and turn.clear_fields:
        invalidate(db, session, task)
        context = json.loads(task.context_json)
        clear_fields(context, turn.clear_fields)
        for key in turn.clear_fields:
            setattr(decision, key, None)
        task.context_json = json.dumps(context, ensure_ascii=False)
    if decision.intent in WRITE_INTENTS:
        if isinstance(current.get('proposal'), dict) and current['proposal']['status'] == 'executed':
            _say(db, task, '原操作已执行，不能修改或撤回原回执。如需另一笔业务，请明确新建；本次没有重复提交。', 'completed')
            return view(db, task)
        if turn and len([g for g in turn.goals if g.kind == 'write']) > 1:
            invalidate(db, session, task)
            _say(db, task, '已保留全部目标：' + '；'.join(g['text'] for g in memory['goals']) +
                '。当前每个计划最多一项写操作，请明确先办理哪一项，其他目标继续保留；没有准备或执行新业务。', 'gathering_info')
            return view(db, task)
        if turn and turn.conditions and decision.intent not in {'conditional_transfer', 'investigate_card'}:
            invalidate(db, session, task)
            _say(db, task, (turn.clarification or '已保留你的条件，请明确当前要准备的操作及条件。') + ' 未准备或执行新业务。', 'gathering_info')
            return view(db, task)
        context = json.loads(task.context_json)
        if behavior in {'new_task', 'switch_topic'}:
            invalidate(db, session, task)
            context = json.loads(task.context_json)
            clear_fields(context, list(CRITICAL))
            task.context_json = json.dumps(context, ensure_ascii=False)
        if behavior in {'modify', 'supplement', 'continue'}:
            decision.follow_up = True
        if behavior == 'modify':
            if len(active) > 1:
                _say(db, task, '当前有多笔业务，请先明确要修改哪一笔；没有修改或执行。', 'gathering_info')
                return view(db, task)
            invalidate(db, session, task)
            context = json.loads(task.context_json)
        unresolved = ground_fields(db, task, text, decision, context)
        context['dialogue_memory']['missing_fields'] = list(dict.fromkeys((decision.missing_fields or []) + unresolved))
        context['dialogue_memory']['hold_draft'] = False
        task.context_json = json.dumps(context, ensure_ascii=False)
        if unresolved or behavior == 'modify' and not any(getattr(decision, key) is not None for key in CRITICAL) and not (turn and turn.clear_fields):
            invalidate(db, session, task)
            context = json.loads(task.context_json)
            clear_fields(context, unresolved)
            if decision.intent=='transfer':task.intent='transfer'
            task.context_json = json.dumps(context, ensure_ascii=False)
            _say(db, task, '本轮关键字段没有得到用户明确内容或唯一上下文的支持，请重新明确' +
                '、'.join({'source':'付款账户','target':'收款对象','amount_yuan':'金额','card':'卡片','schedule_text':'执行时间'}.get(key,key) for key in unresolved) +
                '；旧操作单已失效，没有准备或执行新业务。', 'gathering_info')
            return view(db, task)
        if decision.intent in {'conditional_transfer', 'investigate_card'}:
            from app.agent.crossflow import send as cross_send, promote_transfer
            if task.intent == 'transfer' and decision.intent == 'conditional_transfer':
                task = promote_transfer(db, session, task)
            task.intent = decision.intent
            token = SEMANTIC_DISPATCH.set(True)
            try:
                cross_send(db, session, task, text, already_recorded=True)
            finally:
                SEMANTIC_DISPATCH.reset(token)
            return view(db, task)
        # Time requirements on ordinary writes cannot become an immediate write.
        if turn and turn.time_requirements and decision.intent not in {'schedule_transfer', 'birthday_create', 'birthday_change', 'subscription_reminder'}:
            invalidate(db, session, task)
            _say(db, task, '已保留指定时间：' + '；'.join(turn.time_requirements) + '。这项业务需要单独核对预约能力，未改成立即执行。', 'gathering_info')
            return view(db, task)
    if evidence and decision.intent not in WRITE_INTENTS:
        context = json.loads(task.context_json)
        pending = current.get('proposal') and current['proposal']['status'] == 'pending_confirmation'
        draft = context.get('dialogue_slots') or context['dialogue_memory'].get('hold_draft') or active
        failures=[e for e in evidence if e.get('error')]
        read_status='partially_completed' if failures and len(failures)<len(evidence) else 'failed' if failures else 'completed'
        if turn and (turn.needs_clarification or any(g.kind=='unsupported' for g in turn.goals)):
            read_status='partially_completed'
        if not draft:task.intent=decision.intent
        _say(db, task, ('已停止推进原草稿，旧操作单失效；本次只查询。\n' if behavior == 'pause_query' else '') +
            evidence_reply(decision, evidence), 'awaiting_confirmation' if pending and behavior != 'pause_query' else 'gathering_info' if draft else read_status)
        return view(db, task)
    if decision.intent == 'reply':
        text_reply = safe_explanation(decision, [])
        _say(db, task, text_reply or '当前没有足够的业务证据回答具体数据；可以先查询相关业务或查看支持范围。', 'gathering_info' if active else 'completed')
        return view(db, task)
    # Existing verified business services still own amount calculation, directory
    # resolution, fixed forms, permission/MFA and durable result links.
    original_intent = task.intent
    result = _apply_live_decision(db, session, task, text, view(db, task), decision)
    if decision.intent=='transfer' and isinstance(result.get('proposal'),dict):
        target=result['proposal']['summary'].get('target',{})
        context=json.loads(task.context_json)
        field=context.setdefault('dialogue_memory',{}).setdefault('fields',{}).get('target')
        if field and target.get('id'):
            field['resource_ref']=target['id']
            field['value']=context.get('dialogue_slots',{}).get('target',field['value'])
            task.context_json=json.dumps(context,ensure_ascii=False)
            commit(db)
    if decision.intent not in WRITE_INTENTS and active:
        # Specialized portfolio reads must not retire a different pending write.
        task.intent = original_intent
        task.status = 'awaiting_confirmation' if current.get('proposal') and current['proposal']['status']=='pending_confirmation' else 'gathering_info'
        commit(db)
        result = view(db, task)
    return result


def read_signature(name,args):
    fields=dict(args)
    if name in {'summarize_spending','generate_statement'}:fields.pop('evidence_limit',None)
    return name+json.dumps(fields,sort_keys=True)


def send(db, session, task, text):
    from app.agent.language import interpret, payload_for
    from app.agent.service import _task, _message, _say, view, _record_step, MAX_STEPS
    from app.agent.control import begin_request, assert_request
    from app.domain.operations import _session, _now
    sid, tid = session.id, task.id
    request_id = begin_request(db, tid)
    setting = limits()
    started = monotonic()
    evidence, checked_signatures = [], set()
    with atomic_write(db):
        task = _task(db, session, tid)
        context=json.loads(task.context_json);context.pop('pending_subscription_query',None);context.pop('pending_risk_assessment',None)
        if context.get('analysis_workflow',{}).get('status') in {'running','awaiting_confirmation'}:
            context['analysis_workflow']['status']='superseded'
        task.context_json=json.dumps(context,ensure_ascii=False)
        _message(db, task, 'user', text)
        current = view(db, task)
        payload = payload_for(db, task, current)
        payload['dialogue_context'] = context_for(db, task, current)
        payload['messages'] = payload['dialogue_context'].pop('messages')
        revision = task.updated_at_utc
    failure, decision = None, None
    first_turn = None
    guidance_only = False
    semantic_repairs = 0
    tools_left = setting['tools']
    with budget(setting['model_calls'], setting['seconds']) as used:
        try:
            while True:
                if evidence and used['remaining']<=0:break
                if len(json.dumps(payload, ensure_ascii=False)) > 64000:
                    raise DomainError('CONTEXT_BUDGET_EXCEEDED', '本轮上下文达到长度上限，保留已有目标，请新建任务或缩小查询范围', 409)
                decision = interpret(payload)
                from app.agent.consultation import normalize as normalize_consultation
                decision = normalize_consultation(decision, text, locked=guidance_only)
                guidance_only = decision.intent == 'wealth_guidance'
                # Personal consultation needs synthesis, rather than a raw data dump.
                from app.agent.consultation import general_guidance
                if decision.dialogue and not decision.dialogue.workflow and not evidence and decision.intent not in WRITE_INTENTS:
                    requests=decision.dialogue.tools
                    if (decision.intent=='wealth_guidance' and not general_guidance(text) and any(r.name!='search_knowledge' for r in requests)) or any(r.name=='financial_planning_basis' for r in requests):
                        from app.agent.dialogue_schema import AnalysisWorkflow,AnalysisNode
                        nodes=[AnalysisNode(id='read'+str(i),title=registry.DESCRIPTIONS.get(r.name,r.name).split('：',1)[0][:24],name=r.name,arguments_json=r.arguments_json) for i,r in enumerate(requests)]
                        nodes.append(AnalysisNode(id='answer',title='结合目标综合分析',name='compose_answer',depends_on=[n.id for n in nodes]))
                        decision.dialogue.workflow=AnalysisWorkflow(objective=text,nodes=nodes)
                if decision.dialogue and decision.dialogue.workflow and not evidence:
                    from app.agent.analysis_workflow import start as start_analysis
                    try:return start_analysis(db,session,task,text,current,decision,request_id,revision)
                    except DomainError as error:
                        if error.code!='INVALID_ANALYSIS_PLAN' or semantic_repairs>=2:raise
                        semantic_repairs+=1
                        payload['validation_feedback']={'code':error.code,'message':error.message,'instruction':'修正整个只读DAG，依赖必须指向前面的节点；终点compose_answer覆盖全部步骤，不包含写目标。'}
                        continue
                if first_turn is None and decision.dialogue:
                    first_turn = decision.dialogue.model_copy(deep=True)
                if monotonic() - started >= setting['seconds']:
                    raise DomainError('MODEL_BUDGET_EXCEEDED', '本轮总耗时已到，迟到结果未应用', 503)
                requests = decision.dialogue.tools if decision.dialogue else []
                if (decision.intent=='wealth_guidance' or decision.dialogue and decision.dialogue.behavior=='knowledge') and not any(e['tool']=='search_knowledge' for e in evidence) and not any(r.name=='search_knowledge' for r in requests):
                    from app.agent.dialogue_schema import ReadRequest
                    query=('通用理财建议 风险 期限 应急资金 '+text) if decision.intent=='wealth_guidance' else text
                    requests = list(requests)+[ReadRequest(name='search_knowledge',arguments_json=json.dumps({'query':query},ensure_ascii=False))]
                # A statistics intent needs the trusted calculator, even if the
                # model also asks for supporting individual transaction evidence.
                calculator={'spending':'summarize_spending','statement':'generate_statement','anomalies':'inspect_anomalies'}.get(decision.intent)
                if calculator and not any(e['tool']==calculator for e in evidence) and not any(r.name==calculator for r in requests):
                    from app.agent.dialogue_schema import ReadRequest
                    from app.agent.analytics import resolve_read
                    draft=json.loads(task.context_json)
                    _,args,question=resolve_read(db,task,decision.intent,text,draft,decision)
                    if not question:requests=list(requests)+[ReadRequest(name=calculator,arguments_json=json.dumps(args,ensure_ascii=False))]
                try:
                    checked = validate_reads(requests, tools_left)
                    checked = normalize_reads(db, task, text, decision, checked)
                except DomainError as error:
                    if error.code not in {'MODEL_TOOL_INTENT_MISMATCH','MODEL_OUTPUT_MISMATCH'} or semantic_repairs>=2:raise
                    semantic_repairs+=1
                    payload['validation_feedback']={'code':error.code,'message':error.message,'details':getattr(error,'feedback',None),'remaining_repairs':2-semantic_repairs}
                    continue
                # Repeated reads do not re-run. End the loop using the same
                # authoritative evidence; never turn an LLM repetition into a write.
                checked = [(name,args) for name,args in checked if read_signature(name,args) not in checked_signatures]
                if not checked:
                    if first_turn and decision.dialogue:
                        # Tool-loop answer cannot silently drop a goal/condition
                        # retained by the understanding that requested the reads.
                        if first_turn.goals:
                            decision.dialogue.goals = first_turn.goals
                        for key in ('conditions', 'prohibitions', 'time_requirements'):
                            setattr(decision.dialogue, key, list(dict.fromkeys(getattr(first_turn,key) + getattr(decision.dialogue,key))))
                        if first_turn.behavior in {'pause_query', 'modify', 'cancel', 'result_query'}:
                            decision.dialogue.behavior = first_turn.behavior
                        decision.dialogue.clear_fields = list(dict.fromkeys(first_turn.clear_fields + decision.dialogue.clear_fields))
                    break
                with atomic_write(db):
                    session, _ = _session(db, sid, _now())
                    task = _task(db, session, tid)
                    assert_request(db, tid, request_id)
                    if task.updated_at_utc != revision:
                        return view(db, task)
                    if first_turn and not checked_signatures and first_turn.behavior in {'pause_query', 'pause', 'modify'}:
                        # Revoke the old form BEFORE a potentially slow read or
                        # second model call. A concurrent old confirmation must fail.
                        invalidate(db, session, task)
                        context = json.loads(task.context_json)
                        if first_turn.clear_fields: clear_fields(context, first_turn.clear_fields)
                        if first_turn.behavior in {'pause_query', 'pause'}:
                            context.setdefault('dialogue_memory', {})['hold_draft'] = True
                        task.context_json = json.dumps(context, ensure_ascii=False)
                    for name, args in checked:
                        if name == 'read_agent_status' and first_turn and first_turn.behavior == 'result_query' and not args['task_id']:
                            args['task_id'] = json.loads(task.context_json).get('last_agent_task_id') or task.id
                        signature = read_signature(name,args)
                        if signature in checked_signatures:
                            raise DomainError('TOOL_REPEAT_REJECTED', '本轮重复读取已阻止，请使用已有证据或重新说明需求', 422)
                        checked_signatures.add(signature)
                        if db.query(AgentStep).filter(AgentStep.task_id == tid).count() >= MAX_STEPS:
                            raise DomainError('TOOL_BUDGET_EXCEEDED', '任务持久步骤达到上限，请新建任务继续查询', 409)
                        error = None
                        try:
                            data = registry.execute(db, session_id=sid, owner_id=session.owner_id,
                                name=name, arguments=args, agent_task_id=tid)
                        except DomainError as problem:
                            # Business read failures become evidence, NEVER a write retry.
                            error = {'code': problem.code, 'message': problem.message}
                            data = None
                        except HTTPException as problem:
                            detail = problem.detail if isinstance(problem.detail, dict) else {}
                            error = {'code': detail.get('error_code', 'READ_REJECTED'),
                                     'message': detail.get('message', '读取请求被后端校验拒绝')}
                            data = None
                        ref = 'tool:' + tid + ':' + str(db.query(AgentStep).filter(AgentStep.task_id == tid).count()+1)
                        item = envelope(name, args, data, ref, error)
                        _record_step(db, task, name, args, {'error': error} if error else data)
                        evidence.append(dict(item, raw_data=data))
                        tools_left -= 1
                    context = json.loads(task.context_json)
                    memory = context.setdefault('dialogue_memory', {})
                    from app.agent.read_evidence import memory_reference
                    memory['evidence'] = [memory_reference(e) for e in evidence[-6:]]
                    for name,args in checked:
                        if name in {'summarize_spending','generate_statement','inspect_anomalies','list_transactions'}:
                            context['last_query'] = {'intent': {'summarize_spending':'spending','generate_statement':'statement',
                                'inspect_anomalies':'anomalies','list_transactions':'transactions'}[name], 'args':args}
                    task.context_json = json.dumps(context, ensure_ascii=False)
                    commit(db)
                    payload['tool_results'] = [{k: v for k, v in e.items() if k != 'raw_data'} for e in evidence]
                    payload['tool_loop'] = {'remaining_tools': tools_left,'remaining_model_calls':used['remaining'], 'instruction': '优先直接回答。已返回汇总即为全量计算，禁止为解释同一结果继续读不同页面或无关工具；只在用户明确目标尚未满足时补查。不要机械列出全部类别，默认结论+重点+下一步。剩最后一次调用时tools=[]，说明已完成和未完成的部分。'}
                    payload['dialogue_context'] = context_for(db, task, view(db, task))
                    payload['messages'] = payload['dialogue_context'].pop('messages')
                    revision = task.updated_at_utc  # Our own domain synchronization, under the same lock.
                if any((e.get('error')or{}).get('code')=='SUBSCRIPTION_CONFIRMATION_REQUIRED' for e in evidence):break
        except DomainError as error:
            failure = error
    with atomic_write(db):
        session, _ = _session(db, sid, _now())
        task = _task(db, session, tid)
        try:
            assert_request(db, tid, request_id)
        except DomainError:
            return view(db, task)
        if task.updated_at_utc != revision or task.stop_requested:
            return view(db, task)
        current = view(db, task)
        context = json.loads(task.context_json)
        context.setdefault('dialogue_memory', {})['last_budget'] = {
            'model_calls': used['calls'], 'tool_calls': setting['tools']-tools_left,
            'semantic_repairs': semantic_repairs,
            'seconds': round(monotonic()-started, 3), 'failure': failure.code if failure else None}
        if failure and hasattr(failure,'feedback'):
            context['dialogue_memory']['last_validation_error'] = failure.feedback
        task.context_json = json.dumps(context, ensure_ascii=False)
        if failure and decision and decision.intent not in WRITE_INTENTS and evidence and failure.code in {'MODEL_BUDGET_EXCEEDED','MODEL_UNAVAILABLE','MODEL_RATE_LIMITED','CONTEXT_BUDGET_EXCEEDED','TOOL_BUDGET_EXCEEDED'}:
            successful=[e for e in evidence if not e.get('error')]
            if successful:
                # Keep the trusted facts even when the optional synthesis call fails.
                if decision.dialogue and first_turn:decision.dialogue.goals=first_turn.goals
                result=apply(db,session,task,text,current,decision,evidence)
                _say(db,task,'已保留上面的查询结果；补充分析暂未完成。可以继续问某一项，或展开已有报告查看。', 'awaiting_confirmation' if current.get('proposal') and current['proposal']['status']=='pending_confirmation' else 'partially_completed')
                return view(db,task)
        if failure:
            _say(db, task, failure.message + '。Live未降级为Mock；可查看已有任务状态、回执或使用停止控制。',
                 'awaiting_confirmation' if current.get('proposal') and current['proposal']['status']=='pending_confirmation' else 'gathering_info')
            return view(db, task)
        try:
            return apply(db, session, task, text, current, decision, evidence)
        except DomainError as error:
            _say(db, task, error.message + '。未自动执行业务，可查看原单状态或请求人工接管。', 'gathering_info')
            return view(db, task)
