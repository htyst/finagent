"""Task-scoped UI authorization. These functions are never model tools."""
from __future__ import annotations

import json
from datetime import datetime,timedelta
from uuid import uuid4

from app.data.atomic import atomic_write
from app.domain import security
from app.domain.errors import DomainError

RESOURCES = {'service':('service_request','service_request_id'),'proposal': ('proposal', None), 'wealth': ('wealth_order', 'wealth_order_id'),
    'card': ('card_request', 'card_request_id'), 'subscription': ('subscription_request', 'subscription_request_id'),
    'birthday': ('birthday_request', 'birthday_request_id'), 'schedule': ('schedule', 'schedule_id'),
    'collection': ('collection', 'collection_id')}
LABELS = {'replace_card':'补发模拟借记卡','credit_limit':'申请调整信用卡授信','cancel_contract':'取消商户会员续订','unloss':'解除卡片挂失','transfer':'转账', 'freeze':'冻结卡片', 'unfreeze':'解冻卡片', 'buy':'理财申购',
    'redeem':'理财赎回', 'apply':'申请虚拟卡', 'loss':'挂失卡片', 'limit_increase':'提高卡片限额',
    'limit_decrease':'降低卡片限额', 'online_block':'禁止线上消费', 'online_unblock':'恢复线上消费',
    'reminder':'设置续费提醒', 'revoke':'解除银行代扣', 'create':'创建生日安排', 'change':'修改生日安排',
    'cancel':'取消生日安排', 'cancel_item':'取消生日商品', 'retry':'重试未付生日商品',
    'schedule':'预约转账', 'collection':'发起AA收款'}


def binding(db, session, task, kind, rid):
    if kind not in RESOURCES:
        raise DomainError('RESOURCE_NOT_FOUND', '当前任务没有这项操作', 404)
    context=json.loads(task.context_json)
    expected=task.proposal_id if kind=='proposal' else context.get(RESOURCES[kind][1])
    if rid!=expected:
        raise DomainError('STALE_RESOURCE', '操作单已变化，请核对当前对话中的新单', 409)
    _,row,_,digest,_=security.resolve(db,session.id,kind,rid)
    if kind!='proposal' and row.agent_task_id!=task.id:
        raise DomainError('RESOURCE_NOT_FOUND', '操作不属于当前任务', 404)
    return row,security.digest({'task':task.id,'binding':digest})


def describe(db, session, task, kind, rid):
    _,identifier=binding(db,session,task,kind,rid)
    operation=security.describe(db,session.id,kind,rid)
    return {**operation,'plan_version':task.plan_version,'terms_sha256':security.digest(operation.get('terms',{})),'action_id':identifier,
            'authorization_path':f'/api/tasks/{task.id}/authorizations/{kind}/{rid}',
            'label':LABELS.get(operation['action'],'办理操作')}


def operations(db, task, resources, control):
    from app.data.models import DemoSession
    session=db.get(DemoSession,task.session_id)
    result=[]
    paused=task.stop_requested or control.get('state') in {'paused','handoff','awaiting_reverification','terminated'}
    for kind,(key,_) in RESOURCES.items():
        record=resources.get(key)
        if not isinstance(record,dict):continue
        operation=describe(db,session,task,kind,record['id'])
        pending=operation['status']=='pending_confirmation'
        expired=security.now()>=datetime.fromisoformat(operation['expires_at'])
        locked=False
        # Revocations/cancellations retain the domain's existing withdrawal path.
        if operation['action'] not in {'revoke','cancel','cancel_item'}:
            try:security.assert_unlocked(db,task.owner_id)
            except DomainError:locked=True
        assessment_needed=kind=='wealth' and bool(json.loads(task.context_json).get('pending_risk_assessment'))
        reason=('任务已暂停或停止，请先恢复可办理状态。' if paused else
                '请先完成下方风险评估；评估后会重新核对并生成操作单。' if assessment_needed and pending else
                '操作单已过期，请重新准备并核对。' if expired and pending else
                '安全验证暂时锁定，请等待冷却结束；仍可查询或取消未执行操作。' if locked and pending else None)
        result.append({**operation,'can_confirm':pending and not (paused or expired or locked or assessment_needed),
                       'blocked_reason':reason,'receipt':record.get('receipt')})
    return result


def check(db,session,task,kind,rid,action_id,*,pending=True):
    from app.agent.control import assert_running
    assert_running(db,task.id)
    row,expected=binding(db,session,task,kind,rid)
    if expected!=action_id:raise DomainError('STALE_RESOURCE','操作条款或任务版本已变化，请重新核对',409)
    if pending:security.assert_pending(row)
    return row


def handlers(kind):
    from app.domain import operations as proposal,wealth,card_management as card,subscriptions,birthdays,scheduled_transfers as schedule,collections
    from app.domain import servicing
    return {'service':(servicing.issue_token,servicing.confirm,servicing.cancel,'request_id'),
        'proposal':(proposal.issue_confirmation_token,proposal.confirm_proposal,proposal.cancel_proposal,'proposal_id'),
        'wealth':(wealth.issue_token,wealth.confirm_order,wealth.cancel_order,'order_id'),
        'card':(card.issue_token,card.confirm,card.cancel,'request_id'),
        'subscription':(subscriptions.issue_token,subscriptions.confirm,subscriptions.cancel,'request_id'),
        'birthday':(birthdays.issue_token,birthdays.confirm,birthdays.cancel,'request_id'),
        'schedule':(schedule.issue_schedule_confirmation_token,schedule.confirm_schedule,schedule.cancel_schedule,'schedule_id'),
        'collection':(collections.issue_collection_confirmation_token,collections.confirm_collection,collections.cancel_collection,'collection_id')}[kind]


def issue_token(db,session,task_id,kind,rid,action_id):
    from app.agent.service import _task
    with atomic_write(db):
        task=_task(db,session,task_id);check(db,session,task,kind,rid,action_id)
        token,_,_,field=handlers(kind)
        return token(db,session_id=session.id,**{field:rid})


def confirm(db,session,task_id,kind,rid,action_id,token):
    from app.agent.service import _task,_say,view
    task=_task(db,session,task_id);context=json.loads(task.context_json)
    receipt=context.get('authorization_receipts',{}).get(action_id)
    if receipt=={'kind':kind,'resource_id':rid}:return view(db,task)
    row,expected=binding(db,session,task,kind,rid)
    if expected!=action_id:raise DomainError('STALE_RESOURCE','操作条款或任务版本已变化，请重新核对',409)
    # The secured domain wrapper checks this binding under its writer lock.
    # Do not put an outer transaction around it: its persisted authentication
    # failure counters must survive a rejected confirmation.
    previous=db.info.get('chat_authorization')
    db.info['chat_authorization']={'task_id':task_id,'kind':kind,'rid':rid,'action_id':action_id}
    try:
        _,execute,_,field=handlers(kind)
        result=execute(db,session_id=session.id,confirmation_token=token,**{field:rid})
    except DomainError as error:
        if error.code=='RISK_ASSESSMENT_REQUIRED' and kind=='wealth':
            with atomic_write(db):
                task=_task(db,session,task_id);row,_=binding(db,session,task,kind,rid)
                kwargs={'kind':row.kind,'product_id':row.product_id,'account_id':row.account_id}
                kwargs.update({'amount_minor':row.amount_minor} if row.kind=='buy' else {'units_hundredths':row.units_hundredths})
                stage_assessment(task,kwargs)
                _say(db,task,'风险评估已失效，请先完成下方问卷；评估后重新核对原理财需求。尚未交易。','gathering_info')
        raise
    finally:
        if previous is None:db.info.pop('chat_authorization',None)
        else:db.info['chat_authorization']=previous
    with atomic_write(db):
        task=_task(db,session,task_id);context=json.loads(task.context_json)
        receipt=context.get('authorization_receipts',{}).get(action_id)
        if receipt=={'kind':kind,'resource_id':rid}:return view(db,task)
        # The domain synchronizes steps/receipts; view also resumes supported
        # conditional/composite read steps. No model call or new write here.
        context=json.loads(task.context_json)
        context.setdefault('authorization_receipts',{})[action_id]={'kind':kind,'resource_id':rid}
        task.context_json=json.dumps(context,ensure_ascii=False)
        current=view(db,task)
        status=result.get('status')
        message=(result.get('receipt') or {}).get('message') or result.get('message')
        if status=='submitted':message='调额申请已提交，等待独立模拟审批；授信额度尚未改变。' if kind=='service' else '申请已授权提交，等待银行审批；尚未发卡。'
        elif status=='scheduled':message='预约已授权保存，将在约定时间按固定条款办理；当前尚未转账。' if kind=='schedule' else message
        elif status=='collecting':message='AA收款已发起，等待成员付款；发起不代表款项已经到账。'
        if not message:message='已完成本单确认，办理状态和回执已同步到当前对话。'
        _say(db,task,message,current['status'])
        return view(db,task)


def check_domain_binding(db,session,kind,rid):
    expected=db.info.get('chat_authorization')
    if not expected:return
    from app.agent.service import _task
    if expected['kind']!=kind or expected['rid']!=rid:
        raise DomainError('STALE_RESOURCE','授权与当前固定单不一致',409)
    task=_task(db,session,expected['task_id'])
    # Existing durable results can be recovered without executing again.
    row,_=binding(db,session,task,kind,rid)
    check(db,session,task,kind,rid,expected['action_id'],pending=row.status not in security.TERMINAL and row.status!='collecting')


def cancel(db,session,task_id,kind,rid,action_id):
    from app.agent.service import _task,_say,view
    with atomic_write(db):
        task=_task(db,session,task_id)
        # Cancellation is available while paused/locked, but cannot select a new
        # resource or change its terms. The domain owns terminal-state rules.
        _,expected=binding(db,session,task,kind,rid)
        if expected!=action_id:raise DomainError('STALE_RESOURCE','操作单已变化，请核对当前单',409)
        _,_,stop,field=handlers(kind)
        stop(db,session_id=session.id,**{field:rid})
        current=view(db,task)
        _say(db,task,'本单取消结果已同步；已执行交易和已产生的回执不会撤回。',current['status'])
        return view(db,task)


def stage_assessment(task,kwargs):
    context=json.loads(task.context_json)
    context['pending_risk_assessment']={'id':uuid4().hex,'kwargs':kwargs,'plan_version':task.plan_version,
        'expires_at':(security.now()+timedelta(minutes=15)).isoformat()}
    task.context_json=json.dumps(context,ensure_ascii=False)


def prerequisites(task,control):
    from app.domain.wealth import QUESTIONS
    pending=json.loads(task.context_json).get('pending_risk_assessment')
    if not pending or task.stop_requested or control.get('state') in {'paused','handoff','awaiting_reverification','terminated'}:return []
    if pending['plan_version']!=task.plan_version or datetime.fromisoformat(pending['expires_at'])<=security.now():return []
    return [{'kind':'risk_assessment','pending_id':pending['id'],'questions':QUESTIONS,
             'label':'提交评估并继续准备','effect':'按实际情况完成评估，随后重新核对原需求。提交评估不会申购或赎回，交易仍需单独确认。'}]


def submit_assessment(db,session,task_id,pending_id,answers):
    from app.agent.service import _task,_say,_record_step,view,MAX_STEPS
    from app.data.models import AgentStep
    from sqlalchemy import select
    from app.agent.control import assert_running
    from app.domain import wealth
    with atomic_write(db):
        task=_task(db,session,task_id);assert_running(db,task.id);context=json.loads(task.context_json)
        if context.get('completed_risk_assessment')==pending_id:return view(db,task)
        pending=context.get('pending_risk_assessment')
        if not pending or pending['id']!=pending_id or not prerequisites(task,{}):
            raise DomainError('STALE_RESOURCE','需求或评估表已变化，请使用当前对话中的表单',409)
        if pending['kwargs'].get('kind')!='assessment' and db.query(AgentStep).filter_by(task_id=task.id).count()>=MAX_STEPS:
            raise DomainError('TOOL_BUDGET_EXCEEDED','当前任务步骤已达上限，请新建任务继续；尚未交易',409)
        # Domain validation enforces all four real answers and suitability.
        wealth.submit_assessment(db,session_id=session.id,answers=answers)
        context=json.loads(task.context_json);context.pop('pending_risk_assessment',None)
        context['completed_risk_assessment']=pending_id;task.context_json=json.dumps(context,ensure_ascii=False)
        try:
            kwargs=pending['kwargs']
            if kwargs.get('kind')=='compare':
                result=wealth.compare_products(db,owner_id=session.owner_id,**{k:v for k,v in kwargs.items() if k!='kind'})
                from app.agent.wealth_flow import comparison_reply
                _record_step(db,task,'compare_wealth',{},result);_say(db,task,comparison_reply(result),'completed')
            elif kwargs.get('kind')=='assessment':
                _say(db,task,'风险评估已保存，可继续咨询或比较模拟产品。','completed')
            else:
                order=wealth.prepare_order(db,session_id=session.id,agent_task_id=task.id,**kwargs)
                context=json.loads(task.context_json);context['wealth_order_id']=order['id'];task.context_json=json.dumps(context,ensure_ascii=False)
                _record_step(db,task,'prepare_wealth',{'product_id':order['product_id'],'account_id':order['account_id'],
                    'amount_minor':order['amount_minor'],'units_hundredths':order['units_hundredths']}, {'order':order})
                step=db.scalar(select(AgentStep).where(AgentStep.task_id==task.id).order_by(AgentStep.ordinal.desc()))
                step.result_ref=order['id'];step.status='awaiting_confirmation'
                _say(db,task,'评估已完成，已按原需求重新核对并准备固定理财单。请在下方核对条款并授权；尚未交易。','awaiting_confirmation')
        except DomainError as error:
            _say(db,task,'评估已保存。'+error.message+'；当前未交易，请调整需求后重新准备。','gathering_info')
        return view(db,task)
