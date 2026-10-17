"""Owner grounding and exact-slot validation precede deterministic card business."""
import json,re
from sqlalchemy import select
from app.data.atomic import atomic_write,commit
from app.data.models import Account,Card,User,AgentStep
from app.domain.operations import _session,_now
from app.domain.errors import DomainError
from app.domain.wealth import decimal_hundredths
from app.domain.card_management import prepare,portfolio,invalidate_task,cancel,request_view,LABELS

def detect(text):
 # Normalize channel vocabulary for routing only; strict slots still use the
 # original text, and the same authorization/condition guards remain in force.
 channel_text=re.sub(r'网上|在线|网购', '线上', text)
 if re.search(r'查询|查看|记录|结果|列表',text) and (re.search(r'卡片|卡|挂失|申请',text) or re.search(r'尾号\s*\d{4}',text) and re.search(r'线上|网上|在线|交易.*限制|限额',text)):return 'status'
 if '线上' in channel_text and re.search(r'消费|付款|支付|交易', channel_text):
  if re.search(r'解除|恢复|允许|开启|打开',channel_text):return 'online_unblock'
  if re.search(r'禁止|关闭|禁用|暂停|关掉',channel_text):return 'online_block'
 if re.search(r'申请.*(?:借记卡|虚拟卡|银行卡)|办一张.*卡',text):return 'apply'
 if re.search(r'单笔.*(?:限额|额度)|交易限额',text):return 'limit'
 if re.search(r'解除.*线上|恢复.*线上|允许.*线上',text):return 'online_unblock'
 if re.search(r'(?:禁止|关闭|禁用|暂停).*线上',text):return 'online_block'
 if re.search(r'解挂|解除挂失|撤销挂失',text):return 'unloss'
 if re.search(r'挂失|卡.*丢了',text):return 'loss'
 return None
def slots(text,account_aliases=()):
 tails=re.findall(r'尾号\s*(\d{4})(?!\d)',text);accounts=list(dict.fromkeys([a for a in account_aliases if a in text]+re.findall(r'账户[A-Z]',text)));amounts=re.findall(r'(\d+(?:\.\d+)?)\s*元',text)
 return dict(card=tails[0] if len(tails)==1 else None,account=accounts[0] if len(accounts)==1 else None,amount=amounts[-1] if amounts else None,applicant='本人' if re.search(r'申请人.*本人',text) else None)
def dispatch(db,session,task,text, *, decision_hint=None):
 from app.agent.service import _message,_say,_task,view,_dump,_record_step
 ctx=json.loads(task.context_json)
 kind=('status' if decision_hint.intent=='card_management_status' else decision_hint.action if decision_hint.intent=='card_management' else None) if decision_hint is not None else detect(text)
 if kind is None and task.intent=='card_management' and task.status=='gathering_info' and re.search(r'申请人|本人|账户|尾号|元',text):kind=ctx.get('card_kind')
 if kind is None:return None
 if task.stop_requested:return view(db,task)
 if kind!='status' and len(set(re.findall(r'尾号\s*(\d{4})(?!\d)',text)))>1:
  with atomic_write(db):
   task=_task(db,session,task.id);_message(db,task,'user',text,already_recorded=decision_hint is not None);invalidate_task(db,task);_say(db,task,'一次办理仅支持一张指定卡，请分别提出需求；未准备或办理新业务。','gathering_info');return view(db,task)
 if kind!='status' and re.search(r'怎么|如何|是否|能否|不要|不想|如果|明天|每天|每月|定时|[?？]',text):
  with atomic_write(db):
   task=_task(db,session,task.id);_message(db,task,'user',text,already_recorded=decision_hint is not None);invalidate_task(db,task);_say(db,task,'请明确当前要准备的卡片办理需求；未办理或改变卡片。','gathering_info');return view(db,task)
 _message(db,task,'user',text,already_recorded=decision_hint is not None);version=ctx.get('card_request_version',0)+1;ctx['card_request_version']=version;task.context_json=_dump(ctx);commit(db)
 explicit=slots(text,db.scalars(select(Account.alias).where(Account.owner_id==session.owner_id)).all());values={**(ctx.get('card_slots',{}) if ctx.get('card_kind')==kind else {}),**{k:v for k,v in explicit.items() if v is not None}}
 if task.mode=='live':
  from app.agent.language import interpret,payload_for
  payload=payload_for(db,task,view(db,task));commit(db)
  try:
   decision=decision_hint if decision_hint is not None else interpret(payload)
   if decision.intent!=('card_management_status' if kind=='status' else 'card_management') or kind!='status' and decision.action!=kind:raise DomainError('MODEL_SEMANTIC_MISMATCH','这次未能准确理解卡片需求，未办理；请核对操作类型')
   raw=dict(card=decision.card,account=decision.source,amount=decision.amount_yuan,applicant=decision.applicant)
   for key,value in raw.items():
    if value is None:continue
    if explicit.get(key) is not None:
     same=decimal_hundredths(value,'限额')==decimal_hundredths(explicit[key],'限额') if key=='amount' else explicit[key] in value if key=='card' else value==explicit[key] or key=='applicant' and value in ('我','本人','演示用户本人')
     if not same:raise DomainError('MODEL_SEMANTIC_MISMATCH','模型提取与本轮明确卡片、限额或资料不一致；未办理')
    values[key]=value
  except DomainError as error:
   with atomic_write(db):
    task=_task(db,session,task.id)
    if not task.stop_requested and json.loads(task.context_json).get('card_request_version')==version:
     invalidate_task(db,task);_say(db,task,error.message,'gathering_info')
    return view(db,task)
 with atomic_write(db):
  session,_=_session(db,session.id,_now());task=_task(db,session,task.id);latest=json.loads(task.context_json)
  if task.stop_requested or latest.get('card_request_version')!=version:return view(db,task)
  if kind=='status':
   data=portfolio(db,session.id);labels={'pending_confirmation':'待确认，未办理','submitted':'审核中，尚未发卡','issued':'已发卡','executed':'已办理','cancelled':'已取消','invalidated':'已失效','expired':'已过期'}
   reply='卡片：'+('；'.join(r['alias']+' '+r['mask']+'：'+('已挂失' if r['lost'] else '已冻结' if r['status']=='frozen' else '正常')+f"，单笔限额{r['limit_minor']/100:.2f}元，线上消费"+('允许' if r['online_enabled'] else '禁止')+'，线下消费'+('禁止' if r['lost'] or r['status']!='active' else '可用，仍受单笔限额约束') for r in data['cards']) or '暂无')+'。\n申请与办理记录：'+('；'.join(r['label']+' '+r['id']+'：'+labels[r['status']] for r in data['requests']) or '暂无')+'。挂失不能通过解冻解除；解挂需单独强验证和页面确认，其他限制保留；当前不支持补卡。'
   _record_step(db,task,'read_card_management',{},data);task.intent='card_management_status';_say(db,task,reply,'completed');return view(db,task)
  invalidate_task(db,task)
  from app.domain.wealth import invalidate_task_order
  invalidate_task_order(db,task)
  # Moving to another card write invalidates previous credentials, including shared freeze proposals.
  if task.proposal_id:
   from app.domain.operations import invalidate_agent_proposal
   invalidate_agent_proposal(db,session_id=session.id,proposal_id=task.proposal_id);task.proposal_id=None
  latest['card_kind']=kind;latest['card_slots']=values;task.context_json=_dump(latest);task.intent='card_management'
  try:
   kwargs=dict(session_id=session.id,kind=kind,agent_task_id=task.id)
   if kind=='apply':
    if not values.get('account') or not values.get('applicant'):
     _say(db,task,'请补充申请人（当前演示用户本人）和关联账户。虚拟借记卡为测试产品，授权提交后进入审核，独立模拟审批通过后发卡。','gathering_info');return view(db,task)
    accounts=db.scalars(select(Account).where(Account.owner_id==session.owner_id)).all();matched=[a for a in accounts if values['account'] in (a.id,a.alias,a.account_last4,'尾号'+a.account_last4)]
    if len(matched)!=1:raise DomainError('ACCOUNT_UNRESOLVED','请明确本人关联账户')
    user=db.get(User,session.owner_id);applicant=user.display_name if values['applicant'] in ('本人','我','演示用户本人') else values['applicant'];kwargs.update(account_id=matched[0].id,applicant=applicant)
   else:
    cards=db.scalars(select(Card).where(Card.owner_id==session.owner_id)).all();reference=values.get('card');matched=[c for c in cards if reference and reference in (c.id,c.alias,c.card_last4,'尾号'+c.card_last4)]
    if len(matched)!=1:
     _say(db,task,'请明确要办理的卡片尾号；可选：'+'、'.join(c.alias+' 尾号'+c.card_last4 for c in cards)+'。','gathering_info');return view(db,task)
    kwargs['card_id']=matched[0].id
    if kind=='limit':
     if not values.get('amount'):_say(db,task,'请提供新的单笔交易限额（元）。','gathering_info');return view(db,task)
     kwargs['limit_minor']=decimal_hundredths(values['amount'],'限额')
   result=prepare(db,**kwargs);latest['card_request_id']=result['id'];task.context_json=_dump(latest);_record_step(db,task,'prepare_card_management',{'kind':kind,'card_id':result['card_id']},{'card_request':result});step=db.scalar(select(AgentStep).where(AgentStep.task_id==task.id).order_by(AgentStep.ordinal.desc()));step.result_ref=result['id'];step.status='awaiting_confirmation';terms=result['summary'];detail=('申请人'+terms['applicant']+'、关联'+terms['account_alias']) if kind=='apply' else terms['card_mask']+(f"、新单笔限额{terms['limit_minor']/100:.2f}元" if kind=='limit' else '')
   _say(db,task,'已准备'+LABELS[kind]+'：'+detail+'。未办理、未改变卡片；请核对下方固定办理单，点击“核对并继续”。聊天确认不授权。','awaiting_confirmation');return view(db,task)
  except DomainError as e:_say(db,task,e.message+'；未办理新业务。','gathering_info');return view(db,task)
def cancel_card(db,session,task):
 from app.agent.service import _say,view
 rid=json.loads(task.context_json).get('card_request_id')
 if not rid or task.intent!='card_management':return None
 row=cancel(db,session_id=session.id,request_id=rid);_say(db,task,'卡片办理单已取消或失效，未办理。' if row['status'] in ('cancelled','invalidated','expired') else '已受理或已办理的卡片记录保留。','cancelled' if row['status']=='cancelled' else 'completed');return view(db,task)
