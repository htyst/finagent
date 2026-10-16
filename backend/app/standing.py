"""Monthly plans. Every occurrence prepares a normal proposal; never auto-confirms."""
import calendar
import hashlib
import hmac
import json
import secrets
from datetime import datetime,timedelta
from uuid import uuid4
from sqlalchemy import select
from app.clock import now,business_day,SHANGHAI
from app.data.atomic import atomic_write,commit
from app.data.models import DemoSession,DemoSetting,AuditEvent,Account,Recipient,Proposal
from app.neuro_models import StandingOrder,StandingConfirmation,StandingOccurrence
from app.domain.errors import DomainError

def dump(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()
def intent_digest(row):
    return digest(dump({'owner':row.owner_id,'dataset':row.dataset_version,'action':row.action,
                        'params':json.loads(row.params_json),'schedule':json.loads(row.schedule_json)}))
def audit(db,row,event,detail=None):
    db.add(AuditEvent(id=uuid4().hex,owner_id=row.owner_id,proposal_id=None,event=event,
                      detail_code=detail or row.id,created_at_utc=now().isoformat(timespec='microseconds')))
def owned(db,session_owner,order_id):
    row = db.scalar(select(StandingOrder).where(StandingOrder.id==order_id,StandingOrder.owner_id==session_owner))
    if not row or row.dataset_version != db.get(DemoSetting,1).dataset_version:
        raise DomainError('RESOURCE_NOT_FOUND','定期转账不存在',404)
    return row
def prepare_order(db,session,params,recurrence):
    if recurrence.get('freq') != 'monthly' or type(recurrence.get('day')) is not int or not 1 <= recurrence['day'] <= 31:
        raise DomainError('INVALID_INPUT','每月转账需要固定日期，设置为每月1号可以吗？')
    from app.agent.registry import validate
    params = validate('prepare_transfer',params)
    if params.get('condition'):
        raise DomainError('INVALID_INPUT','本期周期转账不组合消费条件，请分别提出需求。')
    from app.domain.ledger import owned_account
    from app.domain.money import validate_transfer_amount
    with atomic_write(db):
        owned_account(db,session.owner_id,params['source_account_id'])
        target=params['target']
        if target['type']=='own_account':
            owned_account(db,session.owner_id,target['account_id'])
            if target['account_id']==params['source_account_id']:
                raise DomainError('INVALID_INPUT','来源和目标账户不能相同')
        else:
            recipient=db.get(Recipient,target['recipient_id'])
            if not recipient or recipient.owner_id!=session.owner_id or recipient.status!='active':
                raise DomainError('INVALID_INPUT','收款人不可用')
        validate_transfer_amount(params['amount_minor'])
        schedule={'freq':'monthly','day':recurrence['day']}
        row=StandingOrder(id=uuid4().hex,owner_id=session.owner_id,dataset_version=session.dataset_version,
            action='prepare_transfer',params_json=dump(params),schedule_json=dump(schedule),intent_hash='',
            status='pending_confirmation',created_at=now().isoformat(),activated_at=None)
        row.intent_hash=intent_digest(row)
        db.add(row)
        prepare_confirmation(db,session,row,'activate')
        audit(db,row,'standing_prepared')
    return view_order(db,session.owner_id,row.id)
def prepare_confirmation(db,session,row,operation):
    for old in db.scalars(select(StandingConfirmation).where(StandingConfirmation.order_id==row.id,
                          StandingConfirmation.status=='pending')).all():
        old.status='invalidated'
        old.token_hash=None
    c=StandingConfirmation(id=uuid4().hex,order_id=row.id,owner_id=session.owner_id,session_id=session.id,
        operation=operation,intent_hash=row.intent_hash,token_hash=None,status='pending',
        expires_at=(now()+timedelta(minutes=5)).isoformat())
    db.add(c)
    db.flush()
    return c
def view_order(db,session_owner,order_id):
    row=owned(db,session_owner,order_id)
    c=db.scalar(select(StandingConfirmation).where(StandingConfirmation.order_id==row.id,
                StandingConfirmation.status=='pending'))
    params=json.loads(row.params_json)
    source=db.get(Account,params['source_account_id'])
    target=params['target']
    destination=db.get(Account,target.get('account_id')) if target['type']=='own_account' else db.get(Recipient,target['recipient_id'])
    schedule=json.loads(row.schedule_json)
    summary=f"每月{schedule['day']}日从{source.alias}向{getattr(destination,'alias',None) or destination.name}尾号{destination.account_last4}转账{params['amount_minor']/100:.2f}元，持续生效直至取消。月末不足该日期时按当月最后一天处理；每次仍需页面确认。"
    if c and c.operation=='cancel':
        summary='确认取消该定期转账，后续月份不再生成操作单。'
    return {'id':row.id,'status':row.status,'schedule':schedule,'params':params,'intent_hash':row.intent_hash,
        'confirmation':{'id':c.id,'operation':c.operation,'expires_at':c.expires_at,
                        'status':'expired' if datetime.fromisoformat(c.expires_at)<=now() else c.status,
                        'summary':summary} if c else None}
def confirmation(db,session,cid):
    c=db.scalar(select(StandingConfirmation).where(StandingConfirmation.id==cid,
                StandingConfirmation.owner_id==session.owner_id,StandingConfirmation.session_id==session.id))
    if not c:
        raise DomainError('RESOURCE_NOT_FOUND','确认请求不存在',404)
    row=owned(db,session.owner_id,c.order_id)
    if datetime.fromisoformat(c.expires_at)<=now() or c.status!='pending':
        raise DomainError('PROPOSAL_EXPIRED','定期转账确认请求已失效，请重新准备',409)
    if c.intent_hash!=row.intent_hash or row.intent_hash!=intent_digest(row):
        raise DomainError('STALE_RESOURCE','定期转账参数已变化',409)
    return c,row
def issue_token(db,session,cid):
    with atomic_write(db):
        c,row=confirmation(db,session,cid)
        token=secrets.token_urlsafe(32)
        c.token_hash=digest(token)
        audit(db,row,'standing_token_issued')
    return {'confirmation_token':token}
def confirm(db,session,cid,token):
    with atomic_write(db):
        existing=db.get(StandingConfirmation,cid)
        if existing and existing.owner_id==session.owner_id and existing.session_id==session.id and existing.status=='confirmed':
            return view_order(db,session.owner_id,existing.order_id)
        c,row=confirmation(db,session,cid)
        if not c.token_hash or not hmac.compare_digest(c.token_hash,digest(token)):
            raise DomainError('CONFIRMATION_INVALID','确认凭据无效',403)
        row.status='active' if c.operation=='activate' else 'cancelled'
        if c.operation=='cancel':
            from app.domain.operations import _end
            occurrences=db.scalars(select(StandingOccurrence).where(StandingOccurrence.order_id==row.id)).all()
            for occurrence in occurrences:
                proposal=db.get(Proposal,occurrence.proposal_id) if occurrence.proposal_id else None
                if proposal and proposal.status=='pending_confirmation':
                    _end(db,proposal,'invalidated',now(),'STANDING_CANCELLED')
                    occurrence.status='cancelled'
                    occurrence.message='定期转账已取消，本期未执行操作单已失效。'
        if c.operation=='activate':
            row.activated_at=now().isoformat()
        c.status='confirmed'
        c.token_hash=None
        audit(db,row,'standing_'+c.operation)
        from app.data.models import AgentTask
        for task in db.scalars(select(AgentTask).where(AgentTask.owner_id==session.owner_id, AgentTask.session_id==session.id)).all():
            if json.loads(task.context_json).get('standing_order_id')==row.id:
                task.status='completed'
                task.updated_at_utc=now().isoformat()
    return view_order(db,session.owner_id,row.id)
def invalidate_pending(db,session,order_id):
    row=owned(db,session.owner_id,order_id)
    if row.status=='pending_confirmation':
        row.status='cancelled'
        for c in db.scalars(select(StandingConfirmation).where(StandingConfirmation.order_id==row.id,
                               StandingConfirmation.status=='pending')).all():
            c.status='invalidated'
            c.token_hash=None
        audit(db,row,'standing_invalidated')
        commit(db)
def manage(db,session,order_id,operation):
    with atomic_write(db):
        row=owned(db,session.owner_id,order_id)
        if operation=='pause':
            if row.status!='active':
                raise DomainError('INVALID_INPUT','只有生效的定期转账可以暂停')
            row.status='paused'
        elif operation=='resume':
            if row.status!='paused':
                raise DomainError('INVALID_INPUT','只有暂停的定期转账可以恢复')
            row.status='active'
        elif operation=='cancel':
            if row.status not in {'active','paused'}:
                raise DomainError('INVALID_INPUT','该定期转账无法取消')
            prepare_confirmation(db,session,row,'cancel')
        else:
            raise DomainError('INVALID_INPUT','未知管理操作')
        audit(db,row,'standing_'+operation+'_requested')
    return view_order(db,session.owner_id,row.id)
def monthly_due(year,month,day):
    from datetime import date
    # Fixed policy: day=31 in a short month is clamped to that month's last day.
    return date(year,month,min(day,calendar.monthrange(year,month)[1]))
def tick(db,session):
    from app.domain.operations import prepare_transfer
    today=business_day()
    notifications=[]
    order_ids=list(db.scalars(select(StandingOrder.id).where(StandingOrder.owner_id==session.owner_id,
                StandingOrder.dataset_version==session.dataset_version,StandingOrder.status=='active')))
    for order_id in order_ids:
        row=owned(db,session.owner_id,order_id)
        schedule=json.loads(row.schedule_json)
        activation=datetime.fromisoformat(row.activated_at).astimezone(SHANGHAI).date()
        start_index=activation.year*12+activation.month-1
        end_index=today.year*12+today.month-1
        for index in range(start_index,end_index+1):
            year,m=divmod(index,12)
            due=monthly_due(year,m+1,schedule['day'])
            if due<activation or due>today:
                continue
            period=due.strftime('%Y-%m')
            with atomic_write(db):
                row=owned(db,session.owner_id,order_id)
                if row.status!='active':
                    break
                existing=db.scalar(select(StandingOccurrence).where(StandingOccurrence.order_id==row.id,
                                   StandingOccurrence.period==period))
                if existing:
                    continue
                occurrence=StandingOccurrence(id=uuid4().hex,order_id=row.id,owner_id=row.owner_id,period=period,
                    due_date=due.isoformat(),status='pending',proposal_id=None,message='',created_at=now().isoformat())
                db.add(occurrence)
                db.flush()
                try:
                    if row.intent_hash!=intent_digest(row):
                        raise DomainError('STALE_RESOURCE','定期转账哈希不匹配')
                    # Calendar jumps still discover each due monthly occurrence. Each
                    # is only a proposal; the one-active-write guard serializes backlog.
                    result=prepare_transfer(db,session_id=session.id,**json.loads(row.params_json))
                    occurrence.proposal_id=result['proposal']['id']
                    occurrence.status='awaiting_confirmation'
                    occurrence.message=f'{period}定期转账操作单已生成，请页面确认。'
                except DomainError as error:
                    if error.code=='ACTIVE_WRITE_TASK_EXISTS':
                        db.delete(occurrence)  # Retry next tick after pending proposal is handled.
                        notifications.append({'order_id':row.id,'period':period,'status':'deferred',
                                              'message':'已有待确认操作单，本期转账暂缓生成。'})
                        continue
                    occurrence.status='skipped'
                    occurrence.message=f'{period}已跳过：{error.message}（{error.code}）。'
                audit(db,row,'standing_occurrence',occurrence.id)
                notifications.append({'id':occurrence.id,'order_id':row.id,'period':period,
                    'status':occurrence.status,'proposal_id':occurrence.proposal_id,'message':occurrence.message})
    return notifications

def tick_active_sessions(db):
    setting=db.get(DemoSetting,1)
    if not setting:
        return
    owners=list(db.scalars(select(StandingOrder.owner_id).where(
        StandingOrder.dataset_version==setting.dataset_version,StandingOrder.status=='active').distinct()))
    for owner in owners:
        with atomic_write(db):
            candidates=db.scalars(select(DemoSession).where(DemoSession.owner_id==owner,
                                  DemoSession.dataset_version==setting.dataset_version)).all()
            valid=next((s for s in candidates if datetime.fromisoformat(s.expires_at_utc)>now()),None)
            if not valid:
                sid='standing_scheduler_'+owner+'_'+setting.dataset_version
                valid=db.get(DemoSession,sid)
                if not valid:
                    valid=DemoSession(id=sid,owner_id=owner,dataset_version=setting.dataset_version,
                        expires_at_utc=(now()+timedelta(hours=8)).isoformat(),csrf_token=secrets.token_urlsafe(32))
                    db.add(valid)
                else:
                    valid.expires_at_utc=(now()+timedelta(hours=8)).isoformat()
                db.flush()
            tick(db,valid)

def claim_occurrence(db,session,occurrence_id):
    from app.domain.operations import prepare_transfer,invalidate_agent_proposal
    with atomic_write(db):
        occurrence=db.scalar(select(StandingOccurrence).where(StandingOccurrence.id==occurrence_id,
                              StandingOccurrence.owner_id==session.owner_id))
        if not occurrence or occurrence.status not in {'awaiting_confirmation','executed'}:
            raise DomainError('RESOURCE_NOT_FOUND','本期操作单不存在或已跳过',404)
        row=owned(db,session.owner_id,occurrence.order_id)
        if row.status=='cancelled':
            raise DomainError('INVALID_PROPOSAL_STATE','定期转账已取消',409)
        if row.intent_hash!=intent_digest(row):
            raise DomainError('STALE_RESOURCE','定期转账参数已变化',409)
        proposal=db.get(Proposal,occurrence.proposal_id)
        if proposal and proposal.status=='executed':
            occurrence.status='executed'
            return {'status':'executed','message':'本期已执行，不能重复生成。','proposal_id':proposal.id}
        if proposal and proposal.session_id==session.id and proposal.status=='pending_confirmation' and datetime.fromisoformat(proposal.expires_at_utc)>now():
            from app.domain.operations import proposal_view
            return {'proposal':proposal_view(db,proposal)}
        if proposal and proposal.status=='pending_confirmation':
            # Rebinding is performed by superseding the old proposal and preparing
            # a fresh one. The original fixed params, tokens and money transaction
            # are never rewritten. The old session lease is not needed by _end.
            from app.domain.operations import _end
            _end(db,proposal,'invalidated',now(),'STANDING_REBOUND')
        result=prepare_transfer(db,session_id=session.id,**json.loads(row.params_json))
        occurrence.proposal_id=result['proposal']['id']
        occurrence.status='awaiting_confirmation'
        occurrence.message=f'{occurrence.period}操作单已重新核验，请在当前页面确认。'
        audit(db,row,'standing_claimed',occurrence.id)
    return result


def handle_chat(db,session,task,text):
    from app.agent.service import _message,_say,view
    if not any(word in text for word in ('定期转账','周期转账','长期转账')):
        return None
    _message(db,task,'user',text)
    rows=db.scalars(select(StandingOrder).where(StandingOrder.owner_id==session.owner_id,
                   StandingOrder.dataset_version==session.dataset_version,
                   StandingOrder.status.in_(['active','paused']))).all()
    operation=next((op for word,op in [('取消','cancel'),('暂停','pause'),('恢复','resume')] if word in text),None)
    if not operation:
        content='；'.join(f"编号{r.id[:8]} 每月{json.loads(r.schedule_json)['day']}日 {r.status}" for r in rows) or '当前没有生效或暂停的定期转账。'
        _say(db,task,content,'completed')
        return view(db,task)
    selected=[r for r in rows if r.id in text or r.id[:8] in text]
    if not selected and len(rows)==1:
        selected=rows
    if len(selected)!=1:
        _say(db,task,'请先查询定期转账，并补充要管理的指令编号。','gathering_info')
        return view(db,task)
    commit(db)
    order=manage(db,session,selected[0].id,operation)
    context=json.loads(task.context_json)
    if operation=='cancel':
        context['standing_order_id']=order['id']
        task.context_json=dump(context)
        _say(db,task,order['confirmation']['summary'],'awaiting_confirmation')
    else:
        _say(db,task,'定期转账已暂停。' if operation=='pause' else '定期转账已恢复。','completed')
    return view(db,task)
