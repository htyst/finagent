"""Recoverable questions are dialogue state, never execution authority."""
from __future__ import annotations

import json
import re
from sqlalchemy import select
from app.data.atomic import atomic_write
from app.data.models import Account

GIFT_QUESTION = ('你说的“划一笔钱”，是想转账准备买礼物的钱，还是先规划礼物预算？'
                 '也可以明确选择安排鲜花和蛋糕。预算规划不会锁定或扣款。')
INTENT_LABELS = {'transfer':'转账', 'birthday_create':'生日鲜花和蛋糕安排', 'budget':'预算规划',
                 'birthday_change':'修改生日安排', 'birthday_cancel':'取消生日安排',
                 'birthday_cancel_item':'取消蛋糕部分', 'birthday_retry':'重试蛋糕',
                 'birthday_status':'查询生日安排',
                 'accounts':'查余额', 'spending':'查消费', 'transactions':'查流水',
                 'statement':'生成账单报告', 'anomalies':'核查交易异常', 'cards':'查询卡片',
                 'card_action':'冻结或解冻卡片', 'card_management':'办理卡片管理',
                 'card_management_status':'查询卡片办理状态',
                 'schedule_transfer':'预约转账', 'aa_collection':'发起AA收款',
                 'wealth_buy':'申购模拟理财', 'wealth_redeem':'赎回模拟理财',
                 'wealth_assessment':'评估模拟理财', 'wealth_compare':'比较模拟理财',
                 'wealth_status':'查询理财办理状态', 'subscription_inspect':'查询订阅扣费',
                 'subscription_reminder':'设置续费提醒', 'subscription_revoke':'解除银行代扣',
                 'subscription_status':'查询订阅办理状态'}
FIELD_LABELS = {'source':'付款账户', 'target':'收款目标', 'amount_yuan':'转账金额',
                'budget_yuan':'总预算', 'birthday_date':'生日日期',
                'birthday_recipient':'收货人', 'birthday_address':'收货地址',
                'birthday_items':'要购买的商品', 'merchant':'商户', 'card':'卡片尾号',
                'hour':'小时', 'minute':'分钟', 'days_before':'提前天数',
                'product':'产品', 'units':'份额', 'participants':'参与人',
                'collection_account':'收款账户', 'payer':'垫付人', 'schedule_text':'预约时点'}
FIELD_LABELS.update({'action':'办理动作','applicant':'申请人资料','people':'人数','days':'天数',
                     'share_mode':'分摊方式','shares_yuan':'逐人分摊金额','time_zone':'时区',
                     'period':'查询期间','start':'开始日期','end':'结束日期'})
WRITE_KEYS = ('source_account_id','target','amount_minor','action','card_id','ambiguous_name',
              'target_remark','remark_choices','phone_choices','direct_recipient','direct_recipient_active',
              'dialogue_intent','dialogue_slots','birthday_kind','birthday_slots','birthday_structured_fields',
              'clarification_transfer_active')
READ_INTENTS = {'accounts','spending','transactions','statement','anomalies','cards','budget',
                'wealth_assessment','wealth_compare','wealth_status','card_management_status',
                'subscription_inspect','subscription_status','birthday_status'}


def explicit_transfer(text):
    return bool(re.search(r'转账|转给|转到|转入|划到|划给|拨到|拨给|给[^，,。；;]{0,40}转\s*[0-9一二两三四五六七八九十百千万]|从[^，,。；;]{1,40}转[^，,。；;]{0,20}(?:到|给|入)|从[^，,。；;]{1,40}转\s*[0-9一二两三四五六七八九十百千万]+(?:\.\d{1,2})?\s*(?:元|块)',text))


def negated_transfer(text):
    return bool(re.search(r'(?:不(?:用|要|想)?|别|先别|暂时别)(?:再)?转(?:账|钱)',text))


def consultation(text):
    return bool(re.search(r'怎么|如何|能否|是否|能不能|吗|[?？]|假设|举例|只是咨询|[“”「」]',text))


def transfer_choice(text):
    return explicit_transfer(text) and not negated_transfer(text) and not consultation(text)


def procurement_request(text):
    return bool(re.search(r'鲜花|花店|蛋糕',text) and re.search(r'订|买|下单|采购|安排',text)
                and not re.search(r'(?:不|别|不要|不用).{0,6}(?:订|买|下单|采购|安排)',text))


def gift_ambiguity(text):
    return bool(re.search(r'生日|庆生',text) and not explicit_transfer(text)
                and not procurement_request(text)
                and re.search(r'礼物|礼品|划.{0,12}钱|拨.{0,12}钱|留.{0,12}钱|预留.*预算|准备.*钱',text))


def _facts(db,task,text):
    # These facts keep their roles. A birthday budget is not a transfer amount,
    # and a recipient in birthday context is not a banking recipient.
    from app.agent.birthday_flow import _extract
    extracted,_=_extract(db,task,text)
    return {k:extracted[k] for k in ('birthday_date','birthday_source','budget_minor') if k in extracted}


def _invalidate_drafts(db,session,task, *, clear_slots=True):
    from app.agent.birthday_flow import _invalidate_other
    from app.domain.birthdays import invalidate_task
    _invalidate_other(db,session,task)
    invalidate_task(db,task)
    ctx=json.loads(task.context_json)
    ctx['birthday_version']=ctx.get('birthday_version',0)+1
    if clear_slots:
        for key in WRITE_KEYS:ctx.pop(key,None)
    task.context_json=json.dumps(ctx,ensure_ascii=False)
    return ctx


def _ask(db,session,task,text, *, facts=None,candidates=None,fields=None,clear_slots=True):
    from app.agent.service import _say,view,_dump
    candidates=candidates or ['transfer','budget','birthday_create']
    readonly=all(candidate in READ_INTENTS for candidate in candidates) or consultation(text)
    ctx=json.loads(task.context_json) if readonly else _invalidate_drafts(db,session,task,clear_slots=clear_slots)
    previous=ctx.get('clarification',{})
    facts={**previous.get('facts',{}),**(facts or {})}
    ctx['clarification']={'scope':'read' if readonly else 'gift_funding' if gift_ambiguity(text) or previous.get('scope')=='gift_funding' else 'intent',
                          'facts':facts,'candidates':candidates,
                          'missing_fields':fields or [],'source_text':text[:1000]}
    task.context_json=_dump(ctx);task.intent='clarify'
    if fields:
        labels=[FIELD_LABELS[k] for k in fields if k in FIELD_LABELS]
        question='请明确'+ '、'.join(dict.fromkeys(labels))+'，我会保留已经明确的信息，再继续准备；当前没有执行。' if labels else GIFT_QUESTION
    elif ctx['clarification']['scope']=='gift_funding':question=GIFT_QUESTION
    else:
        labels=[INTENT_LABELS[k] for k in ctx['clarification']['candidates'] if k in INTENT_LABELS]
        question='你是想'+ '，还是'.join(dict.fromkeys(labels))+'？请明确要办理哪一项，我再继续核对信息。' if labels else '你希望查询信息，还是准备一笔操作？请说明要办理的业务，我会继续追问缺少的信息。'
    _say(db,task,question,'awaiting_confirmation' if readonly and task.status=='awaiting_confirmation' else 'gathering_info')
    return view(db,task)


def apply_decision(db,session,task,text,decision):
    """Called only after the normal Live revision/cancellation checks."""
    from app.agent.service import _say,view,_dump,_resolve
    ctx=json.loads(task.context_json)
    readonly=decision.intent_candidates and all(candidate in READ_INTENTS for candidate in decision.intent_candidates)
    if not readonly and transfer_choice(text) and not re.search(r'预约|定时|AA|如果|每天|每月',text):
        # Clear business type + missing information stays in the mature transfer
        # resolver. Clarify metadata can never populate a payment parameter.
        restart=bool(re.search(r'重新(?:发起|转|办理)|(?:新|另)(?:的)?一笔',text))
        ctx=_invalidate_drafts(db,session,task,clear_slots=restart)
        # The grounded resolver owns this conversation from here on. Do not
        # leave a second, stale set of Live write slots available for reuse.
        ctx.pop('dialogue_intent',None);ctx.pop('dialogue_slots',None)
        task.intent='transfer';ctx.pop('clarification',None)
        ctx['clarification_transfer_active']=True
        _,_,question=_resolve(task,text,ctx,db)
        task.context_json=_dump(ctx)
        _say(db,task,question or '请核对付款账户、收款目标和金额，并明确要求准备转账；当前未生成或执行操作单。','gathering_info')
        return view(db,task)
    return _ask(db,session,task,text,facts=_facts(db,task,text),
                candidates=decision.intent_candidates,fields=decision.missing_fields)


def dispatch(db,session,task,text):
    """Pre-route ambiguous gift funding and recover explicit answers to questions."""
    from app.agent.service import _cancel_request,_task,_message,_say,view,_dump,_transfer_amount_text
    ctx=json.loads(task.context_json)
    pending=ctx.get('clarification')
    if _cancel_request(text) or task.stop_requested:return None
    if re.search(r'查.*余额|账户余额|查.*流水|查.*账单|查.*消费',text):return None

    # Alternatives are uncertainty, not instructions to pick the last amount or
    # the first account. Keep ordinary/scheduled/AA business boundaries intact.
    fields=[]
    if explicit_transfer(text) and not re.search(r'预约|定时|AA|如果|每天|每月',text):
        if re.search(r'或者|还是|或',text):
            amounts=set(re.findall(r'(\d+(?:\.\d{1,2})?)\s*(?:元|块)',_transfer_amount_text(text)))
            if len(amounts)>1:fields.append('amount_yuan')
            source_text=re.split(r'转|给|到|向|划|拨',text.split('从',1)[1],maxsplit=1)[0] if '从' in text else ''
            aliases=[a.alias for a in db.scalars(select(Account).where(Account.owner_id==task.owner_id)) if a.alias in source_text]
            if len(aliases)>1:fields.append('source')
    ambiguity=gift_ambiguity(text)
    if not ambiguity and not fields and not pending:
        if transfer_choice(text) and re.search(r'生日|庆生',text) and not procurement_request(text):
            # Ground the money/account roles even when the user has already
            # stated transfer as the action, without needing an intent question.
            sid,tid=session.id,task.id
            with atomic_write(db):
                from app.domain.operations import _session,_now
                session,_=_session(db,sid,_now());task=_task(db,session,tid)
                if task.stop_requested:return view(db,task)
                ctx=json.loads(task.context_json);ctx['clarification_transfer_active']=True
                task.context_json=_dump(ctx);task.intent='transfer'
        return None
    sid,tid=session.id,task.id
    if ambiguity or fields:
        with atomic_write(db):
            from app.domain.operations import _session,_now
            session,_=_session(db,sid,_now());task=_task(db,session,tid)
            if task.stop_requested:return view(db,task)
            _message(db,task,'user',text)
            # A new ambiguous request invalidates both kinds of in-flight results.
            readonly=consultation(text) or negated_transfer(text)
            ctx=json.loads(task.context_json) if readonly else _invalidate_drafts(db,session,task,clear_slots=not fields)
            if fields:
                from app.agent.service import _resolve
                task.intent='transfer'
                _resolve(task,text,ctx,db)
                if 'amount_yuan' in fields:ctx.pop('amount_minor',None);ctx.get('dialogue_slots',{}).pop('amount_yuan',None)
                if 'source' in fields:ctx.pop('source_account_id',None);ctx.get('dialogue_slots',{}).pop('source',None)
            ctx['clarification']={'scope':'gift_funding' if ambiguity else 'intent','facts':_facts(db,task,text),
                                  'candidates':['transfer','budget','birthday_create'] if ambiguity else ['transfer'],
                                  'missing_fields':fields,'source_text':text}
            task.context_json=_dump(ctx);task.intent='clarify'
            _say(db,task,'正在核对需求。','awaiting_confirmation' if readonly and task.status=='awaiting_confirmation' else 'gathering_info')
            revision=task.updated_at_utc
            if task.mode=='live':
                from app.agent.language import payload_for
                payload=payload_for(db,task,view(db,task))
        # Live still interprets the real message. A proposed type is not consent,
        # and failure to classify does not erase the deterministic question.
        if task.mode=='live':
            from app.agent.language import interpret
            from app.domain.errors import DomainError
            try:interpret(payload)
            except DomainError:pass
        with atomic_write(db):
            from app.domain.operations import _session,_now
            session,_=_session(db,sid,_now());task=_task(db,session,tid)
            if task.stop_requested or task.updated_at_utc!=revision:return view(db,task)
            return _ask(db,session,task,text,facts=_facts(db,task,text),fields=fields,
                        candidates=['transfer'] if fields else None,clear_slots=not fields)

    with atomic_write(db):
        from app.domain.operations import _session,_now
        session,_=_session(db,sid,_now());task=_task(db,session,tid)
        if task.stop_requested:return view(db,task)
        ctx=json.loads(task.context_json);pending=ctx.get('clarification')
        if not pending:return None
        if pending.get('scope')=='read':return None
        budget_choice=bool(re.search(r'预算|规划|不转账|不转钱|先留|先预留',text)) and not procurement_request(text)
        if transfer_choice(text):
            ctx.pop('clarification',None)
            if pending.get('candidates')!=['transfer']:
                for key in WRITE_KEYS:ctx.pop(key,None)
            ctx['clarification_transfer_active']=True;task.intent='transfer';task.status='gathering_info';task.context_json=_dump(ctx)
            return None
        birthday_choice=('birthday_create' in pending.get('candidates',[]) and
                         re.fullmatch(r'(?:我要|我想|选择|继续|就|还是)?\s*(?:这份|原来的)?生日(?:安排|采购)[。！!\s]*',text))
        if procurement_request(text) or birthday_choice:
            facts=pending.get('facts',{})
            ctx.pop('clarification',None);ctx['birthday_kind']='create';ctx['birthday_slots']={**ctx.get('birthday_slots',{}),**facts}
            task.context_json=_dump(ctx);task.intent='birthday_create';task.status='gathering_info'
            return None
        from app.agent.birthday_flow import detect as birthday_kind
        if not budget_choice and birthday_kind(text,ctx,task) in {'cancel','cancel_item','retry','change','status'}:
            ctx.pop('clarification',None);task.context_json=_dump(ctx)
            task.intent=None;task.status='gathering_info'
            return None
        if re.search(r'申购|赎回|冻结|解冻|挂失|限额|申请.*卡|订阅|续费|代扣|AA收款|预约|定时|取消.*生日|重试.*蛋糕|生日.*(?:取消|重试)|预算.*(?:改成|改为)',text):
            ctx.pop('clarification',None)
            task.context_json=_dump(ctx);task.intent=None;task.status='gathering_info'
            return None
        if budget_choice or pending.get('focus')=='budget':
            # Gift budgeting is read-only; it cannot reserve money independently.
            from app.agent.service import _minor
            amount=_minor(text)
            amount=amount if amount is not None else pending.get('facts',{}).get('budget_minor')
            _message(db,task,'user',text)
            if amount is None or amount<=0:
                pending['focus']='budget';ctx['clarification']=pending;task.context_json=_dump(ctx)
                _say(db,task,'礼物预算打算准备多少元？这是预算规划，不会锁定资金或扣款。','gathering_info')
            else:
                ctx.pop('clarification',None);ctx['gift_budget_minor']=amount;task.intent='budget';task.context_json=_dump(ctx)
                _say(db,task,f'礼物预算按{amount/100:.2f}元记录。这是预算规划，尚未锁定或扣款。实际付款仍需明确账户、对象并在页面确认；独立预留资金目前只支持已授权的鲜花蛋糕安排。','completed')
            return view(db,task)
        # Bare money/account answers resume a uniquely identified transfer.
        if pending.get('candidates')==['transfer']:
            ctx.pop('clarification',None);ctx['clarification_transfer_active']=True;task.intent='transfer'
            task.status='gathering_info';task.context_json=_dump(ctx);return None
        _message(db,task,'user',text)
        return _ask(db,session,task,text,facts=_facts(db,task,text))
