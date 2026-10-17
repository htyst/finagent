"""Birthday semantics prepare fixed terms; only the dedicated page grants authority."""
from __future__ import annotations
import json
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import select
from app.data.atomic import atomic_write, commit
from app.data.models import Account, AgentStep
from app.domain.errors import DomainError
from app.domain.money import yuan_to_minor
from app.domain.operations import _session, _now

INTENTS = {kind: 'birthday_'+kind for kind in ('create','change','cancel','cancel_item','retry','status')}
BUTTONS = {'create':'确认生日安排','change':'确认修改生日安排','cancel':'确认取消生日安排',
           'cancel_item':'确认取消蛋糕部分','retry':'确认重试蛋糕'}
ORIGINAL = '看看我爱人本月什么时候生日，帮我在当月锁定1000元活期资金，生日前两天订鲜花和蛋糕，生日当天送到'

def detect(text, context=None, task=None):
    context=context or {}
    from app.agent.clarification import explicit_transfer,procurement_request
    if explicit_transfer(text) and not procurement_request(text):return None
    birthday=bool(re.search(r'生日|鲜花|蛋糕',text))
    if birthday and re.search(r'是否.{0,8}(?:取消|解除)|有没有.{0,8}订单|(?:已|已经)(?:取消|解除).{0,8}吗|怎么取消|如何取消|能否取消|可以取消|能不能取消',text):
        return 'status'
    if birthday and re.search(r'取消|解除|停止|撤销',text):
        if re.search(r'整个|全部|所有|生日安排|生日计划|生日任务',text) and not re.search(r'只取消|取消蛋糕部分',text):
            return 'cancel'
        if re.search(r'蛋糕',text):return 'cancel_item'
    if birthday and re.search(r'重试|重新下单|再试',text):return 'retry'
    if re.search(r'预算.*(?:改成|改为|调整|改到)|只买|不买蛋糕',text) and (birthday or context.get('birthday_plan_id')):
        return 'change'
    if birthday and re.search(r'完成了吗|哪部分完成|进度|状态|一共用了|还剩|怎么办|查询|查看',text):
        return 'status'
    if '生日' in text and re.search(r'帮我|安排|规划|锁定|预留|订|买|生日是|生日为',text):
        return 'create'
    if context.get('birthday_kind') == 'create' and task is not None and task.status in ('gathering_info','awaiting_confirmation'):
        if re.search(r'余额|查流水|查账单|查询账户|转账|转给|给.{0,60}转\s*\d|申购|赎回|冻结|解冻|挂失|限额|代扣|自动续费',text):return None
        if re.search(r'生日|收货|地址|付款|账户|报价|运费|余款|预算|下单|花店|蛋糕店',text):
            return 'create'
    if birthday and re.search(r'生日任务|生日安排|生日计划|鲜花|蛋糕',text):return 'status'
    return None

def _extract(db, task, text):
    from app.domain.birthdays import birthday_now
    values={}
    errors=[]
    reference=birthday_now(db).astimezone(ZoneInfo('Asia/Shanghai')).date()
    if re.search(r'转账|转给|申购|赎回|冻结|解冻|挂失|单笔.{0,8}限额|申请.{0,8}卡|代扣|自动续费|AA收款',text,re.I):
        errors.append('本条同时包含其他银行办理，请分开提出，不能省略其他操作只准备生日安排。')
    matches=re.findall(r'生日(?:\s*(?:是|为|在))?\s*(?:(\d{4})[年-])?(\d{1,2})[月-](\d{1,2})(?:日|号)?',text)
    matches+=re.findall(r'(?:(\d{4})[年-])?(\d{1,2})[月-](\d{1,2})(?:日|号)?[^，,。；;\n0-9]{0,24}(?:的生日|过生日|生日)',text)
    dates=set()
    for year,month,day in matches:
        try:dates.add(date(int(year) if year else reference.year,int(month),int(day)).isoformat())
        except ValueError:errors.append('生日日期不合法，请核对年月日。')
    relative=re.findall(r'(大后天|后天|明天|今天)(?:\s*生日|\s*是[^，,。；;\n]{0,24}生日|[^，,。；;\n]{0,24}(?:的生日|过生日))',text)
    relative+=re.findall(r'生日(?:是|在|为)?\s*(大后天|后天|明天|今天)',text)
    for day in relative:dates.add((reference+timedelta(days={'今天':0,'明天':1,'后天':2,'大后天':3}[day])).isoformat())
    if len(dates)>1:errors.append('出现多个生日日期，请明确本次唯一生日。')
    elif dates:values['birthday_date']=next(iter(dates));values['birthday_source']='user_provided'
    money=re.findall(r'(?:预算(?:共|是|为|改成|改为|改到)?|锁定|预留)\s*(\d+(?:\.\d{1,2})?)\s*元',text)
    if len(set(money))>1:errors.append('预算金额有冲突，请明确最终总预算。')
    elif money:values['budget_minor']=yuan_to_minor(money[-1])
    accounts=list(db.scalars(select(Account).where(Account.owner_id==task.owner_id)))
    names=[a for a in accounts if a.alias in text]
    if len(names)>1:errors.append('一次生日安排只能指定一个付款账户，请明确账户。')
    elif names:values['source_account_id']=names[0].id;values['source_reference']=names[0].alias
    else:
        source=re.search(r'(?:付款账户|扣款账户)(?:是|为|[:：])?\s*([^，,。；;]{1,80})',text)
        if source:
            from app.agent.forms import payment_account
            ref=source.group(1).strip();account=payment_account(db,task.owner_id,ref)
            values['source_reference']=ref;values['source_account_id']=account.id if account else None
    who=re.search(r'(?:收货人|收件人|收货对象)(?:是|为|[:：])?\s*([^，,。；;]{1,60})',text)
    if who:values['recipient']=who.group(1).strip()
    address=re.search(r'(?:收货地址|地址)(?:是|为|[:：])?\s*([^，,。；;]{1,200})',text)
    if address:values['address']=address.group(1).strip()
    if re.search(r'只买.*鲜花|只(?:要|订).*鲜花|不买蛋糕|不要蛋糕',text):
        values['item_keys']=['flowers']
    elif ('鲜花' in text or '花店' in text) and ('蛋糕' in text or '蛋糕店' in text):
        values['item_keys']=['flowers','cake']
    elif '鲜花' in text or '花店' in text:values['item_keys']=['flowers']
    elif '蛋糕' in text or '蛋糕店' in text:values['item_keys']=['cake']
    quotes={}
    for item,words in [('flowers','花店|鲜花'),('cake','蛋糕店|蛋糕')]:
        amounts=re.findall(r'(?:'+words+r')[^，,。；;元]{0,12}?(\d+(?:\.\d{1,2})?)\s*元',text)
        amounts+=re.findall(r'(\d+(?:\.\d{1,2})?)\s*元的?(?:'+words+r')',text)
        if len(set(amounts))>1:errors.append('同一商品报价有冲突，请核对固定报价。')
        elif amounts:quotes[item]=yuan_to_minor(amounts[0])
    if quotes:values['quoted_amounts']=quotes
    suppliers={}
    for item,label,suffix in [('flowers','鲜花','花店'),('cake','蛋糕','蛋糕店')]:
        # Labelled names also allow shops without a 花店/蛋糕店 suffix.
        labelled=re.findall(label+r'供应商\s*[:：]\s*([^，,。；;\n]{1,80}?)(?=\s*(?:固定报价|报价)|[，,。；;\n]|$)',text)
        named=re.findall(r'([^，,。；;\s]{0,40}'+suffix+r')',text)
        names=[name.strip().strip('“”"') for name in labelled] if labelled else named
        names=[re.sub(r'^(?:供应商[:：]?|选择|使用|由|在)', '',name) for name in names]
        names=['模拟'+suffix if name==suffix else name for name in names]
        if len(set(names))>1:errors.append('同一商品供应商有冲突，请明确本次选定的店铺。')
        elif names:suppliers[item]=names[0]
    if suppliers:values['supplier_names']=suppliers
    if re.search(r'无运费|免运费|运费.*(?:0元|零元)',text):values['no_shipping']=True
    if re.search(r'运费\s*(?:是|为|共|[:：])?\s*[1-9]\d*(?:\.\d{1,2})?\s*元',text):
        errors.append('固定目录报价无运费，不能丢弃你指定的额外费用。')
    if re.search(r'(?:完成后|完成时|办完后).{0,8}释放|余款.{0,8}释放',text):
        values['release_remainder']=True
    clock=re.search(r'下单|订购|订鲜花',text)
    if clock:
        times={(int(hour),int(minute or 0)) for hour,minute in re.findall(r'(\d{1,2})(?:点|时|[:：])(?:(\d{1,2})(?:分)?)?',text)}
        if re.search(r'下午|晚上|中午|点半|时半',text) or any(point!=(9,0) for point in times):
            errors.append('当前采购时点为生日前两天北京时间09:00，不能丢弃其他钟点或分钟。')
        elif times=={(9,0)}:
            values['order_hour']=9;values['order_minute']=0
    if re.search(r'生日前(?!两天|2天)(?:\d+|[一二三四五六七八九十]+)天',text):
        errors.append('当前支持生日前两天采购，不能丢弃你指定的其他提前天数。')
    for key,verb in [('explicit_order_date','下单|订购'),('explicit_delivery_date','送达|配送')]:
        stated=set()
        date_pattern=r'(?:(\d{4})[年-])?(\d{1,2})[月-](\d{1,2})(?:日|号)?'
        mentioned=re.findall(date_pattern+r'[^，,。；;\n]{0,18}?(?:'+verb+r')',text)
        mentioned+=re.findall(r'(?:'+verb+r')[^，,。；;\n]{0,12}?'+date_pattern,text)
        for year,month,day in mentioned:
            try:stated.add(date(int(year) if year else reference.year,int(month),int(day)).isoformat())
            except ValueError:errors.append('下单或送达日期不合法，请核对年月日。')
        if len(stated)>1:errors.append('下单或送达日期有冲突，请明确唯一时点。')
        elif stated:values[key]=next(iter(stated))
    if re.search(r'美元|欧元|纽约|东京|UTC|GMT|Asia/(?!Shanghai)|America/|Europe/',text,re.I):
        errors.append('生日安排只支持人民币及北京时间，当前没有办理。')
    if re.search(r'如果|假设|举例|只是咨询|不要(?:安排|预留|锁定|取消|释放)|不取消|别取消|不释放|别释放|继续锁定|继续预留',text):
        errors.append('当前表述含条件、咨询或否定，请明确本次实际办理需求。')
    return values,errors

def _plan_for(portfolio, context):
    plans=portfolio.get('plans',[])
    reference=context.get('birthday_plan_id') or context.get('last_birthday_plan_id')
    matched=[p for p in plans if p['id']==reference] if reference else plans
    if len(matched)==1:return matched[0]
    return None


def _missing_create_fields(values):
    missing=[label for key,label in [('budget_minor','总预算'),('birthday_date','爱人的生日（资料来源由你提供）'),('source_account_id','付款账户'),('recipient','收货人'),('address','收货地址'),('item_keys','商品'),('order_hour','生日前两天北京时间09:00下单时点'),('release_remainder','任务完成后释放余款约定'),('no_shipping','报价是否含运费')] if not values.get(key)]
    items=values.get('item_keys',[])
    if not all(item in values.get('supplier_names',{}) and item in values.get('quoted_amounts',{}) for item in items):
        missing.append('所选供应商及固定商品报价')
    return missing

def summary(plan, orders):
    plan_states={'scheduled':'等待采购','ordering':'正在采购','awaiting_delivery':'等待送达回执',
                 'partial_failure':'部分采购失败','completed':'全部完成','completed_with_changes':'调整商品后完成',
                 'cancelled':'已取消','blocked':'采购受阻'}
    lines=[f"生日任务状态：{plan_states.get(plan['status'],plan['status'])}；预算{plan['budget_minor']/100:.2f}元，实际花费{plan['spent_minor']/100:.2f}元。",
           f"当前预算未使用{(plan['budget_minor']-plan['spent_minor'])/100:.2f}元；当前专款{plan['reserved_minor']/100:.2f}元，累计释放{plan['released_minor']/100:.2f}元。"]
    labels={'flowers':'鲜花','cake':'蛋糕'}
    states={'accepted':'已受理，等待送达回执','delivered':'已有独立送达回执','rejected':'下单失败或供应商拒单','cancelled':'该部分已取消'}
    for order in orders:
        lines.append(f"{labels.get(order['item_key'],order['item_key'])}：{states.get(order['status'],order['status'])}，"
                     f"{'已支付' if order['payment_status']=='paid' else '未支付'}{order['amount_minor']/100:.2f}元。")
    if plan['status']=='partial_failure':
        lines.append('已成功的鲜花订单保留，不重复购买。蛋糕可以重试或取消蛋糕部分；请明确选择，再核对新的固定办理单。')
    if plan['status'] in ('scheduled','awaiting_delivery','partial_failure'):
        lines.append('任务尚未全部完成；到生日或已付款不等于已送达，余款在有效订单有送达回执后释放。')
    return '\n'.join(lines)

def _invalidate_other(db,session,task):
    from app.domain.operations import invalidate_agent_proposal
    from app.domain.subscriptions import invalidate_task as invalidate_subscription
    from app.domain.card_management import invalidate_task as invalidate_card
    from app.domain.wealth import invalidate_task_order
    invalidate_subscription(db,task);invalidate_card(db,task);invalidate_task_order(db,task)
    if task.proposal_id:
        invalidate_agent_proposal(db,session_id=session.id,proposal_id=task.proposal_id);task.proposal_id=None

def dispatch(db,session,task,text, *, details=None, decision_hint=None):
    from app.agent.service import _task,_message,_say,view,_dump,_record_step,MAX_STEPS
    from app.domain.birthdays import get_portfolio,prepare,invalidate_task,task_state
    ctx=json.loads(task.context_json)
    kind="create" if details is not None else next((key for key,value in INTENTS.items() if value==decision_hint.intent),None) if decision_hint is not None else detect(text,ctx,task)
    pending=task_state(db,task).get('birthday_request')
    if kind is None and pending and pending['status']=='pending_confirmation' and re.fullmatch(r'(?:确认|好的|同意|执行)[。！!\s]*',text):
        _message(db,task,'user',text,already_recorded=decision_hint is not None);_say(db,task,'请在下方对话中的固定办理单点击“核对并继续”；聊天回复不授权，资金和订单尚未因本条消息改变。','awaiting_confirmation');return view(db,task)
    if kind is None:return None
    if task.stop_requested:return view(db,task)
    sid,tid=session.id,task.id
    portfolio=get_portfolio(db,sid)
    selected=_plan_for(portfolio,ctx)
    if kind=='status':
        with atomic_write(db):
            session,_=_session(db,sid,_now());task=_task(db,session,tid);_message(db,task,'user',text,already_recorded=decision_hint is not None)
            task.intent=INTENTS['status']
            if not selected:
                _say(db,task,'请明确要查询的生日任务编号；当前没有唯一可核对的生日计划，未重新执行业务。','gathering_info')
            else:
                fresh=get_portfolio(db,sid);selected=next(p for p in fresh['plans'] if p['id']==selected['id'])
                latest=json.loads(task.context_json);latest['birthday_plan_id']=selected['id'];task.context_json=_dump(latest)
                _record_step(db,task,'read_birthday',{'plan_id':selected['id']},{'birthday_plan':selected})
                _say(db,task,summary(selected,[o for o in fresh['orders'] if o['plan_id']==selected['id']]),'awaiting_confirmation' if pending and pending['status']=='pending_confirmation' else 'completed')
            return view(db,task)
    if kind!='create' and selected is None:
        _message(db,task,'user',text,already_recorded=decision_hint is not None);_say(db,task,'请明确已有生日计划编号；当前没有唯一可变更的计划，未预留或改变订单。','gathering_info');return view(db,task)
    if details is None:explicit,errors=_extract(db,task,text)
    else:
        from app.agent.forms import payment_account
        account=payment_account(db,task.owner_id,details['source_reference'])
        explicit={'source_reference':details['source_reference'],'source_account_id':account.id if account else None,
                  'recipient':details['recipient'],'address':details['address']};errors=[]
    if kind in ('cancel','cancel_item','retry') and set(explicit)-{'item_keys','release_remainder'}:
        errors.append('取消或重试不能同时修改生日、付款账户、报价或收货资料，请分开明确办理；原计划未改变。')
    if kind in ('cancel_item','retry'):
        verbs='取消|解除|停止|撤销' if kind=='cancel_item' else '重试|重新下单|再试'
        clause=r'[^，,。；;！？?\n]{0,16}'
        cake_action=re.search(r'(?:'+verbs+r')'+clause+r'蛋糕|蛋糕'+clause+r'(?:'+verbs+r')',text)
        flowers_action=re.search(r'(?:'+verbs+r')'+clause+r'(?:鲜花|花店)|(?:鲜花|花店)'+clause+r'(?:'+verbs+r')',text)
        if not cake_action or flowers_action:
            errors.append('当前部分处理只支持失败蛋糕，请明确仅取消或重试蛋糕；不能替换你指定的鲜花或多个处理对象。')
        explicit={'item_key':'cake'}
    if kind=='cancel':explicit={}
    values=dict(ctx.get('birthday_slots',{})) if ctx.get('birthday_kind')==kind and kind=='create' else {}
    for key,value in explicit.items():
        if key in ('quoted_amounts','supplier_names'):values[key]={**values.get(key,{}),**value}
        else:values[key]=value
    if kind=='change' and set(explicit)-{'budget_minor','item_keys','quoted_amounts','supplier_names','release_remainder','no_shipping'}:
        errors.append('当前修改办理单只支持预算和商品变更；生日、账户、收货资料或时点变更请另行明确，原计划未改变。')
    if kind=='create' and values.get('birthday_date'):
        birthday=date.fromisoformat(values['birthday_date'])
        for key,expected in [('explicit_order_date',(birthday-timedelta(days=2)).isoformat()),('explicit_delivery_date',birthday.isoformat())]:
            if values.get(key) and values[key]!=expected:
                errors.append('明确下单或送达日期与生日前两天下单、生日当天送达不一致，请核对；未办理。')
    _message(db,task,'user',text,already_recorded=decision_hint is not None)
    version=ctx.get('birthday_version',0)+1
    ctx.update(birthday_version=version,birthday_kind=kind,birthday_slots=values)
    if details is not None:ctx['birthday_structured_fields']=True
    task.intent=INTENTS[kind];task.context_json=_dump(ctx);commit(db)
    semantic_question=None
    interpretation_failed=False
    if task.mode=='live' and not errors and details is None:
        from app.agent.language import interpret,payload_for
        payload=payload_for(db,task,view(db,task));commit(db)
        try:
            decision=decision_hint if decision_hint is not None else interpret(payload)
            if decision.intent not in (INTENTS[kind],'clarify'):
                semantic_question='你是要继续这份生日安排，还是改为转账或预算规划？请明确选择，我会保留你已提供的生日资料，当前没有办理。'
            elif decision.intent=='clarify' and decision.clarification_kind=='intent':
                semantic_question='请明确你要继续生日安排，还是转账准备资金或只规划预算；当前没有执行。'
            allowed={'birthday_date':values.get('birthday_date'),'birthday_recipient':values.get('recipient'),
                     'birthday_address':values.get('address'),'budget_yuan':values.get('budget_minor'),
                     'source':values.get('source_reference'),'birthday_items':values.get('item_keys') or (['cake'] if kind in ('cancel_item','retry') else None)}
            for key,expected in allowed.items():
                proposed=getattr(decision,key,None)
                # Literal form values remain authoritative. A later model response
                # cannot rewrite fields that were not supplied in this chat turn.
                slot={'source':'source_reference','birthday_recipient':'recipient','birthday_address':'address'}.get(key)
                if ctx.get('birthday_structured_fields') and slot and slot not in explicit:continue
                if proposed is None:continue
                matches=(yuan_to_minor(proposed)==values.get('budget_minor')) if key=='budget_yuan' else (set(proposed)==set(expected) if key=='birthday_items' and expected else proposed==expected)
                if key=='source' and expected is not None:
                    from app.agent.forms import payment_account
                    resolved=payment_account(db,task.owner_id,proposed)
                    matches=(resolved is not None and resolved.id==values.get('source_account_id')) or proposed.strip().removeprefix('账户').removeprefix('尾号')==expected.strip().removeprefix('账户').removeprefix('尾号')
                if expected is None or not matches:
                    raise DomainError('MODEL_SEMANTIC_MISMATCH','模型提取与本次已明确资料不一致，或补入未提供信息；请核对生日、预算和收货资料，未办理。')
            for key,expected in [('time_zone','Asia/Shanghai'),('hour',values.get('order_hour')),('minute',values.get('order_minute')),('days_before',2)]:
                proposed=getattr(decision,key,None)
                if proposed is not None and proposed!=expected:
                    raise DomainError('MODEL_SEMANTIC_MISMATCH','模型提取的采购时点或时区与固定生日安排不一致，未办理。')
            unrelated=('target','target_bank','target_account','target_note','amount_yuan','card','action','applicant','product','units','merchant','schedule_text','participants','payer','collection_account','shares_yuan')
            if any(getattr(decision,key,None) is not None for key in unrelated):
                raise DomainError('MODEL_SEMANTIC_MISMATCH','模型混入其他业务参数，请分开办理；未准备生日安排。')
        except DomainError as error:
            if error.code=='MODEL_OUTPUT_MISMATCH':interpretation_failed=True
            else:errors.append(error.message)
    with atomic_write(db):
        session,_=_session(db,sid,_now());task=_task(db,session,tid);latest=json.loads(task.context_json)
        if task.stop_requested or latest.get('birthday_version')!=version:return view(db,task)
        if db.query(AgentStep).filter(AgentStep.task_id==tid).count()>=MAX_STEPS:
            _say(db,task,'本次步骤已达上限，请另开对话查询既有计划，不会重复执行采购。','gathering_info');return view(db,task)
        if semantic_question:
            from app.agent.clarification import _ask
            return _ask(db,session,task,text,facts=values,candidates=[INTENTS[kind],'transfer','budget'])
        if interpretation_failed:
            # Format failure supplies no trusted semantics or authority. Local,
            # validated facts may still identify the exact missing information.
            invalidate_task(db,task)
            missing=_missing_create_fields(values) if kind=='create' else []
            question=('请补充'+'、'.join(missing)+'。' if missing else
                      '已保留你明确提供的资料，请再次明确本次生日办理需求，以便核对固定条款。')
            _say(db,task,question+'本轮理解结果尚未通过校验，未准备或执行生日安排。','gathering_info')
            return view(db,task)
        if errors:
            if not re.search(r'不要|不取消|别取消|假设|举例|只是咨询',text):
                invalidate_task(db,task)
            _say(db,task,'；'.join(errors),'gathering_info');return view(db,task)
        if kind=='create':
            if values.get('birthday_date'):
                from app.domain.birthdays import birthday_now
                appointment=datetime.combine(date.fromisoformat(values['birthday_date'])-timedelta(days=2),time(9),ZoneInfo('Asia/Shanghai'))
                if appointment<=birthday_now(db):
                    invalidate_task(db,task)
                    latest['clarification']={'scope':'gift_funding','facts':values,'candidates':['transfer','budget'],'source_text':text}
                    task.context_json=_dump(latest)
                    _say(db,task,f"你已明确生日为{values['birthday_date']}。当前鲜花蛋糕自动采购须在生日前两天北京时间09:00下单，这个预约时点已经过去，不能假装按时下单。你希望改为转账准备礼物，还是先规划预算？当前没有锁定或扣款。",'gathering_info')
                    return view(db,task)
            missing=_missing_create_fields(values)
            if missing:
                _say(db,task,'尚未预留资金或下单。请补充'+'、'.join(missing)+'。我没有主动读取生日资料，不能推测生日。','gathering_info');return view(db,task)
        if kind=='change' and ('budget_minor' not in values or 'item_keys' not in values):
            _say(db,task,'请明确新的总预算和最终保留的商品；尚未改变原计划。','gathering_info');return view(db,task)
        _invalidate_other(db,session,task)
        terms={key:value for key,value in values.items() if key not in ('source_reference','no_shipping','explicit_order_date','explicit_delivery_date')}
        if kind=='change':
            terms={key:value for key,value in terms.items() if key in ('budget_minor','item_keys','quoted_amounts','supplier_names')}
            for key in ('quoted_amounts','supplier_names'):
                if key in terms:terms[key]={item:value for item,value in terms[key].items() if item in terms['item_keys']}
        try:
            result=prepare(db,session_id=sid,kind=kind,terms=terms,plan_id=selected['id'] if kind!='create' else None,agent_task_id=tid)
            latest['birthday_request_id']=result['id']
            if selected and kind!='create':latest['birthday_plan_id']=selected['id']
            task.context_json=_dump(latest)
            _record_step(db,task,'prepare_birthday',{'kind':kind},{'birthday_request':result})
            step=db.scalar(select(AgentStep).where(AgentStep.task_id==tid).order_by(AgentStep.ordinal.desc()))
            step.result_ref=result['id'];step.status='awaiting_confirmation'
            fixed=result['summary']
            detail=(f"生日{fixed.get('birthday_date')}；{fixed.get('order_at_utc')}（UTC，北京时间+8小时）下单；{fixed.get('delivery_date')}送达；"
                    f"预算{fixed.get('budget_minor',0)/100:.2f}元，付款{fixed.get('account_alias','账户A')}，收货{fixed.get('recipient','')}，"
                    f"任务完成后释放余款。")
            _say(db,task,'已准备固定生日办理单：'+detail+f"请核对下方对话中的固定办理单并点击“核对并继续”。本次聊天没有授权或执行；已有已授权计划和订单以真实记录为准。",'awaiting_confirmation')
        except DomainError as error:_say(db,task,error.message+'；未办理本次变更。','gathering_info')
        return view(db,task)

def cancel_birthday(db,session,task):
    from app.agent.service import _say,view,_dump
    from app.domain.birthdays import task_state,cancel
    current=task_state(db,task).get('birthday_request')
    if not task.intent or not task.intent.startswith('birthday_'):
        if not current or current['status']!='pending_confirmation':return None
    if current and current['status']=='pending_confirmation':
        cancel(db,session_id=session.id,request_id=current['id'])
    task.stop_requested=True
    context=json.loads(task.context_json);context['birthday_version']=context.get('birthday_version',0)+1
    task.context_json=_dump(context)
    _say(db,task,'当前对话及未授权生日办理单已停止。已授权后台计划和已付款订单保留；如需解除预留，请明确“取消整个生日安排”并核对专用办理单。','cancelled')
    return view(db,task)
