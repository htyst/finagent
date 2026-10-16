"""Deterministic controls, separate issuance approval and serialized payment settlement."""
import hmac,json,secrets
from datetime import datetime,timedelta
from uuid import uuid4
from sqlalchemy import select
from app.data.atomic import atomic_write
from app.data.models import Card,User,AgentTask,AgentStep,AuditEvent,Task
from app.data.card_models import CardControl,CardRequest,CardPayment
from app.domain.errors import DomainError
from app.domain.operations import _session,_now,_stamp,_canonical,_digest
from app.domain.ledger import owned_account,post_entry,available_balance
from app.domain.money import MAX_MINOR
LABELS={'apply':'申请虚拟借记卡','limit':'调整单笔交易限额','online_block':'禁止线上消费','online_unblock':'解除线上消费限制','loss':'卡片挂失'}
RULES='虚拟借记卡为本地测试产品；本人资料和关联账户须核对，授权提交进入审核，单独模拟审批发卡。单笔限额初始1000元，线上/线下分别控制；整卡冻结和挂失均拒绝消费。挂失不可通过解冻解除，当前不提供解挂或补卡。'
def owned_card(db,owner,card_id):
 row=db.scalar(select(Card).where(Card.id==card_id,Card.owner_id==owner))
 if row is None:raise DomainError('RESOURCE_NOT_FOUND','资源不存在',404)
 return row
def controls(db,card):
 row=db.get(CardControl,card.id)
 return dict(limit_minor=row.limit_minor if row else 100000,online_enabled=bool(row.online_enabled) if row else True,lost=bool(row.lost) if row else False,version=row.version if row else 0)
def check_purchase(db,card,amount,channel):
 c=controls(db,card)
 if c['lost']:raise DomainError('CARD_LOST','卡片已挂失，原卡交易被拒绝',409)
 if card.status!='active':raise DomainError('INVALID_CARD_STATE','卡片已冻结，不能新增模拟消费',409)
 if amount>c['limit_minor']:raise DomainError('CARD_LIMIT_EXCEEDED',f"超过单笔交易限额{c['limit_minor']/100:.2f}元",409)
 if channel=='online' and not c['online_enabled']:raise DomainError('ONLINE_DISABLED','该卡线上消费已禁止；线下不受此项限制',409)
def request_view(row,now=None):
 status='expired' if row.status=='pending_confirmation' and datetime.fromisoformat(row.expires_at_utc)<=_now(now) else row.status
 return dict(id=row.id,kind=row.kind,label=LABELS[row.kind],status=status,summary=json.loads(row.terms_json),card_id=row.card_id,account_id=row.account_id,issued_card_id=row.issued_card_id,created_at_utc=row.created_at_utc,expires_at_utc=row.expires_at_utc,submitted_at_utc=row.submitted_at_utc,completed_at_utc=row.completed_at_utc,receipt=json.loads(row.receipt_json) if row.receipt_json else None)
def portfolio(db,session_id):
 session,_=_session(db,session_id,_now())
 return dict(rules=RULES,cards=[dict(id=r.id,alias=r.alias,mask='**'+r.card_last4,account_id=r.account_id,status=r.status,**controls(db,r)) for r in db.scalars(select(Card).where(Card.owner_id==session.owner_id).order_by(Card.id))],requests=[request_view(r) for r in db.scalars(select(CardRequest).where(CardRequest.owner_id==session.owner_id,CardRequest.dataset_version==session.dataset_version).order_by(CardRequest.created_at_utc.desc()).limit(100))],payments=[payment_view(r) for r in db.scalars(select(CardPayment).where(CardPayment.owner_id==session.owner_id).order_by(CardPayment.created_at_utc.desc()).limit(100))])
def ensure_no_pending(db,session_id,instant):
 from app.domain.subscriptions import ensure_no_pending as ensure_no_pending_subscription
 ensure_no_pending_subscription(db,session_id,instant)
 if db.scalar(select(CardRequest.id).where(CardRequest.session_id==session_id,CardRequest.status=='pending_confirmation',CardRequest.expires_at_utc>_stamp(instant))):raise DomainError('ACTIVE_WRITE_TASK_EXISTS','请先确认或取消当前卡片办理单',409)
def sync(db,row):
 task=db.get(AgentTask,row.agent_task_id) if row.agent_task_id else None
 if task and json.loads(task.context_json).get('card_request_id')==row.id:
  if (task.intent or '').startswith('card_management'):
   status={'executed':'completed','issued':'completed','submitted':'completed','pending_confirmation':'awaiting_confirmation','invalidated':'gathering_info'}.get(row.status,row.status)
   if task.status!=status:task.status=status;task.updated_at_utc=_stamp(_now())
  for step in db.scalars(select(AgentStep).where(AgentStep.task_id==task.id,AgentStep.result_ref==row.id)):
   step.status={'executed':'succeeded','issued':'succeeded','submitted':'succeeded','pending_confirmation':'awaiting_confirmation'}.get(row.status,row.status);step.result_json=_canonical({'card_request':request_view(row)})
def invalidate_task(db,task):
 rid=json.loads(task.context_json).get('card_request_id');row=db.get(CardRequest,rid) if rid else None
 if row and row.status=='pending_confirmation':row.status='invalidated';row.token_hash=None;sync(db,row)
def invalidate_session(db,session_id):
 for row in db.scalars(select(CardRequest).where(CardRequest.session_id==session_id,CardRequest.status=='pending_confirmation')):
  row.status='invalidated';row.token_hash=None;sync(db,row)
  if row.agent_task_id:
   task=db.get(AgentTask,row.agent_task_id)
   if task:task.stop_requested=True
from app.domain.security import preparing

@preparing
def prepare(db,*,session_id,kind,card_id=None,account_id=None,limit_minor=None,applicant=None,agent_task_id=None,now=None):
 if kind not in LABELS:raise DomainError('INVALID_INPUT','未知卡片业务')
 instant=_now(now)
 with atomic_write(db):
  session,_=_session(db,session_id,instant)
  if agent_task_id:
   task=db.get(AgentTask,agent_task_id)
   if not task or task.session_id!=session.id or task.owner_id!=session.owner_id or task.stop_requested:raise DomainError('INVALID_TASK_STATE','对话已停止',409)
  card=None
  if kind=='apply':
   user=db.get(User,session.owner_id)
   if applicant!=user.display_name:raise DomainError('APPLICANT_MISMATCH','请核对当前演示用户本人的申请资料')
  else:
   card=owned_card(db,session.owner_id,card_id);account_id=card.account_id
   if controls(db,card)['lost']:raise DomainError('CARD_LOST','该卡已挂失；不支持解挂，不能调整原卡限制',409)
  account=owned_account(db,session.owner_id,account_id)
  if kind=='limit' and (type(limit_minor) is not int or not 0<limit_minor<=10000000):raise DomainError('INVALID_INPUT','单笔限额须大于0且不超过10万元')
  terms=dict(kind=kind,card_id=card_id,card_alias=card.alias if card else None,card_mask='**'+card.card_last4 if card else None,card_version=card.version if card else None,controls=controls(db,card) if card else None,account_id=account.id,account_alias=account.alias,account_mask='**'+account.account_last4,applicant=applicant,product='虚拟借记卡' if kind=='apply' else None,limit_minor=limit_minor if kind=='limit' else None)
  digest=_digest(_canonical(terms))
  for old in db.scalars(select(CardRequest).where(CardRequest.session_id==session.id,CardRequest.status=='pending_confirmation')):
   if datetime.fromisoformat(old.expires_at_utc)<=instant:old.status='expired';old.token_hash=None;sync(db,old)
   elif old.agent_task_id==agent_task_id and old.terms_digest==digest:return request_view(old,instant)
   elif agent_task_id and old.agent_task_id==agent_task_id:old.status='invalidated';old.token_hash=None;sync(db,old)
   else:raise DomainError('ACTIVE_WRITE_TASK_EXISTS','请先处理已有卡片办理单',409)
  from app.domain.subscriptions import ensure_no_pending as ensure_no_pending_subscription
  ensure_no_pending_subscription(db,session.id,instant)
  from app.data.wealth_models import WealthOrder
  from app.data.scheduled_models import ScheduledTransfer
  from app.data.collection_models import CollectionRequest
  if db.scalar(select(Task.id).where(Task.active_session_id==session.id)) or any(db.scalar(select(m.id).where(m.session_id==session.id,m.status=='pending_confirmation',m.expires_at_utc>_stamp(instant))) for m in (WealthOrder,ScheduledTransfer,CollectionRequest)):raise DomainError('ACTIVE_WRITE_TASK_EXISTS','请先确认或取消其他待确认业务',409)
  row=CardRequest(id=uuid4().hex,owner_id=session.owner_id,session_id=session.id,dataset_version=session.dataset_version,agent_task_id=agent_task_id,kind=kind,card_id=card_id,account_id=account.id,status='pending_confirmation',terms_json=_canonical(terms),terms_digest=digest,created_at_utc=_stamp(instant),expires_at_utc=_stamp(instant+timedelta(minutes=5)))
  db.add(row);db.flush();return request_view(row,instant)
def owned_request(db,session,rid):
 row=db.scalar(select(CardRequest).where(CardRequest.id==rid,CardRequest.owner_id==session.owner_id,CardRequest.dataset_version==session.dataset_version))
 if not row:raise DomainError('RESOURCE_NOT_FOUND','资源不存在',404)
 return row
def fixed(row):
 if not hmac.compare_digest(row.terms_digest,_digest(row.terms_json)):raise DomainError('REQUEST_CHANGED','固定申请内容已变化',409)
 terms=json.loads(row.terms_json)
 if any(getattr(row,k)!=terms[k] for k in ('kind','card_id','account_id')):raise DomainError('REQUEST_CHANGED','申请字段与固定条款不一致',409)
 return terms
def draft(db,session,rid,instant):
 row=owned_request(db,session,rid)
 if row.session_id!=session.id:raise DomainError('INVALID_ORDER_SESSION','请在原会话办理',409)
 if row.status!='pending_confirmation' or datetime.fromisoformat(row.expires_at_utc)<=instant:raise DomainError('INVALID_ORDER_STATE','办理单已失效或过期',409)
 if row.agent_task_id:
  task=db.get(AgentTask,row.agent_task_id)
  if task.stop_requested:raise DomainError('INVALID_TASK_STATE','任务已取消',409)
 fixed(row);return row
def issue_token(db,*,session_id,request_id,now=None):
 instant=_now(now)
 with atomic_write(db):
  session,_=_session(db,session_id,instant);row=draft(db,session,request_id,instant);token=secrets.token_urlsafe(32);row.token_hash=_digest(token);return {'confirmation_token':token}
from app.domain.security import secured

@secured('card', 'request_id')
def confirm(db,*,session_id,request_id,confirmation_token,now=None):
 instant=_now(now)
 with atomic_write(db):
  session,_=_session(db,session_id,instant);row=owned_request(db,session,request_id)
  if row.session_id!=session.id:raise DomainError('INVALID_ORDER_SESSION','请在原会话办理',409)
  if row.status in ('submitted','issued','executed'):return request_view(row,instant)
  row=draft(db,session,request_id,instant)
  if not isinstance(confirmation_token,str) or not row.token_hash or not hmac.compare_digest(row.token_hash,_digest(confirmation_token)):raise DomainError('CONFIRMATION_INVALID','确认凭据无效',403)
  terms=fixed(row);owned_account(db,session.owner_id,row.account_id)
  if row.kind=='apply':
   if db.get(User,session.owner_id).display_name!=terms['applicant']:raise DomainError('STALE_RESOURCE','申请资料已变化',409)
   row.status='submitted';row.submitted_at_utc=_stamp(instant);receipt=dict(message='申请已受理，审核中，尚未发卡',status='submitted',applicant=terms['applicant'],product=terms['product'])
  else:
   card=owned_card(db,session.owner_id,row.card_id)
   if card.version!=terms['card_version'] or controls(db,card)!=terms['controls']:raise DomainError('STALE_RESOURCE','卡片状态或限制已变化，请重新准备',409)
   if controls(db,card)['lost']:raise DomainError('CARD_LOST','卡片已挂失',409)
   c=db.get(CardControl,card.id)
   if c is None:c=CardControl(card_id=card.id,limit_minor=100000,online_enabled=1,lost=0,version=0);db.add(c)
   if row.kind=='limit':c.limit_minor=terms['limit_minor']
   elif row.kind=='online_block':c.online_enabled=0
   elif row.kind=='online_unblock':c.online_enabled=1
   elif row.kind=='loss':c.lost=1
   c.version+=1;card.version+=1;row.status='executed';row.completed_at_utc=_stamp(instant)
   receipt=dict(message=LABELS[row.kind]+'已办理',status='executed',card_id=card.id,card_mask='**'+card.card_last4,**controls(db,card))
  row.token_hash=None;row.receipt_json=_canonical(dict(request_id=row.id,completed_at_utc=_stamp(instant),**receipt));db.add(AuditEvent(id=uuid4().hex,owner_id=session.owner_id,event='card_'+row.kind+'_confirmed',created_at_utc=_stamp(instant)));db.flush();sync(db,row);return request_view(row,instant)
def approve(db,*,session_id,request_id,now=None):
 instant=_now(now)
 with atomic_write(db):
  session,_=_session(db,session_id,instant);row=owned_request(db,session,request_id)
  if row.kind!='apply':raise DomainError('INVALID_ORDER_STATE','不是卡片申请',409)
  if row.status=='issued':return request_view(row,instant)
  if row.status!='submitted':raise DomainError('INVALID_ORDER_STATE','须先授权提交申请，再模拟审批',409)
  from app.domain.security import assert_unlocked
  assert_unlocked(db,session.owner_id)
  terms=fixed(row);owned_account(db,session.owner_id,row.account_id)
  used={r.card_last4 for r in db.scalars(select(Card))};tail=next((str(n) for n in range(7000,10000) if str(n) not in used),None)
  if tail is None:raise DomainError('CARD_CAPACITY','测试卡编号已用完',409)
  card=Card(id=uuid4().hex,owner_id=session.owner_id,account_id=row.account_id,alias='虚拟借记卡',brand='模拟卡',card_last4=tail,status='active',version=1);db.add(card);db.flush();db.add(CardControl(card_id=card.id,limit_minor=100000,online_enabled=1,lost=0,version=1));row.issued_card_id=card.id;row.status='issued';row.completed_at_utc=_stamp(instant);row.receipt_json=_canonical(dict(message='模拟审批通过，已发卡',request_id=row.id,card_id=card.id,card_mask='**'+tail,applicant=terms['applicant'],product=terms['product'],account_id=row.account_id,completed_at_utc=row.completed_at_utc));db.add(AuditEvent(id=uuid4().hex,owner_id=session.owner_id,event='card_application_simulated_approval',created_at_utc=_stamp(instant)));sync(db,row);return request_view(row,instant)
def cancel(db,*,session_id,request_id):
 with atomic_write(db):
  session,_=_session(db,session_id,_now());row=owned_request(db,session,request_id)
  if row.session_id!=session.id:raise DomainError('INVALID_ORDER_SESSION','请在原会话取消',409)
  if row.status=='pending_confirmation':
   row.status='cancelled';row.token_hash=None;sync(db,row)
   if row.agent_task_id:db.get(AgentTask,row.agent_task_id).stop_requested=True
  return request_view(row)
def task_state(db,task):
 rid=json.loads(task.context_json).get('card_request_id');row=db.get(CardRequest,rid) if rid else None
 if row and row.owner_id==task.owner_id and row.session_id==task.session_id:
  if row.status=='pending_confirmation' and datetime.fromisoformat(row.expires_at_utc)<=_now():row.status='expired';row.token_hash=None
  sync(db,row);return {'card_request':request_view(row)}
 return {'card_request':None}
def payment_view(row):
 messages={'CARD_LOST':'卡片已挂失，交易被拒绝','INVALID_CARD_STATE':'卡片冻结，交易被拒绝','CARD_LIMIT_EXCEEDED':'超过单笔交易限额，交易被拒绝','ONLINE_DISABLED':'线上消费已禁止，交易被拒绝','INSUFFICIENT_FUNDS':'账户余额不足，交易被拒绝'}
 return dict(id=row.id,card_id=row.card_id,amount_minor=row.amount_minor,channel=row.channel,status=row.status,error_code=row.error_code,transaction_id=row.transaction_id,balance_minor=row.balance_minor,created_at_utc=row.created_at_utc,message='模拟消费已入账' if row.status=='posted' else messages.get(row.error_code,'交易被拒绝'))
def payment(db,*,session_id,card_id,amount_minor,channel,idempotency_key,now=None):
 if type(amount_minor) is not int or not 0<amount_minor<=MAX_MINOR or channel not in ('online','offline') or not isinstance(idempotency_key,str) or not 1<=len(idempotency_key)<=100:raise DomainError('INVALID_INPUT','消费金额、渠道或重试编号无效')
 instant=_now(now)
 with atomic_write(db):
  session,_=_session(db,session_id,instant);card=owned_card(db,session.owner_id,card_id);account=owned_account(db,session.owner_id,card.account_id)
  old=db.scalar(select(CardPayment).where(CardPayment.owner_id==session.owner_id,CardPayment.idempotency_key==idempotency_key))
  if old:
   if (old.card_id,old.amount_minor,old.channel)!=(card_id,amount_minor,channel):raise DomainError('IDEMPOTENCY_CONFLICT','重试编号对应另一笔消费',409)
   return payment_view(old)
  error=None
  try:
   check_purchase(db,card,amount_minor,channel)
   if available_balance(db,account)<amount_minor:raise DomainError('INSUFFICIENT_FUNDS','账户一般可用余额不足，预留专款不能用于普通消费',409)
  except DomainError as e:error=e.code
  row=CardPayment(id=uuid4().hex,owner_id=session.owner_id,card_id=card.id,idempotency_key=idempotency_key,amount_minor=amount_minor,channel=channel,status='rejected' if error else 'posted',error_code=error,balance_minor=account.balance_minor,created_at_utc=_stamp(instant))
  if not error:
   tx=post_entry(db,owner_id=session.owner_id,account_id=account.id,kind='purchase',direction='debit',amount_minor=amount_minor,business_at=instant,card_id=card.id,category='购物',merchant='模拟'+('线上' if channel=='online' else '线下')+'消费',business_id=row.id,system_now=instant,channel=channel);row.transaction_id=tx.id;row.balance_minor=account.balance_minor
  db.add(row);db.flush();return payment_view(row)
