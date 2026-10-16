"""Model extracts meaning; domain code owns suitability, numbers and settlement facts."""
import json
import re
from sqlalchemy import select
from app.data.atomic import atomic_write, commit
from app.data.models import Account, AgentStep
from app.domain.errors import DomainError
from app.domain.operations import _session, _now
from app.domain.wealth import (compare_products, assessment_view, current_assessment, decimal_hundredths,
    prepare_order, portfolio, invalidate_task_order, cancel_order, task_state)

INTENTS={'wealth_assessment','wealth_compare','wealth_buy','wealth_redeem','wealth_status'}

def detected(text,context):
    if re.search(r'风险评估|风险测评',text) and not re.search(r'按.*风险评估',text):return 'wealth_assessment'
    if '赎回' in text:return 'wealth_redeem'
    if '申购' in text or re.search(r'(?:买|购买).*(?:P[123]|理财)',text,re.I):return 'wealth_buy'
    if re.search(r'理财.*(?:订单|持仓)|(?:订单|持仓).*理财',text):return 'wealth_status'
    if re.search(r'推荐|比较|对比',text) and re.search(r'理财|P[123]|风险评估',text,re.I):return 'wealth_compare'
    if context.get('last_wealth_query') and re.search(r'收益|保证|保本|另外两款|不选|锁定',text):return 'wealth_compare'
    return None

def mock_slots(text,intent,context,account_aliases=()):
    product=re.findall(r'(?<![A-Za-z0-9])P\d+(?![A-Za-z0-9])',text,re.I)
    amount=re.search(r'([0-9]+(?:\.[0-9]+)?)\s*元',text)
    units=re.search(r'([0-9]+(?:\.[0-9]+)?)\s*份',text)
    horizon=re.search(r'([0-9]+)\s*天',text)
    accounts=list(dict.fromkeys([a for a in account_aliases if a in text]+re.findall(r'账户[A-Z]',text)))
    last=context.get('last_wealth_query',{}) if intent=='wealth_compare' else context.get('wealth_slots',{}) if context.get('wealth_intent')==intent else {}
    return dict(product=product[0].upper() if len(product)==1 else last.get('product'),
        amount_yuan=amount.group(1) if amount else last.get('amount_yuan'),
        units=units.group(1) if units else last.get('units'),
        account=accounts[0] if len(accounts)==1 else last.get('account'),
        horizon_days=int(horizon.group(1)) if horizon else 30 if re.search(r'一个月|一月内',text) else last.get('horizon_days'))

def comparison_reply(result):
    reply=f"已读取有效风险评估R{result['assessment']['max_risk']}；预算{result['budget_minor']/100:,.2f}元，{result['horizon_days']}天内可能用款。\n"
    for p in result['products']:
        reply+=f"{p['id']} {p['name']}：风险R{p['risk_level']}，起购{p['min_minor']/100:,.2f}元，锁定{p['lock_days']}天；{p['redemption']}；{p['yield_text']}。{'可考虑，符合评估、预算和用款期限' if p['matches'] else '不匹配：'+ '；'.join(p['reasons'])}。\n"
    return reply+('当前没有合适的模拟产品。\n' if not result['recommended_ids'] else '可考虑：'+ '、'.join(result['recommended_ids'])+'。\n')+result['yield_notice']

def dispatch(db,session,task,text, *, decision_hint=None):
    from app.agent.service import _message, _say, _task, view, _dump, _record_step
    context=json.loads(task.context_json)
    intent=decision_hint.intent if decision_hint is not None and decision_hint.intent in INTENTS else detected(text,context)
    if intent is None and task.intent in {'wealth_buy','wealth_redeem'} and task.status=='gathering_info' and not re.search(r'查询|余额|取消|谢谢',text):
        intent=task.intent
    if intent is None:return None
    if task.stop_requested:return view(db,task)
    # Requests are durable before model work, and the model never holds a DB write lock.
    _message(db,task,'user',text,already_recorded=decision_hint is not None)
    version=context.get('wealth_request_version',0)+1
    context['wealth_request_version']=version;task.context_json=_dump(context);commit(db)
    account_aliases=db.scalars(select(Account.alias).where(Account.owner_id==session.owner_id)).all()
    slots=mock_slots(text,intent,context,account_aliases)
    if task.mode=='live':
        from app.agent.language import interpret,payload_for
        payload=payload_for(db,task,view(db,task))
        commit(db)
        try: decision=decision_hint if decision_hint is not None else interpret(payload)
        except DomainError as error:
            with atomic_write(db):
                task=_task(db,session,task.id)
                if not task.stop_requested and json.loads(task.context_json).get('wealth_request_version')==version:
                    _say(db,task,error.message,'gathering_info')
                return view(db,task)
        if decision.intent!=intent:
            with atomic_write(db):
                task=_task(db,session,task.id)
                if not task.stop_requested and json.loads(task.context_json).get('wealth_request_version')==version:
                    _say(db,task,'这次未能准确理解理财需求，请补充产品、金额或份额及账户；没有办理交易。','gathering_info')
                return view(db,task)
        # A model must not silently replace exact numbers or product IDs supplied in this turn.
        explicit=mock_slots(text,intent,{},account_aliases)
        for key,value in [('product',decision.product),('amount_yuan',decision.amount_yuan),('units',decision.units),('horizon_days',decision.horizon_days)]:
            if explicit.get(key) is not None and value is not None:
                a,b=explicit[key],value
                same=(str(a).upper()==str(b).upper()) if key=='product' else a==b if key=='horizon_days' else decimal_hundredths(a,key)==decimal_hundredths(b,key)
                if not same:
                    with atomic_write(db):
                        task=_task(db,session,task.id)
                        if not task.stop_requested and json.loads(task.context_json).get('wealth_request_version')==version:
                            invalidate_task_order(db,task)
                            _say(db,task,'模型理解与原话中的明确产品、金额或份额不一致；请重新核对，没有准备或办理新交易。','gathering_info')
                        return view(db,task)
        # Only explicit semantics, plus previously established same-request slots.
        for key,value in [('product',decision.product),('account',decision.source),('amount_yuan',decision.amount_yuan),('units',decision.units),('horizon_days',decision.horizon_days)]:
            if value is not None:slots[key]=value
    sid=session.id;tid=task.id
    with atomic_write(db):
        session,_=_session(db,sid,_now());task=_task(db,session,tid);latest=json.loads(task.context_json)
        if task.stop_requested or latest.get('wealth_request_version')!=version:return view(db,task)
        if intent in {'wealth_buy','wealth_redeem'} and re.search(r'不要(?:申购|买|赎回)|别(?:买|赎回)|不想|怎么|如何|能否|是否|[?？]|如果|否则|预约|定时|明天|每月|每周|美元',text):
            invalidate_task_order(db,task)
            _say(db,task,'请明确当前要准备的模拟产品、金额或份额和账户；条件或预约理财暂不支持，未准备或执行交易。','gathering_info')
            return view(db,task)
        if intent in {'wealth_buy','wealth_redeem'}:
            # A modified request makes old credentials unusable even if the new parameters are invalid.
            invalidate_task_order(db,task)
        try:
            if intent=='wealth_assessment':
                assessment=current_assessment(db,session.owner_id)
                reply=('尚未完成有效风险评估。' if assessment is None else '已有有效评估，可在下方重新完整回答以更新。')+'请在“模拟理财”区域回答投资经验、可接受本金损失、资金使用期限、收入与应急资金四个问题后提交。不是固定风险标签；结果有形成时间、有效期和适用范围。'
                _say(db,task,reply,'completed')
            elif intent=='wealth_compare':
                if not slots.get('amount_yuan') or not slots.get('horizon_days'):
                    _say(db,task,'请提供可投资预算，以及多久内可能需要使用这笔钱。','gathering_info')
                else:
                    result=compare_products(db,owner_id=session.owner_id,budget_minor=decimal_hundredths(slots['amount_yuan'],'预算'),horizon_days=slots['horizon_days'])
                    latest['last_wealth_query']=slots;task.context_json=_dump(latest)
                    _record_step(db,task,'compare_wealth',{'budget_minor':result['budget_minor'],'horizon_days':result['horizon_days']},result)
                    _say(db,task,comparison_reply(result),'completed')
            elif intent=='wealth_status':
                result=portfolio(db,session_id=session.id)
                reply='持仓：'+('；'.join(f"{r['product_id']} {r['units_hundredths']/100:,.2f}份" for r in result['positions']) or '暂无持仓')+'。\n订单：'+('；'.join(f"{r['product_id']} {'申购' if r['kind']=='buy' else '赎回'} {r['amount_minor']/100:,.2f}元，{ {'settled':'已结算','pending_confirmation':'待确认，未办理'}.get(r['status'],r['status'])}" for r in result['orders']) or '暂无订单')+'。以“模拟理财”区域的持久订单、持仓及账户余额为准。'
                _record_step(db,task,'read_wealth',{},result);_say(db,task,reply,'completed')
            else:
                latest['wealth_intent']=intent;latest['wealth_slots']=slots;task.context_json=_dump(latest)
                field='amount_yuan' if intent=='wealth_buy' else 'units'
                if not all(slots.get(key) for key in ('product','account',field)):
                    _say(db,task,'请补充产品编号、'+('申购金额（元）' if intent=='wealth_buy' else '赎回份额（份）')+'及付款/到账账户。','gathering_info')
                else:
                    accounts=db.scalars(select(Account).where(Account.owner_id==session.owner_id)).all()
                    matched=[a for a in accounts if slots['account'] in {a.alias,a.id,'尾号'+a.account_last4,a.account_last4}]
                    if len(matched)!=1:raise DomainError('ACCOUNT_UNRESOLVED','请明确本人账户名称或尾号，不能猜测账户')
                    kwargs={'amount_minor':decimal_hundredths(slots[field],'金额')} if intent=='wealth_buy' else {'units_hundredths':decimal_hundredths(slots[field],'份额')}
                    order=prepare_order(db,session_id=session.id,kind='buy' if intent=='wealth_buy' else 'redeem',product_id=slots['product'].upper(),account_id=matched[0].id,agent_task_id=task.id,**kwargs)
                    latest['wealth_order_id']=order['id'];task.context_json=_dump(latest)
                    _record_step(db,task,'prepare_wealth',{'product_id':order['product_id'],'account_id':order['account_id'],'amount_minor':order['amount_minor'],'units_hundredths':order['units_hundredths']},{'order':order})
                    step=db.scalar(select(AgentStep).where(AgentStep.task_id==task.id).order_by(AgentStep.ordinal.desc()))
                    step.result_ref=order['id'];step.status='awaiting_confirmation'
                    _say(db,task,f"已准备{order['product_id']} {'申购' if intent=='wealth_buy' else '赎回'}固定订单：{order['amount_minor']/100:,.2f}元、{order['units_hundredths']/100:,.2f}份，手续费{order['summary']['fee_minor']/100:.2f}元；尚未受理、未结算、未改变余额和持仓。请核对下方订单并点击“确认理财交易”。聊天确认不授权。",'awaiting_confirmation')
            task.intent=intent;commit(db);return view(db,task)
        except DomainError as error:
            task.intent=intent;_say(db,task,error.message+'；未办理新交易，不产生额外扣款、份额变动或成功回执。','gathering_info')
            return view(db,task)

def cancel_wealth(db,session,task):
    from app.agent.service import _say,view
    context=json.loads(task.context_json)
    if not context.get('wealth_order_id'):return None
    row=cancel_order(db,session_id=session.id,order_id=context['wealth_order_id'])
    _say(db,task,'理财交易已结算，实际结果保留。' if row['status']=='settled' else '理财订单已取消或失效，未办理交易。','completed' if row['status']=='settled' else 'cancelled')
    return view(db,task)
