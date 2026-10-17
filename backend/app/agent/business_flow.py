"""Compile typed semantic requirements into bounded, owner-scoped business actions."""
from __future__ import annotations
import json
import re
from uuid import uuid4
from sqlalchemy import select
from app.data.atomic import atomic_write, commit
from app.data.models import Account, Recipient, AgentStep, AgentTask
from app.domain.errors import DomainError
from app.domain.operations import _session, _now, invalidate_agent_proposal
from app.agent.requirements import (detect_requirement_kind, extract_mock, from_understanding,
                                    validate_requirement)

BUSINESS_INTENTS = {'schedule_transfer', 'aa_collection'}


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def public_state(db, task):
    context = json.loads(task.context_json)
    result = {'schedule': None, 'collection': None,
              'semantic_validation': context.get('semantic_validation')}
    if context.get('schedule_id') and task.intent in {'schedule_transfer', 'composite'}:
        from app.domain.scheduled_transfers import get_schedule
        result['schedule'] = get_schedule(db, session_id=task.session_id, schedule_id=context['schedule_id'])
        task.status = {'pending_confirmation': 'awaiting_confirmation', 'executed': 'completed',
                       'expired': 'expired'}.get(result['schedule']['status'], result['schedule']['status'])
    if context.get('collection_id') and task.intent in {'aa_collection', 'composite'}:
        from app.domain.collections import get_collection
        result['collection'] = get_collection(db, session_id=task.session_id, collection_id=context['collection_id'])
        task.status = {'pending_confirmation': 'awaiting_confirmation', 'completed': 'completed',
                       'expired': 'expired'}.get(result['collection']['status'], result['collection']['status'])
    for kind in ('schedule', 'collection'):
        record = result[kind]
        if not record:
            continue
        for step in db.scalars(select(AgentStep).where(AgentStep.task_id == task.id,
                                                      AgentStep.result_ref == record['id'])).all():
            step.status = {'pending_confirmation': 'awaiting_confirmation', 'executed': 'succeeded',
                           'completed': 'succeeded'}.get(record['status'], record['status'])
            step.result_json = _dump({kind: record})
    return result


def cancel_business(db, session, task):
    from app.agent.service import _say, view
    context = json.loads(task.context_json)
    if context.get('schedule_id'):
        from app.domain.scheduled_transfers import cancel_schedule
        record = cancel_schedule(db, session_id=session.id, schedule_id=context['schedule_id'])
        executed = record['status'] == 'executed'
        if record['status'] not in {'cancelled', 'executed'}:
            _say(db, task, _status_reply({'schedule':record}), {'expired':'expired','failed':'failed'}.get(record['status'],task.status))
        else:
            _say(db, task, '预约已执行，结果保留，请查看回执。' if executed else '预约已取消，不会到点执行。',
                 'completed' if executed else 'cancelled')
        return view(db, task)
    if context.get('collection_id'):
        from app.domain.collections import cancel_collection, get_collection
        existing = get_collection(db, session_id=session.id, collection_id=context['collection_id'])
        if existing['status'] in {'completed','expired'}:
            _say(db,task,_status_reply({'collection':existing}), 'completed' if existing['status']=='completed' else 'expired')
            return view(db,task)
        record = cancel_collection(db, session_id=session.id, collection_id=context['collection_id'])
        _say(db, task, '收款请求已停止，已经到账的款项保留，未付款成员不会继续收取。',
             'completed' if record['status'] == 'completed' else 'cancelled')
        return view(db, task)
    return None


def _status_reply(state):
    if state.get('schedule'):
        row = state['schedule']
        labels = {'pending_confirmation':'待授权预约，尚未扣款', 'scheduled':'已授权，等待到点执行，尚未扣款',
                  'executed':'已执行', 'cancelled':'已取消，未执行', 'failed':'执行失败，未转账', 'expired':'未授权已过期'}
        return (f"预约时间：{row['execute_at_local']}（{row['timezone']}）；转账金额 "
                f"{row['summary']['amount_minor']/100:,.2f} 元；{labels.get(row['status'],row['status'])}。")
    if state.get('collection'):
        row = state['collection']
        return (f"AA收款状态：{ {'pending_confirmation':'待确认发起','collecting':'收款中','completed':'已收齐','cancelled':'已停止','expired':'草稿已过期'}.get(row['status'],row['status'])}；应收 {row['expected_minor']/100:,.2f} 元，"
                f"已收 {row['received_minor']/100:,.2f} 元。" + '；'.join(
                    f"{member['name']}：{'本人承担，无需付款' if member['status']=='self_share' else '已付' if member['status']=='paid' else '已取消' if member['status']=='cancelled' else '未付'} {member['share_minor']/100:,.2f} 元"
                    for member in row['members']))
    return '尚未准备完整请求，请补充下面列出的信息。'


def answer_referenced_status(db, session, task, text):
    """Read persisted business records from a prior chat; never inherit write slots."""
    from app.agent.service import _message, _say, view
    if task.intent in BUSINESS_INTENTS:
        return None
    if not re.search(r'状态|何时|什么时候|执行了吗|成功了吗|收到|谁.*付|已收|待收|是否.*(?:执行|收齐)|现在.*(?:怎样|如何)', text):
        return None
    if re.search(r'改|重新|发起|从.*转|给.*转', text):
        return None
    context = json.loads(task.context_json)
    explicit = 'collection' if re.search(r'AA|收款|谁.*付|已收|待收', text, re.I) else 'schedule' if re.search(r'预约|定时|到点|到时', text) else None
    refs = {kind: context.get(f'last_{kind}_task_id') for kind in ('schedule', 'collection')}
    kinds = [explicit] if explicit else [kind for kind, ref in refs.items() if ref]
    if not kinds:
        return None
    with atomic_write(db):
        session, _ = _session(db, session.id, _now())
        replies = []
        for kind in kinds:
            if not refs.get(kind):
                continue
            original = db.scalar(select(AgentTask).where(AgentTask.id == refs[kind],
                AgentTask.owner_id == session.owner_id, AgentTask.session_id == session.id,
                AgentTask.dataset_version == session.dataset_version))
            if original is not None:
                state = public_state(db, original)
                if state.get(kind):
                    replies.append(_status_reply(state))
        if not replies and not explicit:
            return None
        _message(db, task, 'user', text)
        _say(db, task, '\n'.join(replies) if replies else '当前会话没有对应的已保存请求，请在预约与 AA 收款列表中查看；尚未创建新请求。', 'completed')
        return view(db, task)


def _step(db, task, name, args, result, status, ref=None):
    from app.agent.service import _stamp
    current = db.scalars(select(AgentStep).where(AgentStep.task_id == task.id)).all()
    if len(current) >= 12:
        raise DomainError('TOOL_BUDGET_EXCEEDED','本次任务已达步骤上限，请取消后重新提出需求',409)
    db.add(AgentStep(id=uuid4().hex, task_id=task.id, ordinal=len(current)+1,
                    plan_version=task.plan_version, tool_name=name, args_json=_dump(args),
                    result_json=_dump(result), result_ref=ref, status=status, created_at_utc=_stamp()))


def _ground(db, task, requirement):
    from app.agent.language import _match
    from app.domain.recipient_directory import match_remark
    accounts = db.scalars(select(Account).where(Account.owner_id == task.owner_id)).all()
    recipients = db.scalars(select(Recipient).where(Recipient.owner_id == task.owner_id, Recipient.status != "one_time")).all()
    if requirement.kind == 'schedule_transfer':
        source = _match(requirement.source, accounts)
        own = [] if requirement.target_note else _match(requirement.target, accounts)
        other = match_remark(requirement.target_note, recipients) if requirement.target_note else _match(requirement.target, recipients)
        if requirement.target_note and requirement.target:
            tail = re.search(r'([0-9]{4})(?![0-9])',requirement.target)
            if tail:
                other = [row for row in other if row.account_last4==tail.group(1)]
        if len(source) != 1:
            raise DomainError('NEEDS_CLARIFICATION','请明确付款账户名称或尾号。')
        if len(own)+len(other) != 1:
            choices = own+other
            context=json.loads(task.context_json)
            context['schedule_target_choices']=[{'id':row.id,'tail':row.account_last4} for row in choices]
            task.context_json=_dump(context)
            detail = '；候选尾号：'+'、'.join(row.account_last4 for row in choices) if choices else ''
            raise DomainError('NEEDS_CLARIFICATION','请明确唯一收款对象或该备注对应的收款尾号'+detail+'。')
        return {'source_account_id':source[0].id,
                'target': {'type':'own_account','account_id':own[0].id} if own else {'type':'saved_recipient','recipient_id':other[0].id}}
    target = _match(requirement.collection_account, accounts)
    if len(target) != 1:
        raise DomainError('NEEDS_CLARIFICATION','请明确AA收款到账账户名称或尾号。')
    ids = []
    for member in requirement.participants:
        if member == '我':
            continue
        matches = _match(member, recipients)
        if len(matches) != 1:
            choices = '、'.join(row.account_last4 for row in matches)
            context=json.loads(task.context_json)
            context['aa_member_choices']={'label':member,'recipient_ids':[row.id for row in matches],'tails':[row.account_last4 for row in matches]}
            task.context_json=_dump(context)
            raise DomainError('NEEDS_CLARIFICATION',f'请明确AA成员{member}的收款人尾号。'+(f'候选：{choices}。' if choices else ''))
        ids.append(matches[0].id)
    if len(set(ids)) != len(ids):
        raise DomainError('NEEDS_CLARIFICATION','同一个AA成员不能重复，请核对参与人。')
    if requirement.share_mode != 'equal':
        raise DomainError('UNSUPPORTED_SPLIT','当前AA收款支持平均分摊，请明确使用均分。')
    return {'destination_account_id':target[0].id,'recipient_ids':ids}


def _ordinary_schedule_revision(task, text):
    from app.agent.requirements import _has_time_words
    return (task.intent in {'transfer', 'conditional_transfer'} and _has_time_words(text)
            and bool(re.search(r'改|换|预约|定时|执行|扣款', text))
            and not bool(re.search(r'什么|多少|状态|何时|什么时候|吗|[?？]', text)))


def _bridge_transfer(db, task, context):
    from app.agent.requirements import Requirement, RequirementIssue
    from app.data.models import Proposal
    proposal = db.get(Proposal, task.proposal_id) if task.proposal_id else None
    params = json.loads(proposal.params_json) if proposal else context
    source = db.get(Account, params.get('source_account_id')) if params.get('source_account_id') else None
    target = params.get('target') or {}
    own = db.get(Account, target.get('account_id')) if target.get('type') == 'own_account' else None
    recipient = db.get(Recipient, target.get('recipient_id')) if target.get('type') == 'saved_recipient' else None
    name = own.alias if own and own.owner_id == task.owner_id else (f'{recipient.name} 尾号{recipient.account_last4}' if recipient and recipient.owner_id == task.owner_id else None)
    requirement = Requirement(kind='schedule_transfer', source=source.alias if source and source.owner_id == task.owner_id else None,
                              target=name, amount_minor=params.get('amount_minor'))
    if task.intent == 'conditional_transfer':
        requirement.extraction_issues.append(RequirementIssue(code='unsupported_condition', field='execution_constraints',
            message='当前预约只支持无附加条件的单次转账；原消费条件不能自动删除，请取消后明确提出新的预约需求。'))
    return requirement.model_dump()


def dispatch(db, session, task, text, *, decision_hint=None):
    from app.agent.service import _message, _say, _task, view, _local_reply, _summary
    from app.agent.language import interpret, payload_for
    from app.agent.constraints import fresh_write_request
    from app.agent.requirements import Requirement
    from app.agent.registry import execute
    context = json.loads(task.context_json)
    previous = context.get('requirement')
    kind = decision_hint.intent if decision_hint is not None and decision_hint.intent in BUSINESS_INTENTS else detect_requirement_kind(text, previous.get('kind') if previous else None)
    if kind is None and _ordinary_schedule_revision(task, text):
        kind = 'schedule_transfer'
    if kind is None:
        return None
    selection_requirement = None
    with atomic_write(db):
        session, _ = _session(db, session.id, _now())
        task = _task(db, session, task.id)
        # Snapshot every inherited field under the same lock as the newest message.
        context = json.loads(task.context_json)
        previous = context.get('requirement')
        kind = decision_hint.intent if decision_hint is not None and decision_hint.intent in BUSINESS_INTENTS else detect_requirement_kind(text, previous.get('kind') if previous else None)
        if previous is None and _ordinary_schedule_revision(task, text):
            kind = 'schedule_transfer'
            previous = _bridge_transfer(db, task, context)
            context['requirement'] = previous
            context.pop('unsupported_write', None)
            task.context_json = _dump(context)
        if kind is None:
            return None
        current = view(db, task)
        immediate = previous and decision_hint is None and fresh_write_request(text) and detect_requirement_kind(text) is None
        if immediate:
            cancelled = cancel_business(db, session, task)
            if cancelled and cancelled['status']=='completed':
                return cancelled
            task.context_json = _dump({key:context[key] for key in ('last_query','budget') if key in context})
            task.intent=None;task.status='gathering_info';task.stop_requested=False
            commit(db)
            return None
        _message(db, task, 'user', text, already_recorded=decision_hint is not None)
        if re.fullmatch(r'(?:请|帮我)?(?:取消|停止)(?:这笔|当前|刚才|这个|该)?(?:预约转账|预约|AA收款|收款请求|任务|操作)?[。！! ]*',text):
            cancelled=cancel_business(db,session,task)
            if cancelled is not None:
                return cancelled
            task.stop_requested=True
            _say(db,task,'当前请求已取消，尚未创建预约或收款请求。','cancelled')
            return view(db,task)
        if '余额' in text and not re.search(r'改|转|收款|预约|冻结|执行',text):
            result=execute(db,session_id=session.id,owner_id=session.owner_id,name='list_accounts',arguments={})
            _say(db,task,_summary('list_accounts',result),task.status)
            return view(db,task)
        has_record=current.get('schedule') or current.get('collection')
        querying=bool(re.search(r'状态|何时|什么时候|执行了吗|收到|谁.*付|查看|查询|已收|待收|(?:金额|条件).*(?:多少|什么|解释)',text)
                      and not re.search(r'改|重新|发起|预约.*从',text))
        if previous and not has_record and querying:
            detail = (f"拟定总额 {previous['total_minor']/100:,.2f} 元。" if previous.get('total_minor') else '')
            issues = current.get('semantic_validation', {}).get('issues', []) if current.get('semantic_validation') else []
            missing = '；'.join(issue['message'] for issue in issues)
            _say(db, task, '当前需求尚未创建预约或 AA 收款请求，未授权、未扣款、未入账。' + detail + (missing or '请先补齐先前要求的信息。'), task.status)
            return view(db, task)
        if has_record:
            if re.fullmatch(r'(?:我)?确认[。！! ]*',text):
                _say(db,task,'聊天确认不会授权预约或发起收款，请核对下面固定卡片并点击专用确认按钮。',task.status)
                return view(db,task)
            smalltalk=_local_reply(text)
            querying=bool(re.search(r'状态|何时|什么时候|执行了吗|收到|谁.*付|查看|查询|已收|待收|(?:金额|条件).*(?:多少|什么|解释)',text)
                          and not re.search(r'改|重新|发起|预约.*从',text))
            editing=bool(re.search(r'改|换|调整|重新|[0-9一二两三四五六七八九十]+\s*元|明天|后天|从.*转|给.*转',text))
            if smalltalk or querying or not editing:
                _say(db,task,smalltalk or _status_reply(current),task.status)
                return view(db,task)
            if has_record['status']!='pending_confirmation':
                _say(db,task,'这项请求已经授权或结束；请先查看实际状态，修改需求须取消未完成请求后重新提出。',task.status)
                return view(db,task)
            cancel_business(db,session,task)
            context=json.loads(task.context_json)
            context.pop('schedule_id',None);context.pop('collection_id',None)
            task.context_json=_dump(context);task.plan_version+=1
        tail = re.fullmatch(r'(?:选(?:择)?(?:收款人)?)?(?:尾号)?\s*([0-9]{4})[。！! ]*',text)
        if previous and tail:
            chosen=tail.group(1)
            choices=context.get('aa_member_choices') if kind=='aa_collection' else context.get('schedule_target_choices')
            allowed=(choices.get('tails',[]) if isinstance(choices,dict) else [item['tail'] for item in choices]) if choices else []
            if choices and chosen not in allowed:
                _say(db,task,'请在本次候选尾号中选择：'+'、'.join(allowed)+'。','gathering_info')
                return view(db,task)
            if choices:
                revised=json.loads(_dump(previous))
                if kind=='aa_collection':
                    revised['participants']=[f"{member} 尾号{chosen}" if member==choices['label'] else member for member in revised['participants']]
                    revised['shares']=[]
                else:
                    revised['target']=f'尾号{chosen}' if revised.get('target_note') else f"{revised.get('target') or ''} 尾号{chosen}".strip()
                selection_requirement=Requirement.model_validate(revised)
        if task.proposal_id:
            invalidate_agent_proposal(db,session_id=session.id,proposal_id=task.proposal_id)
            task.proposal_id=None
        task.intent=kind;task.status='gathering_info';task.stop_requested=False
        revision=task.updated_at_utc
        payload=payload_for(db,task,view(db,task))
        commit(db)
    error=None;requirement=None
    try:
        if selection_requirement is not None:
            requirement=selection_requirement
        elif task.mode=='live':
            decision=decision_hint if decision_hint is not None else interpret(payload)
            if decision.intent not in BUSINESS_INTENTS:
                raise DomainError('MODEL_INVALID_RESPONSE','模型没有保留预约或收款要求，请重新说明。',502)
            requirement=from_understanding(decision,text=text,previous=previous)
        else:
            requirement=extract_mock(text,previous=previous)
        if requirement is None:
            raise DomainError('NEEDS_CLARIFICATION','请完整说明预约或AA收款需求。')
        validation=validate_requirement(requirement)
    except DomainError as failure:
        error=failure
    except (ValueError, TypeError) as failure:
        error=DomainError('INVALID_REQUIREMENT','这次需求没有形成完整结构，请核对日期、成员和金额后重新说明。')
    with atomic_write(db):
        session,_=_session(db,session.id,_now());task=_task(db,session,task.id)
        if task.updated_at_utc != revision or task.stop_requested:
            return view(db,task)
        if error:
            _say(db,task,error.message,'gathering_info');return view(db,task)
        context=json.loads(task.context_json)
        context['requirement']=validation.normalized.model_dump()
        context['semantic_validation']=validation.model_dump()
        task.context_json=_dump(context)
        if not validation.ok:
            _say(db,task,'请补充或核对：'+'；'.join(issue.message for issue in validation.issues),'gathering_info')
            return view(db,task)
        try:
            grounded=_ground(db,task,validation.normalized)
            _step(db,task,'validate_requirement',context['requirement'],{'valid':True,'roles_grounded':True},'succeeded')
            task.plan_json=_dump([{'ordinal':1,'tool_name':'validate_requirement','depends_on':[]},
                                  {'ordinal':2,'tool_name':'prepare_schedule' if kind=='schedule_transfer' else 'prepare_collection','depends_on':[1]}])
            task.requested_outcomes_json=_dump(['authorized_future_transfer' if kind=='schedule_transfer' else 'collect_equal_shares'])
            if kind=='schedule_transfer':
                from app.domain.scheduled_transfers import prepare_schedule
                result=prepare_schedule(db,session_id=session.id,**grounded,amount_minor=validation.normalized.amount_minor,
                                        execute_at_utc=validation.normalized.scheduled_at_utc,
                                        timezone_name=validation.normalized.time_zone,agent_task_id=task.id)
                record=result['schedule'];context['schedule_id']=record['id']
                _step(db,task,'prepare_schedule',grounded,result,'awaiting_confirmation',record['id'])
                reply='已准备预约，请核对日期、时区、金额和对象，点击“核对并继续”。授权时不扣款，到点由后台检查后执行一次。'
            else:
                from app.domain.collections import prepare_collection
                record=prepare_collection(db,session_id=session.id,**grounded,total_minor=validation.normalized.total_minor,
                                          request_key=f'{task.id}-{task.plan_version}',agent_task_id=task.id)
                context['collection_id']=record['id']
                _step(db,task,'prepare_collection',grounded,{'collection':record},'awaiting_confirmation',record['id'])
                reply='已准备AA收款，请核对成员、每人份额和到账账户，点击“核对并继续”。发起人自己的份额不收取；成员付款后才记到账。'
            task.context_json=_dump(context)
            _say(db,task,reply,'awaiting_confirmation')
        except DomainError as failure:
            _say(db,task,failure.message,'gathering_info')
        return view(db,task)
