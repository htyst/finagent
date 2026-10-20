"""One live tool loop, with durable human checkpoints and no model commit authority."""
import json,os,re
from copy import deepcopy
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4
from fastapi import HTTPException
from app.data.atomic import atomic_write,commit
from app.domain.errors import DomainError
from app.agent import dynamic_protocol as protocol,registry
from app.agent import analysis_workflow as facts
from app.agent import read_presentation
from app.agent import runtime_attempts as attempts
from app.agent import planning_assumptions,execution_graph as graph,checkpoints


def enabled():return os.getenv('FINAGENT_AGENT_ENGINE','dynamic').lower()!='legacy'
def get(task):return json.loads(task.context_json).get('dynamic_runtime')
def save(task,r):
    ctx=json.loads(task.context_json)
    if graph.typed(r):r['plan_version']=task.plan_version
    from app.agent.trace import event
    for n in r.get('nodes',[]):
        fingerprint=[n['status'],list(n.get('result_refs',[])),bool(n.get('invalidated')),n.get('error_code')]
        if n.get('_trace_state')!=list(fingerprint):
            event(r,task,'node_state',n);n['_trace_state']=list(fingerprint)
    checkpoints.checkpoint(r,task,'persist')
    graph.validate(task,r,ctx.get('dynamic_runtime'));graph.refresh(r)
    ctx['dynamic_runtime']=r;task.context_json=json.dumps(ctx,ensure_ascii=False)


def current_evidence(r):
    stale=set(r.get('stale_evidence',[]))
    cancelled={g['id'] for g in r.get('goals',[]) if g.get('user_cancelled')}
    for n in r.get('nodes',[]):
        if n.get('goal_ids') and set(n['goal_ids'])<=cancelled and not n.get('receipt_refs'):
            stale.update(n.get('result_refs',[]))
    return dict(r,evidence=[e for e in r.get('evidence',[]) if e['source_ref'] not in stale and e['tool'] not in {'declare_goals','revise_goals','manage_task'}],
        facts=[f for f in r.get('facts',[]) if f['source_ref'] not in stale])


def invalidate_snapshot(r):
    r['stale_evidence']=list(dict.fromkeys(r.get('stale_evidence',[])+[
        e['source_ref'] for e in r['evidence'] if e['tool']!='search_knowledge']))


def validate_goal_claim(text,r):
    from app.agent.requirements import yuan_minor
    for claim in re.finditer(r'([一二两三四五六七八九十\d]+)张(?:固定单|操作单|确认单)[^。；\n]{0,20}待确认',text):
        if re.search('已完成|已执行|已取消|其中',claim[0]):continue
        count=yuan_minor(claim[1])
        actual={tuple(sorted(n['resource_ref'].items())) for n in r.get('nodes',[]) if n.get('resource_ref') and n['status']=='awaiting_confirmation' and not n.get('invalidated')}
        if count is not None and count//100>len(actual):
            raise DomainError('OPERATION_NOT_PREPARED','不能把尚未准备的目标说成已生成待确认单；请逐项区分待准备和已准备。',422)
    from app.agent.cancellation_language import claims
    cancellations=claims(text)
    if not graph.typed(r) or not cancellations:return
    active=next((n for n in reversed(r['nodes']) if n['name']=='respond' and n['status'] in {'pending','running'}),{})
    selected=set(active.get('goal_ids') or r.get('turn_goal_ids') or [g['id'] for g in r['goals']])
    goal_cancelled=any(g['id'] in selected and g.get('user_cancelled') for g in r['goals'])
    business_receipt=any(set(n.get('goal_ids',[]))&selected and n.get('receipt_refs') and n['name'] in {'prepare_birthday_cancel','prepare_birthday_cancel_item','prepare_cancel_bank_mandate','prepare_subscription_revoke','prepare_merchant_contract_cancel'} for n in r['nodes'])
    actual=any(set(n.get('goal_ids',[]))&selected and (n.get('outcome')=='cancelled' or n['status']=='cancelled' or n.get('receipt_refs') and n['name'] in {'prepare_birthday_cancel','prepare_birthday_cancel_item','prepare_cancel_bank_mandate','prepare_subscription_revoke','prepare_merchant_contract_cancel'}) for n in r['nodes'])
    for clause in cancellations:
        known=False
        for e in current_evidence(r)['evidence']:
            if e.get('error') or e['tool'] not in {'read_subscriptions','identify_subscriptions'}:continue
            data=e.get('raw_data') or e.get('data') or {};data=data.get('confirmed',data)
            for contract in data.get('contracts',[]):
                from app.agent.servicing_flow import normalized
                name=normalized(contract.get('service_name'))
                if name and name in normalized(clause) and str(contract.get('auto_renew_enabled')).lower() in {'false','0'}:known=True
        business=bool(re.search('订阅|续费|续订|会员|合同|代扣',clause)) and not re.search('任务|目标|操作单',clause)
        supported=business_receipt or known if business else actual or goal_cancelled
        if not supported:
            raise DomainError('GOAL_CONTROL_REQUIRED','本目标没有取消回执，不能说业务已经取消。请区分历史合同状态、尚未执行与已完成；不要为修正回答而取消用户目标。',409)


# Task control is grounded in "stop word + action verb" rather than whole
# sentences, so ordinary phrasings like "先别转了" / "先不要操作" also work.
_CONTROL_STOP=r'(?:先别|先不要|先不|暂别|暂不|暂时不|别|不要|不用|停止|停一下|暂停)'
_CONTROL_ACT=r'(?:转|转账|办|办理|做|操作|付款|扣款|扣钱|推进|动|执行|继续)'
_CONTROL_POSITIVE={
    'cancel':r'停止|取消|终止|不办理|不转了|算了|不用了|都不办|全部取消|别办了|不弄了',
    'pause':_CONTROL_STOP+r'[^。；\n]{0,6}'+_CONTROL_ACT+r'|暂停|等一下|等等|稍等|先缓缓|缓一缓|先放着|先停',
    'continue':r'继续|恢复|接着办|接着来|接着做|往下办',
    'handoff':r'转人工|找人工|人工接管|找客服'}
_CONTROL_NEGATED={name:r'(?:不要|别|不想)\s*(?:(?:帮|替|为)我|再|自动)?\s*(?:'+verbs+')'
    for name,verbs in {'cancel':'停止|取消|终止','pause':'暂停',
        'continue':'继续|恢复','handoff':'转人工|找人工|人工接管'}.items()}
_CONTROL_NON_INSTRUCTION=r'如何|怎么|假设|如果|为什么|是不是'


def control_grounded(action,quote,request):
    """True only when this turn's own words clearly issue the control action."""
    if action not in _CONTROL_POSITIVE:return False
    if not quote or quote not in request:return False
    if not re.search(_CONTROL_POSITIVE[action],quote):return False
    return not re.search(_CONTROL_NEGATED[action]+'|'+_CONTROL_NON_INSTRUCTION,quote)


def cancelled_goal_reply(text,r):
    """Render control evidence without relaxing the no-data financial guard."""
    from app.agent.cancellation_language import claims
    if not graph.typed(r) or not claims(text):return None
    active=next((n for n in reversed(r['nodes']) if n['name']=='respond' and n['status'] in {'pending','running'}),{})
    selected=set(active.get('goal_ids') or r.get('turn_goal_ids') or [])
    goals={g['id']:g for g in r['goals']}
    if selected and all(goals.get(gid,{}).get('user_cancelled') for gid in selected):
        return '已取消该目标尚未执行的部分。已发生的交易不受此次取消影响，请以固定单和实际回执为准。'
    return None


_PENDING_ORDER_FIELDS=('proposal','wealth_order','card_request','subscription_request',
    'birthday_request','schedule','collection','service_request')
_ORDER_LABEL={'proposal':'转账或卡片操作单','wealth_order':'理财申购或赎回单','card_request':'卡片办理单',
    'subscription_request':'订阅提醒或解除代扣单','birthday_request':'生日安排单','schedule':'预约转账单',
    'collection':'AA收款单','service_request':'业务办理单'}


SELF_ASSET_TOOLS={'list_accounts','list_cards','read_financial_profile','read_financial_history',
    'read_credit_cards','financial_planning_basis','read_wealth','read_card_management',
    'read_subscriptions','identify_subscriptions','read_birthdays','list_schedules','list_collections'}


PERIOD_TOOLS={'summarize_spending','generate_statement','analyze_finances','list_transactions'}
PERIOD_WORDS={'上个月':'last_month','上月':'last_month','这个月':'this_month','本月':'this_month',
    '昨天':'yesterday','今天':'today'}
_PERIOD_LABEL={'last_month':'上个月','this_month':'这个月','yesterday':'昨天','today':'今天'}


def demo_day(db):
    """The demo calendar day every relative period must resolve against."""
    from app.data.models import DemoSetting
    from app.domain.clock import parse_day
    setting=db.get(DemoSetting,1)
    if setting is None or not setting.demo_date:return None
    return parse_day(setting.demo_date)


def expected_period(request,demo_date):
    """(label, start, end) the user's own period words require, on the demo clock."""
    from app.domain.clock import (DateRange,current_month_range,previous_full_month_range)
    if not request or demo_date is None:return None
    words={word for word in re.findall(r'上个月|上月|这个月|本月|昨天|今天',request)}
    # Two different period words in one turn is a comparison we must not police.
    if len(words)!=1:return None
    key=PERIOD_WORDS[next(iter(words))]
    if key=='this_month':window=current_month_range(demo_date)
    elif key=='last_month':window=previous_full_month_range(demo_date)
    elif key=='today':window=DateRange(demo_date,demo_date+timedelta(days=1))
    else:window=DateRange(demo_date-timedelta(days=1),demo_date)
    return _PERIOD_LABEL[key],window.start.isoformat(),(window.end_exclusive-timedelta(days=1)).isoformat()


def period_mismatch_message(name,args,request,demo_date):
    """The demo clock decides "this month"; real-time dates silently answer about nothing."""
    if name not in PERIOD_TOOLS:return None
    wanted=expected_period(request,demo_date)
    if not wanted:return None
    label,start,end=wanted
    supplied=(args.get('start'),args.get('end'))
    if supplied==(None,None):
        # Omitting dates is only safe when the tool default already equals the range.
        return None if label=='这个月' else (
            f'用户说的是“{label}”，按演示日期应查询 {start} 至 {end}；'
            '省略区间会让工具默认查本月，所以请显式按这个区间查询。')
    if (str(supplied[0]),str(supplied[1]))!=(start,end):
        return (f'用户说的是“{label}”，按演示日期 {demo_date.isoformat()} 该区间是 {start} 至 {end}，'
            f'不是 {supplied[0]} 至 {supplied[1]}。请按演示日期修正区间；'
            '不要把真实当天日期当作账单日期。')
    return None


_NEGATED_STOP=r'(?:不要|别|不想|不用|不需要|暂不|先不)\s*(?:帮我|替我|给我|再|自动)?\s*(?:取消|退订|关闭|停止|解除|挂失)'
_ACTION_VERB=r'(?:取消|退订|关闭|停止|解除|挂失)'


def supersede_stale_prohibitions(prohibitions,request):
    """Drop an earlier "先别…" once this turn explicitly asks for the same action.

    Prohibitions are meant to constrain the turn that stated them. Keeping them alive
    forever meant one "先别取消" made every later cancellation impossible. A prohibition
    is only dropped when the user's own current words request the action and no longer
    repeat the refusal, so nothing is loosened beyond what the user just asked for.
    """
    kept=[]
    for value in prohibitions or []:
        if isinstance(value,str) and re.search(_NEGATED_STOP,value) \
                and re.search(_ACTION_VERB,request or '') and not re.search(_NEGATED_STOP,request or ''):
            continue
        kept.append(value)
    return kept


def foreign_subject_message(db,session,request):
    """Refuse to answer a self-asset read that was actually asked about someone else.

    The tools only ever resolve the session owner, so without this guard the answer
    silently becomes the user's own data presented as the other person's.
    """
    if not request:return None
    from sqlalchemy import select
    from app.data.models import Recipient,User
    owner=db.get(User,session.owner_id)
    own_name=getattr(owner,'display_name',None) if owner else None
    if not own_name:return None
    payees={name for name in db.scalars(select(Recipient.name).where(Recipient.owner_id==session.owner_id)) if name}
    for user in db.scalars(select(User)):
        name=getattr(user,'display_name',None)
        if not name or user.id==session.owner_id or name==own_name or name in payees:continue
        if name in request:
            return (f'只能查询你本人（{own_name}）的账户与记录；{name} 不是本会话用户，'
                '系统不能查询他人账户，也没有返回任何其他人的数据。')
    return None


def pending_orders(state):
    """Fixed orders this turn actually produced and that still await a human click."""
    return [(key,state[key]) for key in _PENDING_ORDER_FIELDS
        if isinstance(state.get(key),dict) and state[key].get('status')=='pending_confirmation']


def unfinished_operations(r):
    return [g for g in r.get('goals',[]) if g.get('kind')=='operation'
        and g.get('status') not in {'completed','cancelled','failed'}]


def closing_text(state,r,visible):
    """Speak honestly when the loop stops: prepared, still missing, or merely answered."""
    details=''
    if visible:
        from app.agent.dialogue import evidence_reply
        from app.agent.dialogue_schema import DialogueTurn
        details='独立查询已完成：\n'+evidence_reply(SimpleNamespace(reply=None,intent='reply',
            dialogue=DialogueTurn(behavior='insert_query')),visible)
    orders=pending_orders(state)
    if orders:
        labels=list(dict.fromkeys(_ORDER_LABEL[key] for key,_ in orders))
        return ('已准备'+'、'.join(labels)+'，尚未执行。请在页面核对条款后手动确认；'
            '聊天里的“确认”不会生效，也不会自动扣款。'+('\n\n'+details if details else ''))
    if unfinished_operations(r):
        # No fixed form exists yet: never let a green read read as "the business is
        # being handled", and always ask for the one object or term that is missing.
        return ((details+'\n\n' if details else '')
            +'这次还没有生成办理固定单。请补充要办理的具体对象或条款（例如卡片尾号、收款人、金额、日期），'
            '我再准备固定单；在你页面确认之前不会有任何扣款或状态变更。')
    return details or None


def render_response(response,r):
    """Only the server substitutes numerical facts into natural model prose."""
    from app.agent.planning_reply import validate_assertions
    validate_goal_claim(response.text,r)
    validate_assertions(response.text,r)
    response=response.model_copy(update={'text':response.text.replace('\\n','\n')})
    selected={f['id']:f for f in r['facts']};used=[]
    def replace(match):
        f=selected.get(match[1])
        if not f:raise DomainError('ANALYSIS_REFERENCE','回答引用了未返回或已过期的事实，请重新查询',422)
        suffix=response.text[match.end():]
        prefix=response.text[max(0,match.start()-16):match.start()]
        if f['unit']=='yuan' and re.search(r'(?:日期|截止|到期|还款日|下次扣费日|权益至)\s*[:：]?\s*$',prefix):
            raise DomainError('ANALYSIS_UNIT','金额事实不能当作合同日期；请引用工具返回的完整日期。',422)
        if f['unit']=='yuan' and re.match(r'\s*(?:笔|次|个月|天|人|%)',suffix) or f['unit']=='number' and re.match(r'\s*(?:万元|元)',suffix):
            raise DomainError('ANALYSIS_UNIT','引用事实的单位与文字不符；笔数须引用笔数事实，金额须引用金额事实',422)
        # A count fact dropped in without its unit renders as a bare number
        # ("、32，"); require the model to write the unit next to the placeholder.
        if f['unit']!='yuan' and re.search(r'笔|次|份|项|条',f.get('label') or '') \
                and not re.match(r'\s*(?:笔|次|份|项|条|%|个月|天|人)',suffix):
            raise DomainError('ANALYSIS_UNIT','引用笔数事实时必须紧跟单位，例如“{{'+match[1]+'}} 笔”；否则会出现没有单位的裸数字',422)
        used.append(f);value=facts.decimal(f['value'])
        return (f'{value:,.2f}' if re.match(r'\s*元',suffix) else f'{value:,.2f} 元') if f['unit']=='yuan' else format(value.normalize(),'f')
    text=re.sub(r'\{\{(f[0-9]+)\}\}',replace,response.text)
    prose=re.sub(r'\{\{f[0-9]+\}\}','该核算值',response.text)
    money={facts.decimal(f['value']) for f in r['facts'] if f['unit']=='yuan'};verified_literals=[]
    def checked_money(match):
        value=facts.decimal(match[1].replace(',',''))*(10000 if match[2]=='万元' else 1)
        if value in money:verified_literals.append(value);return '该核算金额'
        return match[0]
    prose=re.sub(r'(-?[0-9][0-9,]*(?:\.[0-9]+)?)\s*(万元|元)',checked_money,prose)
    months={facts.decimal(f['value']) for f in r['facts'] if f['unit']=='number' and ('月' in f['label'] or f['key']=='complete_months')}
    prose=re.sub(r'([0-9]+(?:\.[0-9]+)?)\s*个?(?:完整)?月',lambda m:'已核对期限' if facts.decimal(m[1]) in months else m[0],prose)
    # The user's own duration words ("三个月", "一个月") are ordinary prose, not a
    # number claim; neutralise them only when the span length is a verified count.
    from app.agent.requirements import yuan_minor
    def duration_word(match):
        minor=yuan_minor(match[1]);value=Decimal(minor)/100 if minor is not None else None
        return '已核对期限' if value in months else match[0]
    prose=re.sub(r'([零〇一二两三四五六七八九十]+)\s*个(?:完整)?月',duration_word,prose)
    # A historical monthly average is intrinsically a one-month quantity.
    if any(f['key']=='monthly_expense_mean' for f in r['facts']):prose=prose.replace('一个月','每月')
    def verified_date(value):
        nonlocal prose
        if isinstance(value,str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):
            y,m,d=map(int,value.split('-'))
            # Most specific first: a loose "9-01" pattern would otherwise eat the "9"
            # of "09-01" and leave a stray "0" behind. Whitespace is tolerated
            # because models write "2026 年 9 月" as readily as "2026年9月".
            for pattern in (rf'{y}\s*-\s*{m:02d}\s*-\s*{d:02d}',rf'{y}\s*-\s*{m}\s*-\s*{d}',
                    rf'{y}\s*年\s*{m:02d}\s*月\s*{d:02d}\s*日',rf'{y}\s*年\s*{m}\s*月\s*{d}\s*日',
                    rf'{m:02d}\s*月\s*{d:02d}\s*日',rf'{m}\s*月\s*{d}\s*日',
                    rf'{m:02d}\s*-\s*{d:02d}',rf'{m}\s*-\s*{d}',
                    rf'{y}\s*年\s*{m:02d}\s*月',rf'{y}\s*年\s*{m}\s*月'):
                prose=re.sub(pattern,'已核对日期',prose)
    def mask_seen(mask,label):
        """Neutralise a queried card/account tail, tolerating "尾号 3355" spacing."""
        nonlocal prose
        if isinstance(mask,str) and re.fullmatch(r'\*\*\d{4}',mask):
            prose=re.sub(r'尾号\s*'+re.escape(mask[2:]),label,prose)
            prose=prose.replace(mask,label)
    for e in r['evidence']:
        data=e.get('data') or {}
        # A window the coverage map does not touch at all produces arithmetic zeros,
        # not "you spent nothing": never let that render as a period total.
        coverage=data.get('coverage') if isinstance(data,dict) else None
        if isinstance(coverage,dict) and coverage.get('status')=='incomplete' and not coverage.get('segments') \
                and (data.get('totals') or {}).get('is_complete') is False:
            raise DomainError('ANALYSIS_INCOMPLETE',
                '这个区间没有任何已覆盖的流水记录，不能把 0 当成该区间的真实总额；'
                '请说明该区间未被覆盖，并改用有覆盖的区间或请用户确认口径。',422)
        if e['tool'] in {'list_accounts','list_cards'} and isinstance(data,list):
            for record in data:
                for key in ('account_mask','card_mask'):
                    mask=record.get(key) or ''
                    if re.fullmatch(r'\*\*\d{4}',mask):mask_seen(mask,'已查询尾号')
        if isinstance(data,dict):
            for key in ('period_start','period_end','snapshot_as_of','as_of'):
                value=data.get(key)
                verified_date(value)
            if e['tool'] in {'read_subscriptions','identify_subscriptions'}:
                contract_data=data.get('confirmed',data)
                verified_date(contract_data.get('as_of'))
                for value in contract_data.get('period',{}).values():verified_date(value)
                for contract in contract_data.get('contracts',[]):
                    for key in ('next_billing_date','access_end_date','cancel_requested_on','cancellation_effective_on'):
                        verified_date(contract.get(key))
            if e['tool']=='financial_planning_basis':
                due=data.get('upcoming_obligations',{})
                verified_date(due.get('as_of'));verified_date(due.get('through'))
                for item in due.get('items',[]):verified_date(item.get('due_date'))
            if e['tool']=='read_credit_cards':
                # Verify only this query's card identifiers and calendar metadata;
                # these never establish evidence for amounts or other dates.
                for card in data.get('credit_cards',[]):
                    mask=card.get('card_mask') or ''
                    if re.fullmatch(r'\*\*\d{4}',mask):
                        mask_seen(mask,'已查询卡片')
                    for key,label in [('statement_day','账单日'),('due_day','还款日')]:
                        day=str(card.get(key) or '')
                        if day.isdigit() and 1<=int(day)<=31:
                            prose=re.sub(label+r'(?:为|是|：|:)?\s*(?:每月)?'+str(int(day))+r'(?:日|号)','已查询日期',prose)
                    statement=card.get('latest_statement') or {}
                    for key in ('statement_date','due_date'):
                        value=statement.get(key)
                        verified_date(value)
            if e['tool'] in {'prepare_card_replacement','prepare_credit_limit_change','prepare_merchant_contract_cancel','read_service_requests'}:
                records=data.get('items',[data])
                for record in records:
                    identifiers=[(r'(?:回执号|回执编号|操作单号|业务编号)',record.get('id'))]
                    for part in (record.get('summary',{}),record.get('receipt') or {}):
                        identifiers.append((r'(?:合同编号|订阅编号)',part.get('contract_id')))
                    for label,value in identifiers:
                        if isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}',value):
                            prose=re.sub(label+r'\s*[:：]?\s*'+re.escape(value)+r'(?![A-Za-z0-9_-])','已核对业务编号',prose)
                    for part in (record.get('summary',{}),record.get('receipt') or {}):
                        for key in ('card_mask','old_card_mask','new_card_mask'):
                            mask=part.get(key) or ''
                            if re.fullmatch(r'\*\*\d{4}',mask):mask_seen(mask,'已核对卡片')
                        date=part.get('access_end_date')
                        verified_date(date)
    user_text='\n'.join(r.get('user_messages',[]))
    prose=re.sub(r'[0-9]+\s*(?:天|人)',lambda m:'用户指定条件' if m[0] in user_text else m[0],prose)
    counts={facts.decimal(f['value']) for f in r['facts'] if f['unit']!='yuan' and (
        f['key'].endswith('_count') or f['key'].split('.')[-1]=='count'
        or re.search(r'笔数|笔|次数|条数|件数|份数|项数|数量',f.get('label') or ''))}
    prose=re.sub(r'([0-9]+)\s*(?:笔|次|份|项|条)',lambda m:'已核对数量' if facts.decimal(m[1]) in counts else m[0],prose)
    queried_dates=[v for e in r['evidence'] for v in e.get('query',{}).values() if isinstance(v,str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}',v)]
    for day in queried_dates:
        y,m,d=map(int,day.split('-'))
        for form in (day,f'{y}年{m}月{d}日',f'{y}年{m}月',f'{m}月'):
            prose=prose.replace(form,'已查询月份')
    prose=re.sub(r'(?m)^\s*(?:\*\*)?[0-9]{1,2}[.、)]\s*(?:\*\*)?\s*','• ',prose)
    prose=re.sub(r'(?:还|尚|暂)?(?:不能|无法|不足以)(?:确定|判断|说明|断言|认定)?[^。；\n]{0,24}(?:负担得起|预算充足|还款没有压力)',
        '尚待补充成本信息',prose)
    refs=list(dict.fromkeys(response.evidence_refs+[f['source_ref'] for f in used]))
    if any(f.get('data_complete') is False for f in r['facts']):
        for sentence in re.split('[。；，,\n]',prose):
            trend=re.search(r'(?:支出|消费|花费|金额|费用|本期|本月|这月|该月|整体).{0,12}(?:下降|降低|更低|低于|减少|增长|高于|增多)|(?:同比|环比|较上期|相比上期).{0,12}(?:下降|降低|减少|增长|增多)',sentence)
            guarded=re.search('不能|无法|不代表|已记录|已导入|样本|不(?:会|能)?.{0,8}(?:判断|比较|计算|下结论)',sentence) or re.match(r'\s*(?:建议|可考虑|可以通过|可通过|可以尝试|可尝试|如果|假设)',sentence)
            if trend and not guarded:
                raise DomainError('ANALYSIS_INCOMPLETE','月份数据有缺口，只能列已记录金额，不能判断实际总额或真实增减方向；未知区间可能有消费或退款。',422)
    facts.render(facts.Answer(answer_template=prose,evidence_refs=refs),r)
    return text,bool(used or verified_literals)


def public(task):
    r=get(task)
    if not r:return None
    return {'id':r['id'],'objective':r['request'],'status':r['status'],'read_only':not any(n['name'] in protocol.OPERATIONS for n in r['nodes']),
        'nodes':[{k:n[k] for k in ('id','title','name','depends_on','status','recovered_by') if k in n} for n in r['nodes']]}


def config():
    def bounded(key,default,maximum):
        try:return max(1,min(maximum,int(os.getenv(key,str(default)))))
        except ValueError:return default
    return {'calls':bounded('AGENT_MAX_MODEL_CALLS',8,12),'seconds':bounded('AGENT_TIMEOUT_SECONDS',100,180),
            'tools':bounded('AGENT_MAX_TOOL_CALLS',16,32)}


def load(db,session,tid):
    from app.agent.service import _task
    from app.domain.operations import _session,_now
    session,_=_session(db,session.id,_now());task=_task(db,session,tid)
    return session,task


def check(db,session,tid,r,request_id,revision):
    from app.agent.control import assert_request
    session,task=load(db,session,tid);assert_request(db,tid,request_id)
    current=get(task)
    if not current or current['id']!=r['id'] or task.updated_at_utc!=revision or graph.typed(r) and r['plan_version']!=task.plan_version:
        raise DomainError('STALE_PLAN_RESPONSE','问题或操作状态已变化，迟到结果不再应用',409)
    return task


def context(db,task,r):
    from app.agent.dialogue import context_for
    from app.agent.language import payload_for
    from app.agent.service import view
    from app.agent.model_budget import ACTIVE
    current=view(db,task);base=payload_for(db,task,current);ctx=context_for(db,task,current)
    for key in ('allowed_read_tools','messages','earlier_summary'):ctx.pop(key,None)
    progress=graph.public(task)
    if progress:
        progress['nodes']=[{k:n.get(k) for k in ('id','name','goal_ids','depends_on','status','error_code','receipt_refs','resource_ref','invalidated','verification_required','next_step')} for n in progress['nodes'] if not n.get('invalidated')][-24:]
    return {k:base[k] for k in ('demo_date','current_time','birthday_business_now_utc') if k in base}|{
        'current_time_utc':datetime.now(timezone.utc).isoformat(),
        'execution_graph':progress,'dialogue_context':ctx,'tool_budget':config()['tools']-(len(r['nodes'])-r.get('turn_start_node_count',0)),
        'model_budget':{'remaining_calls':(ACTIVE.get() or {}).get('remaining'),
            'guidance':'证据足够就综合回答；余量较少时不再扩展备选情景或重复查询，优先完成当前目标。'},
        'authorization_context':{'subscription_read_completed':any(e['tool'] in {'read_subscriptions','identify_subscriptions'} and not e.get('error') for e in current_evidence(r)['evidence'])},
        'facts':facts.focused_facts(current_evidence(r)['facts']),'completed_operations':r.get('completed_operations',[]),
        'stale_evidence':r.get('stale_evidence',[]),'checkpoint':r.get('checkpoint'),
        'valid_prior_nodes':[{k:n.get(k) for k in ('id','name','arguments','goal_ids','result_refs')} for n in r['nodes'] if n['status']=='succeeded' and not n.get('invalidated') and n.get('policy',{}).get('effect') in {'read','calculate'}][-12:],
        'analysis_assumption_scope':planning_assumptions.scope(r.get('user_messages',[])),
        'advice_only':planning_assumptions.advice_only(r['request'])}


def observation(r,node,data,error=None,*,record=True):
    from app.agent.dialogue import envelope
    source='call:'+r['id']+':'+node['id']
    e=envelope(node['name'],node['arguments'],data,source,error)
    if record:
        r['evidence'].append(dict(e,raw_data=data));node['status']='failed' if error else 'succeeded'
        if not error:attempts.recovered(r,node)
        items=[data]+data.get('supporting_facts',[]) if node['name']=='calculate' and not error else facts.facts_from(node,data) if not error else []
        if not error and node['name'] in {'summarize_spending','analyze_finances','generate_statement'}:items+=read_presentation.count_facts(data)
        if node['name'] in {'summarize_spending','generate_statement'} and isinstance(data,dict):
            comparison=data.get('comparison') or {}
            if comparison.get('percent_change') is not None:items.append({'key':'comparison.percent_change',
                'label':'净消费变化百分数','value':comparison['percent_change'],'unit':'number'})
            for f in items:
                section=data.get('comparison' if f['key'].startswith('comparison.') else 'current',{}) or {}
                if (section.get('totals') or {}).get('is_complete') is False:f['data_complete']=False
        facts.add_facts(r,node,items,source)
        if node['name']=='calculate' and not error:
            facts.add_facts(r,node,[dict(key='parameter_'+str(i),label=a['label'],value=a['value'],unit=a['unit']) for i,a in enumerate(data.get('assumptions',[]))],source)
    result={'call_id':node['id'],'status':'rejected' if error else 'completed','source_ref':source,
        'facts':[f for f in r['facts'] if f['node']==node['id']],**facts.compose_evidence(e)}
    if r.get('stale_evidence'):
        result['valid_evidence_refs']=[e['source_ref'] for e in current_evidence(r)['evidence'] if not e.get('error')]
        result['stale_evidence_refs']=r['stale_evidence']
    if error:
        result['error']=error
        from app.agent.failure_policy import classify
        node['failure_policy']=classify(error['code'],registry.execution_policy(node['name'],node['arguments'])['effect'])
        result['recovery']={'identical_attempts':len(attempts.prior_rejections(r,node))+1,
            **node['failure_policy'],
            'instruction':'按错误字段修正；相同失败参数最多尝试两次。不得省略用户条件、切换工具绕过权限，无法继续时说明缺项或交由用户处理。',
            'active_evidence_refs':[e['source_ref'] for e in current_evidence(r)['evidence'] if not e.get('error')]}
    if not error and node['name']=='read_tool_catalog':result['data']=data
    if not error and node['name']=='summarize_spending':
        result['data']={k:data[k] for k in ('category','merchant','expense_basis') if k in data}
        for key in ('current','comparison'):
            if isinstance(data.get(key),dict):result['data'][key]={k:data[key][k] for k in ('period','coverage','totals','delta_minor','percent_change','percent_change_reason') if k in data[key]}
    # API observations are bounded; the complete domain result stays local.
    if len(json.dumps(result,ensure_ascii=False))>(18000 if node['name']=='financial_planning_basis' else 10000):result.pop('data',None);result['excerpt_notice']='完整结果已保存在服务端；金额使用facts，明细可按工具分页。'
    graph.observed(r,node,result)
    return result


def append_result(r,node,result):
    # Chat Completions cannot insert synthetic mid-conversation tool calls.
    # Across a new user turn, expose the actual receipt as untrusted checkpoint
    # data; current facts/ownership/domain state stay authoritative.
    matched=any(any(c.get('id')==node['model_call_id'] for c in m.get('tool_calls',[])) for m in r['messages'])
    if matched:r['messages'].append({'role':'tool','tool_call_id':node['model_call_id'],'content':json.dumps(result,ensure_ascii=False)})
    else:r['messages'].append({'role':'user','content':json.dumps({'kind':'server_checkpoint_data_not_user_instruction','observation':result},ensure_ascii=False)})


def record_step(db,task,node,result):
    from app.agent.service import _record_step
    _record_step(db,task,node['name'],node['arguments'],result,max_steps=96)


def prepared_state(task,state):
    pending=[op for op in state.get('authorization_operations',[]) if op['status']=='pending_confirmation']
    if pending:
        op=pending[-1]
        return {'kind':'authorization','resource_kind':op['resource_kind'],'resource_id':op['resource_id'],'action_id':op['action_id'],'authorization_path':op['authorization_path']}
    ctx=json.loads(task.context_json)
    if ctx.get('pending_subscription_query'):return {'kind':'subscription_precondition','pending_id':ctx['pending_subscription_query']['id']}
    return {'kind':'input'}


def prepare(db,session,task,r,node):
    from app.agent.dialogue import apply
    from app.agent.language import Understanding
    from app.agent.dialogue_schema import DialogueTurn,Goal
    from app.agent.service import view
    value=dict(node['arguments']);opctx=value.pop('operation_context',{})
    from app.agent.planning_assumptions import advice_only
    if advice_only(r['request']):
        raise DomainError('ANALYSIS_ONLY','用户当前仅询问建议或明确暂不办理；只能查询、计算和解释，不能准备资金操作单。',422)
    quote=opctx.pop('instruction_quote',None) or r['request']
    original_quote=quote
    if node['name']=='prepare_merchant_contract_cancel':
        from app.agent.subscription_selection import continuation_quote
        quote=continuation_quote(r,node,quote)
    inherited=node['name']=='prepare_merchant_contract_cancel' and quote!=original_quote
    if quote not in r['request'] and not inherited:raise DomainError('UNGROUNDED_OPERATION','办理原文没有本轮用户消息支持，不能重放历史办理指令',422)
    canonical,value=protocol.operation_values(node['name'],value)
    signature=canonical+json.dumps({k:v for k,v in value.items() if v is not None},sort_keys=True,ensure_ascii=False)
    if signature in r.get('prepared_signatures',{}):
        raise DomainError('OPERATION_ALREADY_PREPARED','同一笔操作已经准备或执行，请查看原单和回执，不重复创建',409)
    if len(r.get('prepared_signatures',{}))>=3:raise DomainError('OPERATION_LIMIT','当前请求已达到办理数量限制，请核对已有单据后继续',409)
    current=view(db,task)
    if any(op['status']=='pending_confirmation' for op in current.get('authorization_operations',[])) and opctx.get('behavior','new_task') not in {'modify','supplement','continue'}:
        raise DomainError('PENDING_OPERATION','请先处理当前操作单，不能用另一项办理悄悄替换它',409)
    # A distinct subsequent instruction may follow an already confirmed transfer.
    # Completed resources stay in ledger/steps and authorization_receipts.
    if current.get('proposal',{} ) and current['proposal']['status']=='executed':
        if r.get('completed_operations') and quote==r.get('last_operation_quote'):
            raise DomainError('EXPLICIT_NEXT_OPERATION_REQUIRED','原操作已完成；下一笔需明确不同的用户指令，未重复准备',409)
        if canonical in {'prepare_transfer','prepare_card_action'}:
            ctx=json.loads(task.context_json);ctx['last_transfer_proposal_id']=task.proposal_id;task.proposal_id=None
            task.context_json=json.dumps(ctx,ensure_ascii=False);current=view(db,task)
    from app.agent.servicing_flow import TOOLS as SERVICE_TOOLS,prepare as prepare_service
    if node['name'] in SERVICE_TOOLS:
        from app.agent.servicing_flow import validate_context
        if isinstance(opctx,dict):
            # A "先别取消" said earlier must not block the user from asking again now.
            opctx['prohibitions']=supersede_stale_prohibitions(opctx.get('prohibitions'),r['request'])
        validate_context(node['name'],opctx,subject=value.get('merchant'),amount_yuan=value.get('amount_yuan'))
        before_version=checkpoints.version(db,task)
        if r.get('evidence_version')!=before_version:invalidate_snapshot(r)
        result=prepare_service(db,session,task,node['name'],value,quote,modify=opctx.get('behavior')=='modify')
        r['evidence_version']=checkpoints.version(db,task)
        if node['name']=='prepare_merchant_contract_cancel' and result.get('service_request'):
            record=result['service_request'];merchant=record['summary']['merchant']
            for goal in r['goals']:
                if goal['id'] in node.get('goal_ids',[]):
                    suffix='（合同：'+merchant[:100]+'）'
                    goal['text']=goal['quote'][:300-len(suffix)]+suffix
        r['last_operation_quote']=quote
        r.setdefault('prepared_signatures',{})[signature]={'node':node['id'],'quote':quote}
        wait=prepared_state(task,result);wait.update(node_id=node['id'],plan_version=task.plan_version,
            dataset_version=task.dataset_version,expires_at=(datetime.now(timezone.utc)+timedelta(minutes=15)).isoformat())
        return wait,result
    intent=protocol.OPERATIONS[node['name']][0]
    readonly=intent in {'wealth_compare','wealth_assessment','budget'}
    turn=DialogueTurn(behavior=opctx.pop('behavior','new_task'),goals=[Goal(text=quote,kind='read' if readonly else 'write')],**opctx)
    decision=Understanding(intent=intent,dialogue=turn,follow_up=turn.behavior!='new_task',**value)
    result=apply(db,session,task,quote,current,decision,[],validated_new_operation=True)
    if readonly and result['status']=='completed' and not result.get('prerequisites'):
        return None,{'status':'completed','messages':[m['content'] for m in result['messages'][-2:] if m['role']=='assistant'],'steps':result.get('steps',[])[-1:]}
    last=next((m['content'] for m in reversed(result['messages']) if m['role']=='assistant'),'')
    if re.search('未更改|没有准备|未准备|不能办理|不支持|风险不匹配',last) and not any(
        result.get(k) for k in ('transfer_details','birthday_details','prerequisites','next_actions','authorization_operations')):
        raise DomainError('OPERATION_NOT_PREPARED',last+'；请返回当前问题所需的查询与分析，不等待不存在的确认单。',422)
    r['last_operation_quote']=quote
    # The signature also protects a missing-information checkpoint from replays.
    r.setdefault('prepared_signatures',{})[signature]={'node':node['id'],'quote':quote}
    wait=prepared_state(task,result);wait.update(node_id=node['id'],plan_version=task.plan_version,
        dataset_version=task.dataset_version,expires_at=(datetime.now(timezone.utc)+timedelta(minutes=15)).isoformat())
    return wait,result


def calculate(r,node):
    if set(node.get('goal_ids',[])) & set(r.get('revision_pending',[])):
        raise DomainError('GOAL_REVISION_REQUIRED','原假设已更正，须先成功更新该目标与约束，再重新计算；不能沿用旧预算。',409)
    r=current_evidence(r)
    args=node['arguments']
    c=facts.Calculation.model_validate(args['calculation']) if 'calculation' in args else facts.Calculation.model_validate_json(args['calculation_json'])
    allowed={'sum','subtract','multiply','divide','min','max','mean','funding_check','reserve','runway_check','savings_projection','income_shock','goal_budget','emergency_check','savings_capacity'}
    allowed.add('obligation_budget')
    if c.operation not in allowed:raise DomainError('INVALID_CALCULATION','未知计算操作',422)
    required=4 if c.operation=='goal_budget' else 3 if c.operation in {'funding_check','income_shock','emergency_check'} else 2 if c.operation in {'subtract','multiply','divide','reserve','runway_check','savings_projection','savings_capacity'} else None
    if required and len(c.inputs)!=required:raise DomainError('INVALID_CALCULATION','计算输入数量不符',422)
    if c.operation=='obligation_budget' and len(c.inputs) not in (4,5):raise DomainError('INVALID_CALCULATION','应付款情景需要四项输入，可另加明确目标成本',422)
    for op in c.inputs:
        if op.node:
            if op.value is not None or not op.key or op.node not in {f['node'] for f in r['facts']}:raise DomainError('ANALYSIS_REFERENCE','只可引用已返回的事实，不能引用尚未完成的调用',422)
            if any(f['node']==op.node and f['key']==op.key and f.get('data_complete') is False for f in r['facts']):
                raise DomainError('ANALYSIS_INCOMPLETE','该区间数据有缺口，不能据此计算完整月差额或降幅；只说明已记录金额与缺失情况。',422)
        elif not op.label or op.value is None or op.unit not in {'number','yuan'}:raise DomainError('INVALID_CALCULATION','计算常量须说明值、单位与用途',422)
        elif not op.quote:
            if op.unit=='number' and not 0<=facts.decimal(op.value)<=120 or facts.decimal(op.value)==0 and re.search('未知|占位|待.*确认',op.label):raise DomainError('INVALID_CALCULATION','情景参数不合理，未知成本不能用零占位',422)
    if c.operation=='goal_budget' and c.inputs[3].value is not None and facts.decimal(c.inputs[3].value)==0 and not c.inputs[3].quote:
        raise DomainError('ANALYSIS_ASSUMPTION','未知目标成本不能用零占位；没有成本时用emergency_check先测生活费缓冲，再询问目标预算。',422)
    if c.operation=='goal_budget' and re.search(r'月度储蓄|月储蓄|储蓄目标|目标储蓄|每月.{0,12}(?:存|储蓄|攒)|每个月.{0,12}(?:存|储蓄|攒)|内部转账|资金调拨',
            (c.inputs[3].label or '')+(c.inputs[3].quote or '')):
        raise DomainError('ANALYSIS_UNIT','储蓄或内部划转不是新增对外费用。月储蓄目标用savings_capacity比较monthly_cashflow_mean和目标金额，不用goal_budget把储蓄再扣成消费。',422)
    if c.operation=='funding_check':
        first,second,cost=c.inputs
        if cost.value is not None and facts.decimal(cost.value)==0 and not cost.quote:
            raise DomainError('ANALYSIS_ASSUMPTION','目标成本未知不能用零占位。可用subtract算预留后的算术余量，再向用户询问目标成本；不能称为已覆盖目标。',422)
        reserve=next((f for f in r['facts'] if f['node']==second.node and f['key']==second.key),None)
        target=next((f for f in r['facts'] if f['node']==cost.node and f['key']==cost.key),None)
        if target and target.get('metric')=='reserve':
            raise DomainError('ANALYSIS_UNIT','不能把另一档生活费预留同时当作独立目标成本，造成两档预留重复扣减。比较应急金档位时分别用subtract计算可用活期与该档reserve的差额。',422)
        if first.key!='available_liquid' or not (reserve and reserve.get('metric')=='reserve' or second.value is not None and re.search('预留|应急',second.label or '')):
            raise DomainError('ANALYSIS_UNIT','funding_check依次需要available_liquid、reserve调用返回的result、目标成本；不能把单月消费冒充多月预留。',422)
    users=r.get('user_messages',[])
    target_index=3 if c.operation=='goal_budget' else 2 if c.operation=='funding_check' else 4 if c.operation=='obligation_budget' and len(c.inputs)==5 else None
    if target_index is not None:
        target=c.inputs[target_index]
        if target.value is not None and not target.quote:
            raise DomainError('ANALYSIS_ASSUMPTION','独立目标成本须来自用户明确预算；未知费用不设演示报价。先测缓冲并追问目标预算。',422)
        target_fact=next((f for f in r['facts'] if f['node']==target.node and f['key']==target.key),None)
        if target_fact and target_fact['key'] in {'available_liquid','monthly_expense_mean','monthly_income_mean','monthly_cashflow_mean','monthly_direct_cash_expense_mean','mixed_net_worth','term_principal','snapshot_investments','snapshot_physical_assets'}:
            raise DomainError('ANALYSIS_UNIT','账户资金、历史收支或资产估值不能冒充独立目标的价格。请取得用户预算或已核验的目标成本。',422)
        if target_fact and target_fact.get('tool')=='calculate' and any(a['unit']=='yuan' and a['source']!='用户明确参数' for a in target_fact.get('assumptions',[])):
            raise DomainError('ANALYSIS_ASSUMPTION','派生目标成本包含未由用户提供的金额，不能借汇总计算编造未知价格。',422)
        if target_fact and target_fact.get('metric') in {'reserve','emergency_buffer','funding_buffer','commitment_buffer'}:
            raise DomainError('ANALYSIS_UNIT','不能把另一个预留档位或现金余量当作独立目标费用重复扣减。',422)
    result=facts.calculate({'title':node['arguments']['title'],'arguments':c.model_dump()},r['facts'],users)
    if c.operation in {'goal_budget','emergency_check','funding_check'}:
        result['known_dues_not_deducted']=any(f['key']=='upcoming_due_known' and facts.decimal(f['value'])>0 for f in r['facts'])
    return result


def finish(db,task,r,response):
    from app.agent.service import _say,view
    from app.agent.dialogue import safe_explanation,evidence_reply
    active=current_evidence(r);inserted=False;selected=set()
    from app.agent.subscription_selection import validate_completion
    responding=next((n for n in reversed(r['nodes']) if n['name']=='respond' and n['status'] in {'pending','running'}),None)
    validate_completion(r,responding,response)
    if response.synthesis:
        from app.agent import planning_reply
        if not any(e['tool']=='financial_planning_basis' and not e.get('error') for e in active['evidence']):
            raise DomainError('ANALYSIS_REFERENCE','针对个人目标的财务分析需要先查询本人财务底稿。',422)
        text,refs,selected=planning_reply.response_text(response.synthesis,active)
        # In this typed answer, provenance comes exclusively from validated
        # active fact IDs. Model-written source strings are redundant claims.
        response=response.model_copy(update={'text':text,'evidence_refs':refs})
    validate_goal_claim(response.text,r)
    if active['evidence']:
        successful=[e for e in active['evidence'] if not e.get('error')]
        refs=response.evidence_refs or [e['source_ref'] for e in successful][-12:]
        if not successful:
            text=safe_explanation(SimpleNamespace(reply=response.text,intent='reply',dialogue=None),[]) or cancelled_goal_reply(response.text,r)
            if not text:raise DomainError('UNSUPPORTED_ANSWER','未取得数据，不能宣称具体金额或办理成功',422)
        else:text,inserted=render_response(response.model_copy(update={'evidence_refs':refs}),active)
        if any(e['tool'] in {'financial_planning_basis','calculate'} for e in active['evidence']):
            # Corrected attempts remain in the audit trace, not as unfinished work.
            presentation=attempts.presentation(active)
            if response.synthesis:
                text+='\n\n'+planning_reply.notes(presentation,selected)
                unresolved=[e['error']['message'] for e in presentation['evidence'] if e.get('error')]
                if unresolved:text+='\n未完成：'+'；'.join(dict.fromkeys(unresolved))+'。'
            else:text+=('\n\n'+read_presentation.fact_block(active) if not inserted else '')+'\n\n'+facts.footer(presentation)
        elif any(e['tool'] in registry.DESCRIPTIONS and e['tool']!='search_knowledge' for e in active['evidence']):
            from app.agent.dialogue_schema import DialogueTurn
            from app.agent.tool_catalog import READ_DESCRIPTIONS
            visible=[e for e in attempts.presentation(active)['evidence'] if e['tool'] in registry.DESCRIPTIONS and e['tool'] not in READ_DESCRIPTIONS]
            if inserted and any(e['tool'] in {'summarize_spending','read_subscriptions','identify_subscriptions'} and not e.get('error') for e in visible):
                text+='\n\n'+read_presentation.scope_notes(active)
            elif visible:text+='\n\n'+evidence_reply(SimpleNamespace(reply=None,intent='reply',dialogue=DialogueTurn(behavior='insert_query')),visible)
    else:
        text=safe_explanation(SimpleNamespace(reply=response.text,intent='reply',dialogue=None),[]) or cancelled_goal_reply(response.text,r)
        if not text:raise DomainError('UNSUPPORTED_ANSWER','回答含未查询的金额或执行断言，请先取证或改为说明未知',422)
    responding=next((n for n in reversed(r['nodes']) if n['name']=='respond' and n['status'] in {'pending','running'}),None)
    if set((responding or {}).get('goal_ids',r.get('turn_goal_ids',[]))) & set(r.get('revision_pending',[])):
        raise DomainError('GOAL_REVISION_REQUIRED','目标更新尚未成功，不能给出依赖旧约束的结论；先修复revise_goals或说明尚待核对。',409)
    graph.responded(r,response)
    for n in r['nodes']:
        if n['name']=='respond' and n['status'] in {'pending','running'}:n['status']='succeeded'
    needs_input=response.needs_input or bool(response.synthesis and response.synthesis.questions)
    r['status']='gathering_info' if needs_input else 'partially_completed' if any(attempts.unresolved(n) for n in r['nodes']) else 'completed'
    save(task,r);current=view(db,task)
    pending=any(op['status']=='pending_confirmation' for op in current.get('authorization_operations',[]))
    _say(db,task,text,'awaiting_confirmation' if pending else r['status']);return view(db,task)


def send(db,session,task,text):
    from app.agent.service import _task,_message,view
    from app.agent.dialogue import context_for
    from app.agent.control import begin_request
    request_id=begin_request(db,task.id)
    with atomic_write(db):
        session,task=load(db,session,task.id);ctx=json.loads(task.context_json)
        from app.agent.control import assert_request
        assert_request(db,task.id,request_id)
        ctx.pop('pending_subscription_query',None);ctx.pop('pending_risk_assessment',None)
        task.context_json=json.dumps(ctx,ensure_ascii=False)
        _message(db,task,'user',text)
        history=context_for(db,task,view(db,task))['messages']
        r={'id':uuid4().hex,'request':text,'nodes':[],'evidence':[],'facts':[],'messages':history,
            'user_messages':[m['content'] for m in history if m['role']=='user'],
            'queue':[],'status':'running','wait':None,'completed_operations':[],'prepared_signatures':{}}
        previous=get(task)
        continued=checkpoints.continuation(db,session,task,previous,text,history)
        if continued:r=continued
        else:graph.initialize(task,r);r['evidence_version']=checkpoints.version(db,task)
        r['request_id']=request_id
        wait=r.get('wait') or {}
        if wait.get('kind')=='authorization' and re.fullmatch(r'\s*(?:确认|同意|确认取消|同意取消)[。！!\s]*',text):
            current=view(db,task)
            pending=next((op for op in current.get('authorization_operations',[]) if op['resource_kind']==wait['resource_kind']
                and op['resource_id']==wait['resource_id'] and op['status']=='pending_confirmation'),None)
            is_cancel=(current.get('service_request') or {}).get('kind')=='cancel_contract'
            if pending and ('取消' not in text or is_cancel):
                r['status']='awaiting_confirmation';save(task,r)
                from app.agent.service import _say
                _say(db,task,'原固定单仍待确认，没有重复建单，也未执行。请在页面核对固定条款并手动确认；聊天中的同意不会授权办理。','awaiting_confirmation')
                return view(db,task)
        save(task,r);revision=task.updated_at_utc;commit(db)
    return run(db,session,task.id,r,request_id,revision)


def run(db,session,tid,r,request_id,revision):
    from app.agent.service import _say,view
    from app.agent.model_budget import budget
    setting=config();used_tools=0;feedback=0;draining=False
    with budget(setting['calls'],setting['seconds']):
        try:
            while True:
                if draining:
                    prior={n['id']:n for n in r['nodes']}
                    ready=next((i for i,n in enumerate(r['queue']) if
                        n.get('policy',{}).get('effect') in {'read','calculate'} and
                        n.get('policy',{}).get('risk_floor')=='green' and n['name'] not in protocol.OPERATIONS and
                        all(d in prior and prior[d]['status']=='succeeded' and not prior[d].get('invalidated') for d in n.get('depends_on',[]))),None)
                    if ready is None:
                        with atomic_write(db):
                            task=check(db,session,tid,r,request_id,revision)
                            start=r.get('turn_start_node_count',0)
                            refs={ref for n in r['nodes'][start:] if n.get('policy',{}).get('effect')=='read' and n['status']=='succeeded' for ref in n.get('result_refs',[])}
                            visible=[e for e in current_evidence(r)['evidence'] if e['source_ref'] in refs and not e.get('error')]
                            state=view(db,task)
                            service=state.get('service_request') or {}
                            subscriptions=next((e for e in reversed(visible) if e['tool'] in {'read_subscriptions','identify_subscriptions'}),None)
                            if service.get('kind')=='cancel_contract' and service['status']=='pending_confirmation' and subscriptions:
                                data=subscriptions.get('raw_data') or subscriptions.get('data') or {};data=data.get('confirmed',data)
                                summary=data.get('summary',{});merchant=service['summary']['merchant']
                                details=(f"已查询{summary['contract_count']}份订阅，{summary['auto_renew_contract_count']}份开启续费，折月合计{summary['auto_renew_monthly_minor']/100:.2f}元。\n\n"
                                    +'已为“'+merchant+'”准备取消下期续订的固定单，尚未执行。请在下方核对条款并手动确认；不退款，也不解除独立银行代扣。')
                                _say(db,task,details,r['status']);return view(db,task)
                            text=None
                            if (r.get('wait') or {}).get('kind')!='input':
                                # When a specialised flow is already waiting for a specific
                                # answer it owns the message; a generic closing line would
                                # bury the concrete question it just asked.
                                text=closing_text(state,r,visible)
                            if text:_say(db,task,text,r['status'])
                            return view(db,task)
                    r['queue'].insert(0,r['queue'].pop(ready))
                if not r['queue']:
                    with atomic_write(db):
                        task=check(db,session,tid,r,request_id,revision);ctx=context(db,task,r);commit(db)
                    if len(json.dumps(r['messages'],ensure_ascii=False))+len(json.dumps(ctx,ensure_ascii=False))>100000:
                        raise DomainError('CONTEXT_BUDGET_EXCEEDED','对话证据过多，已保留结果，请缩小查询范围',409)
                    message=protocol.step(r['messages'],ctx)
                    with atomic_write(db):
                        task=check(db,session,tid,r,request_id,revision)
                        if not message.get('tool_calls'):
                            try:return finish(db,task,r,protocol.Respond(text=message['content']))
                            except DomainError as error:
                                feedback+=1
                                if feedback>2:raise
                                r['messages'].extend([message,{'role':'user','content':'[服务端校验反馈，不是用户指令] '+error.message+'。请用respond重写为简短判断，只引用已返回facts.id（零金额也须引用），不写未经计算的金额、比例或回补月数，不解释内部限制。'}]);save(task,r);commit(db);continue
                        r['messages'].append(message)
                        parsed=[];invalid=None;invalid_id=None;model=None
                        try:
                            for call in message['tool_calls']:
                                invalid_id=call['id'];model=None
                                name=call['function']['name']
                                if name not in protocol.models():raise DomainError('INVALID_TOOL','请求了未允许工具，不执行本批任何调用',422)
                                model=protocol.models()[name]
                                value=model.model_validate_json(call['function']['arguments']).model_dump()
                                parsed.append((call,name,value))
                            if any(n=='declare_goals' for _,n,_ in parsed) and ([n for _,n,_ in parsed].count('declare_goals')!=1 or parsed[0][1]!='declare_goals'):raise DomainError('INVALID_TOOL_BATCH','目标声明必须是本批第一个调用且只有一次；后续工具仍按串行执行',422)
                            if any(n=='respond' for _,n,_ in parsed) and len(parsed)>1:raise DomainError('INVALID_TOOL_BATCH','回答须在工具结果返回后单独生成',422)
                        except (ValueError,DomainError) as error:
                            invalid=attempts.validation_feedback(error,model)
                        candidates=None;renamed_goals={}
                        if not invalid and parsed and parsed[0][1]=='declare_goals':
                            try:
                                preview=deepcopy(r);value=dict(parsed[0][2]);value.pop('execution',None)
                                declared=graph.declare(preview,value);candidates=preview['goals']
                                renamed_goals=declared.get('renamed_goal_ids') or {}
                            except (DomainError,ValueError) as error:
                                invalid_id=parsed[0][0]['id'];invalid=attempts.validation_feedback(error,None)
                        if renamed_goals:
                            # Write the server-assigned ids back into the outgoing call so the
                            # executed declaration, graph.bind and graph.validate all agree.
                            for goal in parsed[0][2].get('goals') or []:
                                if goal.get('id') in renamed_goals:goal['id']=renamed_goals[goal['id']]
                        batch_start=len(r['queue'])
                        for call in message['tool_calls']:
                            node={'id':'s'+str(len(r['nodes'])+len(r['queue'])+1),'model_call_id':call['id'],
                                'name':call['function']['name'],'arguments':next((v for c,n,v in parsed if c['id']==call['id']),{}),
                                'title':{'calculate':'精确计算','respond':'整理回答','manage_task':'任务控制'}.get(call['function']['name'],
                                    protocol.DESCRIPTIONS.get(call['function']['name'],registry.DESCRIPTIONS.get(call['function']['name'],'工具调用')).split('：',1)[0].split('。',1)[0][:24]),
                                'depends_on':[],'status':'pending',
                                'invalid_arguments_digest':attempts.invalid_digest(call['function']['arguments']) if invalid and call['id']==invalid_id else None,
                                'batch_error':invalid if call['id']==invalid_id else {'code':'BATCH_NOT_EXECUTED','message':'同批其他调用校验失败，本调用没有执行；修正后可重新调用。'} if invalid else None}
                            binding=node['arguments'].pop('execution',None)
                            try:graph.bind(task,r,node,binding,candidates)
                            except (DomainError,ValueError) as error:
                                invalid_id=call['id'];invalid=attempts.validation_feedback(error,None);node['batch_error']=invalid;graph.bind(task,r,node,{})
                            r['queue'].append(node)
                        if invalid:
                            # Deliberate invariant: a batch is validated as a unit, so a
                            # rejected call voids the whole batch (INVALID_TOOL contract).
                            # Callers keep validated arguments; the repair path re-issues.
                            for node in r['queue'][batch_start:]:
                                node['batch_error']=invalid if node['model_call_id']==invalid_id else {'code':'BATCH_NOT_EXECUTED','message':'同批其他调用校验失败，本调用没有执行；修正后可重新调用。'}
                                node['depends_on']=[];node['dependency_sources']={}
                                node['goal_ids']=[] if node['name']=='declare_goals' else [g['id'] for g in r.get('goals',[])]
                        save(task,r);commit(db)
                with atomic_write(db):
                    task=check(db,session,tid,r,request_id,revision)
                    if used_tools>=setting['tools'] or len(r['nodes'])>=64:raise DomainError('TOOL_BUDGET_EXCEEDED','工具调用已达到本轮上限，保留已完成结果',409)
                    node=graph.next_ready(r);r['nodes'].append(node);used_tools+=1
                    name,args=node['name'],node['arguments']
                    try:
                        graph.start(r,node)
                        if name!='respond' and len(attempts.prior_rejections(r,node))>=2:
                            raise DomainError('TOOL_RETRY_EXHAUSTED','相同工具和参数已经连续失败，停止重复执行；请修正已指出的字段，或说明缺项并请求人工处理。',409)
                        if node.get('batch_error'):raise DomainError(node['batch_error']['code'],node['batch_error']['message'],422)
                        if name=='declare_goals':
                            data=graph.declare(r,args)
                            if renamed_goals:
                                data['renamed_goal_ids']=renamed_goals
                                data['next']=('目标ID已由服务端重新分配：'+'、'.join(f'{k}→{v}' for k,v in renamed_goals.items())
                                    +'。请用返回的id绑定goal_ids，不要继续使用旧ID。'+data.get('next',''))
                            result=observation(r,node,data);append_result(r,node,result);save(task,r);commit(db);continue
                        if name=='prepare_review_plan':
                            from app.agent.workflow_plans import prepare as review_draft
                            data=review_draft(task,r,args,node)
                            result=observation(r,node,data);append_result(r,node,result)
                            for goal in r['goals']:
                                if goal['id'] in node['goal_ids']:goal['needs_input']=True
                            save(task,r);commit(db)
                            _say(db,task,'只读复查草稿已保存，尚未安排执行。请到“联动计划”核对时间、频率、参考额和授权期限，勾选范围后手动确认。结果保存在页面，不会转账、锁款或发送站外消息。','gathering_info')
                            return view(db,task)
                        if name=='revise_goals':
                            data=checkpoints.revise(db,session,task,r,args)
                            result=observation(r,node,data);append_result(r,node,result);save(task,r);revision=task.updated_at_utc;commit(db);continue
                        if name=='respond':return finish(db,task,r,protocol.Respond.model_validate(args))
                        if name in protocol.OPERATIONS:
                            wait,result=prepare(db,session,task,r,node)
                            if wait is None:
                                obs=observation(r,node,result);append_result(r,node,obs);save(task,r);revision=task.updated_at_utc;commit(db);continue
                            node['status']='awaiting_confirmation' if wait['kind'] in {'authorization','subscription_precondition'} else 'pending'
                            graph.waiting(task,r,node,wait,result)
                            r['wait']=wait;r['status']='awaiting_confirmation' if wait['kind']!='input' else 'gathering_info'
                            save(task,r);revision=task.updated_at_utc;commit(db);draining=True;continue
                        if name=='calculate':data=calculate(r,node)
                        elif name=='manage_task':
                            quote=args['instruction_quote'];action=args['action']
                            if not control_grounded(action,quote,r['request']):
                                # The user's words decide the control, not the model's label:
                                # adopt the action only when the quote grounds exactly one.
                                grounded=[a for a in ('pause','cancel','continue','handoff') if control_grounded(a,quote,r['request'])]
                                if len(grounded)!=1:
                                    raise DomainError('UNGROUNDED_CONTROL','任务控制缺少用户明确指令',422)
                                action=grounded[0]
                            if action=='cancel':
                                from app.agent.cancellation_language import goal_cancel_allowed
                                if not goal_cancel_allowed(quote,r['request'],[g['text'] for g in r['goals']]):
                                    raise DomainError('UNGROUNDED_CONTROL','取消订阅是业务需求，不是取消整个任务。继续准备固定单或澄清对象，不能终止用户目标。',422)
                            from app.agent.control import change
                            from app.agent.service import cancel
                            if action=='cancel':cancel(db,session,tid)
                            else:change(db,task,'resume' if action=='continue' else action,quote)
                            data={'state':action,'execution':'未授权任何新业务'}
                            result=observation(r,node,data);append_result(r,node,result);r['status']='paused' if action in {'pause','handoff'} else 'cancelled' if action=='cancel' else 'completed'
                            save(task,r);commit(db)
                            if action in {'pause','handoff'}:
                                # Control actions must answer the user, not leave the previous
                                # "please confirm this order" message as the latest reply.
                                from app.agent.service import _say
                                _say(db,task,('已暂停当前任务，待办不会自动继续；已发生的业务不受影响，原固定单仍在页面上待你处理。需要继续时告诉我。'
                                    if action=='pause' else '已请求人工接管；已发生的业务不受影响，你仍可在页面查看原固定单。'),'paused')
                            return view(db,task)
                        else:
                            if name in SELF_ASSET_TOOLS:
                                foreign=foreign_subject_message(db,session,r['request'])
                                if foreign:raise DomainError('FOREIGN_SUBJECT',foreign,403)
                            period=period_mismatch_message(name,args,r['request'],demo_day(db))
                            if period:raise DomainError('PERIOD_UNGROUNDED',period,422)
                            signature=name+json.dumps(args,sort_keys=True)
                            duplicate=next((e for e in r['evidence'] if not e.get('error') and e['source_ref'] not in r.get('stale_evidence',[]) and e['tool']+json.dumps(e['query'],sort_keys=True)==signature),None)
                            if duplicate:
                                original=next(n for n in r['nodes'] if duplicate['source_ref'].endswith(':'+n['id']))
                                result={'call_id':original['id'],'status':'completed','reused':True,'source_ref':duplicate['source_ref'],'data':duplicate['data'],
                                    'facts':[f for f in r['facts'] if f['source_ref']==duplicate['source_ref']]}
                                node['status']='succeeded';node['reused_from']=original['id'];graph.observed(r,node,result);attempts.recovered(r,node)
                                append_result(r,node,result);save(task,r);commit(db);continue
                            data=registry.execute(db,session_id=session.id,owner_id=session.owner_id,name=name,arguments=args,agent_task_id=tid)
                        result=observation(r,node,data);record_step(db,task,node,data);append_result(r,node,result)
                    except (DomainError,HTTPException,ValueError) as problem:
                        detail=getattr(problem,'detail',{})
                        code=getattr(problem,'code','INVALID_ARGUMENTS' if isinstance(problem,ValueError) else detail.get('error_code','READ_REJECTED') if isinstance(detail,dict) else 'READ_REJECTED')
                        message=getattr(problem,'message',('计算参数格式错误；calculate.calculation.inputs须为操作数数组，每项引用本轮node/key，或提供value/unit/label。' if name=='calculate' else '当前工具参数格式不符，请按此工具的字段与类型修正，未执行该调用。') if isinstance(problem,ValueError) else detail.get('message','请求被校验拒绝') if isinstance(detail,dict) else '请求被校验拒绝')
                        if code=='SUBSCRIPTION_CONFIRMATION_REQUIRED' and not draining:
                            from app.agent.conversation_reads import stage_subscription_access
                            stage_subscription_access(task,r['request'],[{'tool':name,'query':args,'error':{'code':code}}])
                            ctx=json.loads(task.context_json);pending=ctx['pending_subscription_query'];pending['runtime_id']=r['id'];task.context_json=json.dumps(ctx,ensure_ascii=False)
                            r['wait']={'kind':'subscription_query','node_id':node['id'],'pending_id':pending['id'],'plan_version':task.plan_version,'dataset_version':task.dataset_version,'expires_at':(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat()}
                            node['status']='awaiting_confirmation';node['next_step']='请在页面确认本人订阅查询范围；该授权不包含取消代扣。';r['status']='awaiting_confirmation';save(task,r)
                            _say(db,task,'这项工具需要先确认本人订阅查询范围。请在下面手动确认；确认后继续刚才的分析，不会取消或扣款。','gathering_info');return view(db,task)
                        if name=='revise_goals' and args.get('action')=='update':
                            # Deliberate invariant (see test_failed_revision_cannot_be_bypassed_
                            # with_new_calculation): while a user-stated change is unrecorded,
                            # later calculate/respond on that goal stay locked.
                            owned={g['id'] for g in r.get('goals',[])}
                            r['revision_pending']=list(dict.fromkeys(r.get('revision_pending',[])+[gid for gid in args.get('goal_ids',[]) if gid in owned]))
                        error={'code':code,'message':message}
                        if node.get('batch_error'):error.update(node['batch_error'])
                        node['error_code']=code;node['error_message']=error['message']
                        result=observation(r,node,None,error);record_step(db,task,node,{'error':error});append_result(r,node,result)
                    save(task,r);revision=task.updated_at_utc;commit(db)
        except DomainError as error:
            with atomic_write(db):
                from app.agent.control import assert_request
                session,task=load(db,session,tid)
                try:assert_request(db,tid,request_id)
                except DomainError:return view(db,task)
                if get(task).get('id')!=r['id'] or task.updated_at_utc!=revision or graph.typed(r) and r['plan_version']!=task.plan_version:return view(db,task)
                r['status']='partially_completed' if r['evidence'] else 'gathering_info';r['failure_classification']=error.code;save(task,r)
                text=read_presentation.fallback(current_evidence(r),error.code)
                _say(db,task,text,r['status']);return view(db,task)


def after_input(db,session,tid,result):
    """A trusted form updates the fixed terms; it never grants execution permission."""
    from app.agent.service import view
    with atomic_write(db):
        session,task=load(db,session,tid);r=get(task)
        if not r or not r.get('wait') or r['wait']['kind'] not in {'input','subscription_precondition'}:return result
        from app.agent.control import assert_running
        assert_running(db,tid)
        if result['status']=='completed' and not result.get('prerequisites') and not result.get('authorization_operations'):
            node=next(n for n in r['nodes'] if n['id']==r['wait']['node_id'])
            if node['name'] in {'compare_wealth_products','request_risk_assessment','plan_budget'}:
                data={'status':'completed','messages':[m['content'] for m in result['messages'][-2:] if m['role']=='assistant'],'steps':result.get('steps',[])[-1:]}
                append_result(r,node,observation(r,node,data));r['wait']=None;r['status']='running';save(task,r)
                from app.agent.control import begin_request
                request_id=begin_request(db,tid);revision=task.updated_at_utc;commit(db)
                resume=True
            else:resume=False
        else:resume=False
        if not resume:
            wait=prepared_state(task,view(db,task));wait.update(node_id=r['wait']['node_id'],plan_version=task.plan_version,
                dataset_version=task.dataset_version,expires_at=(datetime.now(timezone.utc)+timedelta(minutes=15)).isoformat())
            r['wait']=wait;r['status']='awaiting_confirmation' if wait['kind']!='input' else 'gathering_info'
            node=next(n for n in r['nodes'] if n['id']==wait['node_id']);node['status']='awaiting_confirmation' if wait['kind']!='input' else 'pending'
            graph.waiting(task,r,node,wait,result)
            save(task,r);commit(db);return view(db,task)
    return run(db,session,tid,r,request_id,revision)


def after_authorization(db,session,tid,kind,rid,action_id,result,*,cancelled=False):
    """Consume a durable receipt, never call the financial executor again."""
    from app.agent.service import view
    from app.agent.control import begin_request,assert_running
    with atomic_write(db):
        session,task=load(db,session,tid);r=get(task)
        if not r or not r.get('wait'):return result
        wait=r['wait']
        if wait.get('kind')!='authorization' or wait.get('action_id')!=action_id or wait.get('resource_kind')!=kind or wait.get('resource_id')!=rid:return result
        assert_running(db,tid)
        receipt=json.loads(task.context_json).get('authorization_receipts',{}).get(action_id)
        if not cancelled and receipt!={'kind':kind,'resource_id':rid}:raise DomainError('AUTHORIZATION_REQUIRED','未取得对应的已确认业务回执',403)
        if wait['plan_version']!=task.plan_version or wait['dataset_version']!=task.dataset_version:
            r['status']='superseded';save(task,r);commit(db);return view(db,task)
        from app.agent.authorization import RESOURCES
        current=view(db,task);record=current.get(RESOURCES[kind][0])
        if not record or record['id']!=rid:raise DomainError('STALE_RESOURCE','当前回执与等待中的调用不一致',409)
        if cancelled and record['status'] not in {'cancelled','expired'}:return result
        data={k:record[k] for k in ('id','status','receipt','summary','execute_at_utc') if k in record}
        data.update(resource_kind=kind,resource_id=rid,authorization='用户已取消本单，不能重新准备或付款' if cancelled else '用户已在固定操作单完成授权，状态以本回执为准')
        node=next(n for n in r['nodes'] if n['id']==wait['node_id'])
        invalidate_snapshot(r)
        obs=observation(r,node,data);append_result(r,node,obs)
        r['completed_operations'].append({'name':node['name'],'resource_kind':kind,'resource_id':rid,'status':record['status']})
        r['wait']=None;r['status']='running';save(task,r)
        request_id=begin_request(db,tid);revision=task.updated_at_utc;commit(db)
    return run(db,session,tid,r,request_id,revision)


def queue_authorization(background,db,session,tid,kind,rid,action_id,result,*,cancelled=False):
    """Return the durable receipt promptly; synthesis runs after the HTTP response."""
    from app.agent.service import _task,view
    task=_task(db,session,tid);r=get(task);wait=(r or {}).get('wait') or {}
    if wait.get('kind')!='authorization' or wait.get('action_id')!=action_id:return result
    from sqlalchemy.orm import sessionmaker
    factory=sessionmaker(bind=db.get_bind(),expire_on_commit=False)
    runtime_id=r['id']
    with atomic_write(db):
        # A duplicate confirmation can wait behind the original follow-up.
        # Never write the pre-lock JSON over completed or newer task state.
        session,task=load(db,session,tid);r=get(task);wait=(r or {}).get('wait') or {}
        if (not r or r['id']!=runtime_id or wait.get('kind')!='authorization'
                or wait.get('action_id')!=action_id or wait.get('resource_kind')!=kind
                or wait.get('resource_id')!=rid or wait.get('plan_version')!=task.plan_version
                or wait.get('dataset_version')!=task.dataset_version):return view(db,task)
        from app.agent.control import assert_running
        try:assert_running(db,tid)
        except DomainError as error:
            if error.code=='TASK_PAUSED':return view(db,task)
            raise
        r['status']='running';save(task,r);task.status='running_readonly';commit(db)
    from app.domain.workflows import enqueue_receipt,run_job
    with atomic_write(db):
        session,task=load(db,session,tid)
        job_id=enqueue_receipt(db,task,kind,rid,action_id,cancelled=cancelled)
    if job_id:background.add_task(run_job,factory,job_id)
    current=view(db,task)
    # Release the request session's read transaction before a background writer.
    db.rollback()
    return current


def _authorized_background(factory,sid,tid,runtime_id,kind,rid,action_id,cancelled=False,*,expected_request_id=None):
    from app.data.models import DemoSession
    from app.agent.service import _task,view,_say
    with factory() as db:
        try:
            session=db.get(DemoSession,sid);task=_task(db,session,tid);r=get(task)
            if not r or r['id']!=runtime_id or expected_request_id is not None and r.get('request_id')!=expected_request_id:return
            after_authorization(db,session,tid,kind,rid,action_id,view(db,task),cancelled=cancelled)
        except DomainError:
            # Pauses, new questions and expired sessions cannot be overridden by
            # the asynchronous analysis. The financial receipt already exists.
            return


def resume_control(db,session,tid,result):
    from app.agent.control import begin_request,assert_running
    recovery=None
    with atomic_write(db):
        session,task=load(db,session,tid);r=get(task)
        if not r:return result
        assert_running(db,tid)
        wait=r.get('wait') or {}
        if wait:
            receipt=json.loads(task.context_json).get('authorization_receipts',{}).get(wait.get('action_id'))
            if wait.get('kind')!='authorization' or receipt!={'kind':wait.get('resource_kind'),'resource_id':wait.get('resource_id')}:return result
            recovery=(wait['resource_kind'],wait['resource_id'],wait['action_id'])
        else:
            if r['status'] not in {'running','paused','partially_completed'}:return result
            if graph.typed(r):checkpoints.reconcile(db,session,task,r)
            r['status']='running';save(task,r)
            request_id=begin_request(db,tid);revision=task.updated_at_utc
        commit(db)
    if recovery:return after_authorization(db,session,tid,*recovery,result)
    return run(db,session,tid,r,request_id,revision)


def resume_subscription(db,session,tid,pending_id):
    from app.agent.service import view
    from app.agent.control import begin_request,assert_running
    from app.domain.security import confirm_subscription_access
    with atomic_write(db):
        session,task=load(db,session,tid);ctx=json.loads(task.context_json);r=get(task)
        if ctx.get('completed_subscription_query')==pending_id:return view(db,task)
        pending=ctx.get('pending_subscription_query',{});wait=(r or {}).get('wait') or {}
        if not r or pending.get('id')!=pending_id or pending.get('runtime_id')!=r['id'] or wait.get('pending_id')!=pending_id or wait.get('kind')!='subscription_query':
            raise DomainError('STALE_RESOURCE','查询问题已变化，请使用当前确认卡',409)
        assert_running(db,tid)
        if wait['plan_version']!=task.plan_version or wait['dataset_version']!=task.dataset_version or wait['expires_at']<datetime.now(timezone.utc).isoformat():
            raise DomainError('STALE_RESOURCE','查询检查点已失效，请重新提出问题',409)
        node=next(n for n in r['nodes'] if n['id']==wait['node_id'])
        if node['name']=='prepare_merchant_contract_cancel':
            protocol.models()[node['name']].model_validate(node['arguments']);confirm_subscription_access(db,session.id)
            ctx.pop('pending_subscription_query',None);ctx['completed_subscription_query']=pending_id;task.context_json=json.dumps(ctx,ensure_ascii=False)
            wait,result=prepare(db,session,task,r,node);r['wait']=wait;r['status']='awaiting_confirmation' if wait['kind']!='input' else 'gathering_info'
            node['status']='awaiting_confirmation' if wait['kind']!='input' else 'pending';save(task,r);commit(db);return view(db,task)
        if node['name'] not in {'read_subscriptions','identify_subscriptions','read_financial_history','read_service_requests'}:raise DomainError('INVALID_TOOL','此确认只支持受保护查询',422)
        registry.validate(node['name'],node['arguments']);confirm_subscription_access(db,session.id)
        data=registry.execute(db,session_id=session.id,owner_id=session.owner_id,name=node['name'],arguments=node['arguments'],agent_task_id=tid)
        # A read-consent grant changes no financial state. Keep the timestamped
        # observations if their owned-data version is unchanged; otherwise make
        # the old financial snapshot stale before the model can reuse it.
        current_version=checkpoints.version(db,task)
        if r.get('evidence_version')!=current_version:invalidate_snapshot(r)
        r['evidence_version']=current_version
        append_result(r,node,observation(r,node,data));record_step(db,task,node,data)
        ctx=json.loads(task.context_json);ctx.pop('pending_subscription_query',None);ctx['completed_subscription_query']=pending_id;task.context_json=json.dumps(ctx,ensure_ascii=False)
        r['wait']=None;r['status']='running';save(task,r)
        request_id=begin_request(db,tid);revision=task.updated_at_utc;commit(db)
    return run(db,session,tid,r,request_id,revision)
