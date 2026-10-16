"""Durable topological plan runner. Each write pauses for existing page consent."""
import json
import re
from datetime import timedelta
from decimal import Decimal
from time import perf_counter
from uuid import uuid4
from sqlalchemy import select
from app.clock import now, business_day
from app.data.atomic import atomic_write, commit
from app.data.models import Account, Recipient, Card, AgentTask, AgentStep, AgentMessage, Proposal, DemoSetting, Task
from app.agent.registry import execute, validate, DESCRIPTIONS
from app.agent.guardrails import has_condition, money_minor, normalize_money, EXTREME_MARKERS
from app.domain.clock import previous_full_month_range, shifted_month
from app.domain.errors import DomainError
from app.planning.schema import Plan, PlanInvalid, validate_asset, violation
from app.planning.provider import generate
from app.verifier import verify, record, record_clarification
from app.planning import temporal

class Clarify(Exception):
    pass
def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',',':'))
def match(db, model, owner_id, ref):
    if not ref:
        raise Clarify('请补充付款账户、收款对象或卡片名称。')
    rows = db.scalars(select(model).where(model.owner_id == owner_id)).all()
    reference = ref.strip().removeprefix('尾号')
    matched = []
    for row in rows:
        name = getattr(row,'alias',None) or row.name
        tail = row.card_last4 if isinstance(row, Card) else row.account_last4
        if reference in {name,name.removesuffix('账户'),tail} or (name in reference and tail in reference):
            matched.append(row)
    if len(matched) != 1:
        choices = '、'.join((getattr(r,'alias',None) or r.name) + ' 尾号' +
                           (r.card_last4 if isinstance(r,Card) else r.account_last4) for r in matched)
        raise Clarify('对象不唯一或不存在，请补充名称和尾号。' + choices)
    return matched[0]
def period_args(expr, day):
    if expr is None:
        return {}
    if expr.type == 'period':
        if expr.period == 'this_month':
            start,end = day.replace(day=1),day
        elif expr.period == 'last_month':
            span = previous_full_month_range(day)
            start,end = span.start,span.end_exclusive-timedelta(days=1)
        else:
            start = end = day-timedelta(days=1) if expr.period == 'yesterday' else day
    elif expr.type == 'offset':
        start = end = day+timedelta(days=expr.n) if expr.unit == 'day' else shifted_month(day,expr.n)
    else:
        raise Clarify('周期时间只能用于周期转账，请明确查询范围。')
    return {'start':start.isoformat(),'end':end.isoformat()}

def compile_step(db, session, step, plan):
    p = step.params
    args = {}
    if step.tool in {'list_transactions','summarize_spending'}:
        args.update(period_args(p.time,business_day()))
        if p.category and p.category != '全部':
            args['category'] = p.category
        if p.account:
            args['account_id'] = match(db,Account,session.owner_id,p.account).id
        if p.card:
            args['card_id'] = match(db,Card,session.owner_id,p.card).id
        if step.tool == 'summarize_spending':
            args['comparison_mode'] = 'none'
    elif step.tool == 'get_task_status':
        row=db.scalar(select(Task).where(Task.id==p.task_id,Task.owner_id==session.owner_id,
                      Task.session_id==session.id,Task.dataset_version==session.dataset_version))
        if not row:
            raise Clarify('请指定当前会话中已有的操作任务，不能猜测任务编号。')
        args['task_id']=row.id
    elif step.tool == 'list_recipients':
        args['q'] = p.q
    elif step.tool == 'prepare_transfer':
        source = match(db,Account,session.owner_id,p.source)
        # Resolve own accounts first, then saved recipients; never trust a model ID.
        account_rows = db.scalars(select(Account).where(Account.owner_id == session.owner_id)).all()
        own = any(p.target and (p.target in {r.alias,r.alias.removesuffix('账户'),r.account_last4}
                  or (r.alias in p.target and r.account_last4 in p.target)) for r in account_rows)
        target = match(db,Account if own else Recipient,session.owner_id,p.target)
        if not p.amount_yuan:
            raise Clarify('请补充转账金额，例如 100 元。')
        args = {'source_account_id':source.id,
            'target':{'type':'own_account','account_id':target.id} if own else {'type':'saved_recipient','recipient_id':target.id},
            'amount_minor':money_minor(p.amount_yuan),'currency':'CNY'}
        if p.time:
            raise Clarify('暂不支持未来一次性转账，请明确是否现在准备操作单。')
        if step.condition:
            referenced = next(s for s in plan.steps if s.id == step.condition.step_ref)
            read_args = compile_step(db,session,referenced,plan)
            day = business_day()
            args['condition'] = {'type':'spending_gt' if step.condition.op == 'gt' else 'spending_lt',
                'start':read_args.get('start',day.replace(day=1).isoformat()),
                'end':read_args.get('end',day.isoformat()),'account_id':read_args.get('account_id'),
                'category':read_args.get('category') or '', 'threshold_minor':money_minor(step.condition.value)}
            if not args['condition']['category']:
                raise Clarify('请明确条件中的消费类别。')
    elif step.tool == 'prepare_card_action':
        if step.recurrence or plan.recurrence or step.condition:
            raise Clarify('当前周期与消费条件仅支持转账，请重新说明卡片操作。')
        args = {'card_id':match(db,Card,session.owner_id,p.card).id,'action':p.action}
    return validate(step.tool,args)

def semantic_check(db, session, plan, user_text, current_text=None, temporal_state=None):
    errors = []
    writes = [s for s in plan.steps if s.tool.startswith('prepare_')]
    current_text = current_text or user_text
    normalized = normalize_money(user_text)
    transfer_amounts = re.findall(r'转(?:账)?(?:给[^0-9，,。;；]*?)?\s*([0-9]+(?:\.[0-9]{1,2})?)', normalized)
    amount_revision = re.findall(r'(?:金额改成|金额改为|转账改成|转账改为)\s*([0-9]+(?:\.[0-9]{1,2})?)', normalize_money(current_text))
    if amount_revision:
        transfer_amounts = transfer_amounts[:-1] + amount_revision[-1:]
    expected_amounts = [Decimal(n) for n in transfer_amounts]
    transfer_steps = [s for s in writes if s.tool == 'prepare_transfer']
    if len(transfer_steps) == 1 and expected_amounts:
        expected_amounts = expected_amounts[-1:]
    if expected_amounts and len(expected_amounts) == len(transfer_steps):
        for s,expected in zip(transfer_steps, expected_amounts):
            if s.params.amount_yuan and Decimal(s.params.amount_yuan) != expected:
                errors.append(violation('P_AMOUNT_BINDING',f'steps.{s.id}.params.amount_yuan',s.params.amount_yuan,str(expected),'转账金额必须对应用户转账子句，不能取条件阈值或另一笔金额。'))
    numbers = set(re.findall(r'(?<![0-9.])([0-9]+(?:\.[0-9]{1,2})?)(?![0-9.])',normalize_money(user_text)))
    amounts = {Decimal(n) for n in numbers}
    if has_condition(user_text) and writes and not any(s.condition for s in writes):
        errors.append(violation('R1','condition',None,'完整条件','不得丢掉用户条件。'))
    if any(word in user_text for word in EXTREME_MARKERS) and not plan.clarification:
        errors.append(violation('P_EXTREME','clarification',None,'极值统计对象','先追问单笔极值或类别极值，不能输出普通总额。'))
    if '余额' in current_text and not any(s.tool == 'list_accounts' for s in plan.steps) and not plan.clarification:
        errors.append(violation('P_TASK_COVERAGE','steps','缺少余额查询','list_accounts','保留用户明确要求的余额查询任务。'))
    transfer_mentions = re.findall(r'转(?:账)?\s*[0-9]+',normalize_money(current_text))
    if len(transfer_mentions) > sum(s.tool == 'prepare_transfer' for s in writes) and not plan.clarification:
        errors.append(violation('P_TASK_COVERAGE','steps',len(writes),len(transfer_mentions),'不得丢掉多笔转账任务。'))
    for step in plan.steps:
        if step.tool in {'summarize_spending','list_transactions'}:
            period_words = re.findall(r'上个月|上月|这个月|本月|昨天|今天', current_text)
            expected_period = {'上个月':'last_month','上月':'last_month','这个月':'this_month',
                               '本月':'this_month','昨天':'yesterday','今天':'today'}.get(period_words[-1]) if period_words else None
            expr = step.params.time
            if expected_period and (not expr or expr.type != 'period' or expr.period != expected_period):
                errors.append(violation('P_TIME_GROUNDING',f'steps.{step.id}.params.time',expr.model_dump() if expr else None,
                                        expected_period,'保留用户明确的查询时间，不能默认其他范围。'))
            if any(word in current_text for word in ('去年','明年','上周','下周','昨晚')) and not plan.clarification:
                errors.append(violation('P_TIME_UNSUPPORTED',f'steps.{step.id}.params.time',current_text,
                                        '明确支持的结构化范围','该时间范围需要澄清，不能静默改为本月。'))
        allowed = {'list_accounts':set(),'list_cards':set(),'list_recipients':{'q'},
            'list_transactions':{'time','account','card','category'},'summarize_spending':{'time','account','category'},
            'prepare_transfer':{'source','target','amount_yuan'},'prepare_card_action':{'card','action'},
            'get_task_status':{'task_id'}}.get(step.tool,set())
        supplied = set(step.params.model_dump(exclude_none=True))
        if supplied - allowed:
            errors.append(violation('P_TOOL_PARAMS',f'steps.{step.id}.params',sorted(supplied-allowed),sorted(allowed),'参数不可忽略，按工具能力修复。'))
        if step.tool.startswith('prepare_'):
            for field in ('source','target','card'):
                ref = getattr(step.params,field)
                if ref:
                    tokens = re.findall(r'[0-9]{4}|[\u4e00-\u9fff]+',ref)
                    # A name plus tail is allowed only when both occur in the accepted dialogue.
                    if not tokens or any(t.removeprefix('尾号').removesuffix('账户') not in user_text for t in tokens if t != '尾号'):
                        errors.append(violation('P_GROUNDING',f'steps.{step.id}.params.{field}',ref,'用户明确提供的对象','不能从目录自行选择对象。'))
            if step.params.amount_yuan and Decimal(step.params.amount_yuan) not in amounts:
                errors.append(violation('P_GROUNDING',f'steps.{step.id}.params.amount_yuan',step.params.amount_yuan,
                                        '用户明确提供的金额','不能编造或计算最终转账金额。'))
            if step.condition and Decimal(step.condition.value) not in amounts:
                errors.append(violation('P_GROUNDING',f'steps.{step.id}.condition.value',step.condition.value,'用户阈值','保留原阈值。'))
        if step.condition:
            if ('不超过' in user_text or any(w in user_text for w in ('至少','至多','以上','以下','不足','不到','否则'))) and not plan.clarification:
                errors.append(violation('P_CONDITION_OPERATOR',f'steps.{step.id}.condition','不支持的边界/分支','明确gt或lt','不等号和否则分支不能默默替换为严格比较。'))
            directions = re.findall(r'不超过|超过|大于|低于|小于', user_text)
            direction = directions[-1] if directions else None
            if direction in {'小于','低于'} and step.condition.op != 'lt':
                errors.append(violation('P_CONDITION_OPERATOR',f'steps.{step.id}.condition.op',step.condition.op,'lt','保留比较方向。'))
            if direction in {'大于','超过'} and step.condition.op != 'gt':
                errors.append(violation('P_CONDITION_OPERATOR',f'steps.{step.id}.condition.op',step.condition.op,'gt','保留比较方向。'))
        recurrence = step.recurrence or (plan.recurrence if step.tool.startswith('prepare_') else None)
        if recurrence and temporal_state and {k:getattr(recurrence,k) for k in ('type','freq','day')}!=temporal_state['recurrence']:
            errors.append(violation('P_GROUNDING',f'steps.{step.id}.recurrence',recurrence.model_dump(),
                                    temporal_state['recurrence'],'日期必须由用户表达或后端确认记录支持。'))
    if errors:
        raise PlanInvalid(errors)

def payload_for(db,session,task,text):
    messages = db.scalars(select(AgentMessage).where(AgentMessage.task_id == task.id)
                         .order_by(AgentMessage.ordinal)).all()
    catalog = {}
    for label,model in [('accounts',Account),('recipients',Recipient),('cards',Card)]:
        rows = db.scalars(select(model).where(model.owner_id == session.owner_id)).all()
        catalog[label] = [{'name':getattr(r,'alias',None) or r.name,
            'tail':r.card_last4 if isinstance(r,Card) else r.account_last4,'status':r.status} for r in rows]
    start = json.loads(task.context_json).get('current_request_start', 1)
    active_messages = [r for r in messages if r.ordinal >= start]
    return {'clock_now':now().isoformat(),'timezone':'Asia/Shanghai','input':text,
            'active_dialogue':[{'ordinal':r.ordinal,'role':r.role,'content':r.content} for r in active_messages],
            'history':[{'role':r.role,'content':r.content} for r in messages[-16:]],
            'known_operation_tasks':[{'id':r.id,'kind':r.kind,'status':r.status} for r in db.scalars(select(Task).where(
                Task.owner_id==session.owner_id,Task.session_id==session.id,Task.dataset_version==session.dataset_version)).all()],
            'catalog':catalog,'pending_plan':json.loads(task.context_json).get('neuro_plan')}

def template_recurring(db,session,task,text,state):
    """Template translation only; R10 below owns all time questions/defaults."""
    from app.agent.service import _resolve
    from types import SimpleNamespace
    context=json.loads(task.context_json)
    slots=context.get('standing_slots',{}) if state['recurrence'] else {}
    cleaned=re.sub(r'每(?:个)?月\s*(?:[0-9一二三四五六七八九十]+\s*[号日])?','',text)
    cleaned=re.sub(r'(转(?:账)?\s*[0-9]+(?:\.[0-9]{1,2})?)(?![0-9.元])',r'\1元',cleaned)
    actor=SimpleNamespace(owner_id=task.owner_id,intent='transfer')
    name,args,question=_resolve(actor,cleaned,slots,db)
    if state['recurrence']:
        context['standing_slots']=slots
    task.context_json=dump(context)
    commit(db)
    if question:
        return validate_asset({'steps':[],'clarification':question})
    if name!='prepare_transfer':
        return validate_asset({'steps':[{'id':'read','tool':name}]})
    source=db.get(Account,args['source_account_id'])
    target_info=args['target']
    target=db.get(Account,target_info['account_id']) if target_info['type']=='own_account' else db.get(Recipient,target_info['recipient_id'])
    label=getattr(target,'alias',None) or target.name
    return validate_asset({'steps':[{'id':'accounts','tool':'list_accounts'},
        {'id':'transfer','tool':'prepare_transfer','depends_on':['accounts'],
         'params':{'source':source.alias,'target':label+' 尾号'+target.account_last4,
                   'amount_yuan':format(Decimal(args['amount_minor'])/100,'f')}}]})

def without_model_time_question(db,session,task,plan,text,state):
    if not temporal.is_time_question(plan.clarification):
        return plan
    plan.clarification=None
    if plan.steps:
        return plan
    # No executable asset was supplied. Reuse the grounded single transfer
    # template to ask only missing business slots, never a model time question.
    if has_condition(text):
        return validate_asset({'steps':[],'clarification':'请完整说明转账条件、对象和金额。'})
    replacement=template_recurring(db,session,task,text,state)
    temporal.normalize(replacement,state)
    return replacement


def send(db,session,task,text):
    from app.agent.service import _message, _say, _task, view
    from app.domain.operations import get_proposal, invalidate_agent_proposal
    if task.status in {'completed','cancelled','expired','failed','partially_completed'}:
        raise DomainError('INVALID_TASK_STATE','任务已结束，请创建新任务',409)
    if re.fullmatch(r'(?:我)?确认[。！!\s]*',text):
        _message(db,task,'user',text)
        _say(db,task,'聊天确认不会执行业务，请在页面核对操作单并点击确认。',task.status)
        return view(db,task)
    if task.proposal_id:
        proposal = get_proposal(db,session_id=session.id,proposal_id=task.proposal_id)
        if proposal['status'] == 'pending_confirmation' and re.fullmatch(r'(?:先)?(?:查一下|看看|查询)?(?:账户)?余额[？?。\s]*',text):
            _message(db,task,'user',text)
            result = execute(db,session_id=session.id,owner_id=session.owner_id,name='list_accounts',arguments={})
            from app.agent.service import _summary
            _say(db,task,_summary('list_accounts',result),'awaiting_confirmation')
            return view(db,task)
        if proposal['status'] == 'pending_confirmation':
            invalidate_agent_proposal(db,session_id=session.id,proposal_id=task.proposal_id)
            task = _task(db,session,task.id)
            task.proposal_id = None
            task.status = 'gathering_info'
    context = json.loads(task.context_json)
    if context.get('standing_order_id'):
        from app.standing import invalidate_pending
        invalidate_pending(db,session,context['standing_order_id'])
        context.pop('standing_order_id',None)
        task.context_json = dump(context)
    _message(db,task,'user',text)
    commit(db)
    payload = payload_for(db,session,task,text)
    revision = task.updated_at_utc
    session_id,task_id = session.id,task.id
    started = perf_counter()
    # Only unresolved dialogue is used for grounding. Completed writes never donate authorization.
    history_text = ' '.join(m['content'] for m in payload['active_dialogue'] if m['role']=='user')
    state=temporal.resolve(payload['active_dialogue'],context)
    task.context_json=dump(context)
    commit(db)
    corrections=[]
    def check_semantics(candidate):
        corrections.extend(temporal.normalize(candidate,state))
        semantic_check(db,session,candidate,history_text,text,state)
    try:
        if task.mode == 'mock':
            plan = template_recurring(db,session,task,text,state)
            corrections.extend(temporal.normalize(plan,state))
            repairs,history = 0,[]
        else:
            plan,repairs,history = generate(payload,check_semantics)
        parse_ms = (perf_counter()-started)*1000
        # Generation occurs outside any write lock. Revalidate revision and identity after network.
        from app.domain.operations import _session, _now
        with atomic_write(db):
            session,_ = _session(db,session_id,_now())
            task = _task(db,session,task_id)
            if task.updated_at_utc != revision or task.stop_requested:
                return view(db,task)
            context = json.loads(task.context_json)
            context.update(neuro_plan=plan.model_dump(),plan_source='model' if task.mode=='live' else 'template',
                           repair_rounds=repairs,repair_history=history,parse_ms=parse_ms,
                           emotion=plan.emotion.model_dump(),plan_input=text,verify_ms=0,plan_violations=[],model_error=None)
            task.context_json = dump(context)
            context['temporal_decision']={'intent':state,'corrections':corrections,
                'rules_checked':['R10'],'clarifications':[]}
            items=temporal.check(plan,state)
            if items:
                item=items[0]
                record_clarification(db,session.owner_id,items)
                context['temporal_decision']['clarifications']=[{'rule_id':i.rule_id,'field':i.field,
                    'observed':i.observed,'expected':i.expected,'message':i.message,
                    'suggested_value':i.suggested_value} for i in items]
                if item.suggested_value is not None:
                    context['temporal_pending']={'rule_id':'R10','anchor':state['anchor'],
                        'suggested_day':item.suggested_value,'message':item.message}
                context.pop('standing_day_question',None)
                task.context_json=dump(context)
                _say(db,task,item.message,'gathering_info')
                return view(db,task)
            context.pop('standing_day_question',None)
            context.pop('temporal_pending',None)
            task.context_json=dump(context)
            if state['unsupported']:
                _say(db,task,'该重复频率暂不支持，未生成操作单。','gathering_info')
                return view(db,task)
            plan=without_model_time_question(db,session,task,plan,history_text,state)
            context=json.loads(task.context_json)
            context['neuro_plan']=plan.model_dump()
            task.context_json=dump(context)
            if plan.clarification:
                _say(db,task,plan.clarification,'gathering_info')
                return view(db,task)
            compiled = {s.id:compile_step(db,session,s,plan) for s in plan.steps}
            task.plan_version += 1
            task.intent = 'neuro_plan'
            ordinal = {s.id:i for i,s in enumerate(plan.steps,1)}
            task.plan_json = dump([{'ordinal':i,'tool_name':s.tool,'depends_on':[ordinal[d] for d in s.depends_on],
                                   'on_failure':s.on_failure} for i,s in enumerate(plan.steps,1)])
            task.requested_outcomes_json = dump([DESCRIPTIONS[s.tool] for s in plan.steps])
            for i,step in enumerate(plan.steps,1):
                db.add(AgentStep(id=uuid4().hex,task_id=task.id,plan_version=task.plan_version,
                    ordinal=i,tool_name=step.tool,depends_on_json=dump([ordinal[d] for d in step.depends_on]),
                    on_failure=step.on_failure,args_json=dump(compiled[step.id]),result_json='{}',
                    status='pending',created_at_utc=now().isoformat()))
            task.status = 'running_readonly'
            commit(db)
            return run(db,session,task)
    except Clarify as error:
        task = _task(db,session,task_id)
        _say(db,task,str(error),'gathering_info')
        return view(db,task)
    except PlanInvalid as error:
        task = _task(db,session,task_id)
        context = json.loads(task.context_json)
        context.update(repair_rounds=2,plan_violations=error.violations,plan_source='fallback',model_error=None,
                       repair_history=getattr(error,'repair_history',[]),
                       parse_ms=(perf_counter()-started)*1000)
        task.context_json = dump(context)
        if any(v['rule_id']=='R1' for v in error.violations):
            result = verify('prepare_transfer',{}, {'input':history_text,'emotion':None})
            record(db,session.owner_id,None,'plan',result)
        _say(db,task,'计划经过两轮定向修复仍未通过校验，请补充或重新说明账户、金额、条件与任务顺序。','gathering_info')
        return view(db,task)
    except DomainError as error:
        task = _task(db,session,task_id)
        context = json.loads(task.context_json)
        context.update(plan_source='fallback',parse_ms=(perf_counter()-started)*1000,
                       repair_rounds=0,repair_history=[],plan_violations=[],verify_ms=0,
                       model_error=getattr(error,'provider_error',{'code':error.code,'message':error.message}))
        task.context_json = dump(context)
        # Transport failure may use the known whole-instruction crossflow template only.
        from app.agent.crossflow import detect,send as send_cross
        known = detect(text)
        if known and error.code == 'MODEL_UNAVAILABLE':
            task.mode = 'mock'
            task.intent = known
            commit(db)
            send_cross(db,session,task,text)
            _message(db,task,'assistant','计划模型不可用，已明确回退旧模板场景；条件仍由后端复核，写操作仍需页面确认。')
            commit(db)
        elif error.code == 'MODEL_UNAVAILABLE' and not has_condition(text) and not any(word in text for word in ('每月','然后','接着','并且','再转','再查')):
            from app.agent.service import _resolve,_intent,_record_step,_summary
            name,args,question = _resolve(task,text,context,db)
            task.intent = _intent(text,task.intent)
            task.context_json = dump(context)
            commit(db)
            if question or not name:
                _say(db,task,'计划模型不可用，已回退旧单场景模板。'+(question or '请补充需求。'),'gathering_info')
            else:
                try:
                    result=execute(db,session_id=session.id,owner_id=session.owner_id,name=name,
                                   arguments=args,agent_task_id=task.id if name.startswith('prepare_') else None)
                    _record_step(db,task,name,validate(name,args),result)
                    if isinstance(result,dict) and result.get('proposal'):
                        task.proposal_id=result['proposal']['id']
                    _say(db,task,'计划模型不可用，已回退旧单场景模板。'+_summary(name,result),
                         'awaiting_confirmation' if task.proposal_id else 'completed')
                except DomainError as fallback_error:
                    _say(db,task,'旧模板回退未完成：'+fallback_error.message,'gathering_info')
        else:
            _say(db,task,error.message+' 请处理接口问题后重试；本次未准备新操作单。','gathering_info')
        return view(db,task)

def run(db,session,task):
    from app.agent.service import _say,view,_summary
    context = json.loads(task.context_json)
    plan = Plan.model_validate(context['neuro_plan'])
    steps = db.scalars(select(AgentStep).where(AgentStep.task_id==task.id,
                      AgentStep.plan_version==task.plan_version).order_by(AgentStep.ordinal)).all()
    by_ordinal = {s.ordinal:s for s in steps}
    for row,step in zip(steps,plan.steps):
        if row.status in {'succeeded','skipped','failed','cancelled'}:
            continue
        if row.status == 'awaiting_confirmation':
            if not task.proposal_id:
                break
            proposal = db.get(Proposal,row.result_ref or task.proposal_id)
            if proposal.status == 'executed':
                row.status = 'succeeded'
                row.result_json = dump({'proposal_id':proposal.id,'status':'executed'})
                task.proposal_id = None
            else:
                break
            continue
        deps = [by_ordinal[i] for i in json.loads(row.depends_on_json)]
        if any(r.status != 'succeeded' for r in deps):
            row.status = 'skipped'
            row.result_json = dump({'message':'前置步骤未成功，已跳过依赖操作。'})
            continue
        args = json.loads(row.args_json)
        recurrence = step.recurrence or (plan.recurrence if step.tool.startswith('prepare_') else None)
        try:
            verify_started = perf_counter()
            verified = verify(step.tool,args,{'db':db,'owner_id':session.owner_id,
                'input':context.get('plan_input',''),'emotion':context.get('emotion'),
                'recurrence':recurrence.model_dump() if recurrence else None,
                'destination':'standing_orders' if recurrence else 'proposal'})
            record(db,session.owner_id,None,'plan_prepare',verified)
            context['last_verification'] = verified.as_dict()
            context['verify_ms'] = context.get('verify_ms',0) + (perf_counter()-verify_started)*1000
            task.context_json = dump(context)
            commit(db)
            if not verified.approved:
                raise DomainError('VERIFICATION_REJECTED',verified.violations[0].hint,409)
            if recurrence:
                from app.standing import prepare_order
                result = prepare_order(db,session,args,recurrence.model_dump())
                context['standing_order_id'] = result['id']
                task.context_json = dump(context)
                row.status = 'succeeded'
                row.result_json = dump(result)
                _say(db,task,result['confirmation']['summary'],'awaiting_confirmation')
                return view(db,task)
            row.status = 'running'
            commit(db)
            result = execute(db,session_id=session.id,owner_id=session.owner_id,name=step.tool,
                             arguments=args,agent_task_id=task.id if step.tool.startswith('prepare_') else None)
            row.result_json = dump(result)
            if step.tool.startswith('prepare_') and result.get('proposal'):
                row.status = 'awaiting_confirmation'
                row.result_ref = result['proposal']['id']
                task.proposal_id = result['proposal']['id']
                _say(db,task,_summary(step.tool,result),'awaiting_confirmation')
                return view(db,task)
            row.status = 'skipped' if isinstance(result,dict) and result.get('condition_met') is False else 'succeeded'
            _say(db,task,'条件未满足，已跳过写操作。' if row.status=='skipped' else _summary(step.tool,result),'running_readonly')
        except (DomainError,ValueError) as error:
            row.status = 'failed'
            row.result_json = dump({'message':str(error),'error_code':getattr(error,'code','INVALID_INPUT')})
            _say(db,task,'该步骤未完成：'+str(error),'gathering_info')
            if step.on_failure == 'stop':
                for other in steps:
                    if other.status == 'pending':
                        other.status = 'skipped'
                        other.result_json = dump({'message':'计划停止，未执行。'})
                break
    if not any(s.status == 'awaiting_confirmation' for s in steps):
        failed = any(s.status=='failed' for s in steps)
        _say(db,task,'计划已处理；失败与跳过原因见步骤。' if failed else '本次计划已处理完成。',
             'partially_completed' if failed else 'completed')
    commit(db)
    return view(db,task)

def resume_after_confirm(db,session,proposal_id):
    task = db.scalar(select(AgentTask).where(AgentTask.proposal_id==proposal_id,
                     AgentTask.owner_id==session.owner_id,AgentTask.session_id==session.id))
    if task and json.loads(task.context_json).get('neuro_plan'):
        task.status = 'running_readonly'
        commit(db)
        with atomic_write(db):
            return run(db,session,task)
