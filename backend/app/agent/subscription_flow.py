"""Subscription understanding never grants execution authority."""
from __future__ import annotations
import json,re
from sqlalchemy import select
from app.data.atomic import atomic_write,commit
from app.data.models import AgentStep
from app.domain.errors import DomainError
from app.domain.operations import _session,_now
from app.domain.subscriptions import identify,get_portfolio,prepare,invalidate_task,cancel,task_state
INTENTS={'inspect':'subscription_inspect','reminder':'subscription_reminder','revoke':'subscription_revoke','status':'subscription_status'}
NUMBERS={'零':0,'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}
def number(value):
 if value.isdigit():return int(value)
 if '十' in value:
  left,right=value.split('十',1)
  return NUMBERS.get(left,1)*10+NUMBERS.get(right,0)
 return NUMBERS.get(value)
def detect(text):
 if re.search(r'查询|查看|看看|记录|状态|结果|列表',text) and not re.search(r'^(?:请|帮我)?(?:取消|解除|关闭|停止)',text) and re.search(r'订阅|代扣|续费提醒|通知',text) and not re.search(r'找出|识别|可能|疑似',text):return 'status'
 if '提醒' in text and re.search(r'续费|扣费|扣款',text):return 'reminder'
 if re.search(r'取消|解除|关闭|停止',text) and re.search(r'自动续费|续费|代扣',text):return 'revoke'
 if re.search(r'订阅|持续.*自动扣费',text) and re.search(r'找|识别|可能|疑似',text):return 'inspect'
 return None
def slots(text,authorizations):
 target=re.sub(r'(?:保留|保持)[^，,；;。]*','',text)
 names={r['merchant'] for r in authorizations if r['merchant'] in target}
 values={'merchant':next(iter(names)) if len(names)==1 else None}
 before=re.search(r'前\s*(\d+|[零一二两三四五六七八九十]{1,3})\s*天',text)
 clock=re.search(r'(上午|早上|下午|晚上)?\s*(\d{1,2}|[零一二两三四五六七八九十]{1,3})(?:点|[:：])(?:(半|一刻|三刻)|(\d{1,2}|[零一二两三四五六七八九十]{1,3})分?)?',text)
 if before:values['days_before']=number(before.group(1))
 if clock:
  hour=number(clock.group(2))
  if clock.group(1) in ('下午','晚上') and hour is not None and hour<12:hour+=12
  values.update(hour=hour,minute={'半':30,'一刻':15,'三刻':45}.get(clock.group(3),number(clock.group(4)) if clock.group(4) else 0))
 if re.search(r'北京时间|上海时间|Asia/Shanghai',text):values['time_zone']='Asia/Shanghai'
 return values,names
def unsupported(text,authorizations):
 foreign_time=re.findall(r'([A-Za-z/\u4e00-\u9fff]+)时间',text)
 allowed_time_words=('北京','上海','提醒','续费','扣费','本次','下次','指定','执行','取消')
 if re.search(r'UTC|GMT|东京|日本时间|纽约|伦敦|欧洲时间|美国时间|(?:Europe|America)/|Asia/(?!Shanghai)',text,re.I) or any(not word.endswith(allowed_time_words) for word in foreign_time) or ('时区' in text and not re.search(r'北京|上海|Asia/Shanghai',text)):
  return '当前提醒只支持北京时间Asia/Shanghai；不能丢弃指定时区。请明确北京时间的本次提醒时点，未办理新业务。'
 if re.search(r'(?:取消|解除|关闭|停止)[^，,；;。]*提醒',text):
  return '取消已设置的提醒，请在“订阅与代扣”的提醒记录点击“取消续费提醒”；未新建提醒或解除代扣。'
 for row in authorizations:
  name=re.escape(row['merchant'])
  if re.search(r'(?:保留|保持)[^，,；;。]*'+name,text) and re.search(r'(?:取消|解除|关闭|停止)[^，,；;。]*'+name,text):
   return '同一指令同时要求保留和取消同一项代扣，请澄清最终对象；未办理新业务。'
 if re.search(r'转账|转给|转\s*(?:\d|[一二两三四五六七八九十])|申购|赎回|冻结|解冻|挂失|申请.*卡|单笔.*限额|AA收款',text,re.I):
  return '订阅需求包含另一项资金或卡片写操作，请分别提出办理需求；未办理新业务。'
 if re.search(r'(?:然后|并且|同时|[，,；;])\s*(?:再|也|请)?(?:查一下|查看|查询|看看)',text):
  return '订阅办理与查询是两项需求，请分别提出，避免遗漏操作；未办理新业务。'
 if len(re.findall(r'(?:\d{1,2}|[零一二两三四五六七八九十]{1,3})(?:点|[:：])',text))>1:
  return '发现多个提醒钟点，请明确本次唯一的北京时间提醒时点；未办理新业务。'
 if re.search(r'不要|不需要|无需|没说|并未|别取消|不取消|不想|如何|怎么关闭|怎么解除|能否|是否|假设|举例|[?？]|每天|每月|每次|如果|只要|超过|少于|低于|高于|才提醒|才取消|美元',text):
  return '未更改代扣或提醒。当前支持为已确认的银行代扣设置下一次续费提醒，或解除指定银行代扣；请明确对象和本次时间。'
 return None

def _invalidate_other(db,session,task):
 from app.domain.card_management import invalidate_task as invalidate_card
 from app.domain.wealth import invalidate_task_order
 from app.domain.operations import invalidate_agent_proposal
 invalidate_card(db,task);invalidate_task_order(db,task)
 if task.proposal_id:
  invalidate_agent_proposal(db,session_id=session.id,proposal_id=task.proposal_id);task.proposal_id=None
def dispatch(db,session,task,text, *, decision_hint=None):
 from app.agent.service import _message,_say,_task,view,_dump,_record_step,MAX_STEPS
 ctx=json.loads(task.context_json)
 kind=next((key for key,value in INTENTS.items() if value==decision_hint.intent),None) if decision_hint is not None else detect(text)
 if kind is None and task.intent in ('subscription_reminder','subscription_revoke') and task.status=='gathering_info' and re.search(r'云|前.*天|点|北京时间',text):kind=ctx.get('subscription_kind')
 if kind is None:return None
 if task.stop_requested:return view(db,task)
 sid,tid=session.id,task.id;writing=kind in ('reminder','revoke')
 try:authorizations=get_portfolio(db,sid)['authorizations']
 except DomainError as error:
  if error.code!='SUBSCRIPTION_CONFIRMATION_REQUIRED':raise
  _message(db,task,'user',text,already_recorded=decision_hint is not None);_say(db,task,error.message+'。请在订阅面板确认后重新发送本需求。','gathering_info');return view(db,task)
 explicit,names=slots(text,authorizations)
 blocked=unsupported(text,authorizations) if writing else None
 if blocked:
  with atomic_write(db):
   session,_=_session(db,sid,_now());task=_task(db,session,tid);_message(db,task,'user',text,already_recorded=decision_hint is not None)
   if not re.search(r'不要|不需要|无需|没说|并未|不取消|不想',text):invalidate_task(db,task)
   pending=task_state(db,task).get('subscription_request')
   _say(db,task,blocked,'awaiting_confirmation' if pending and pending['status']=='pending_confirmation' else 'gathering_info')
   return view(db,task)
 if writing and len(names)>1:
  with atomic_write(db):
   task=_task(db,session,tid);_message(db,task,'user',text,already_recorded=decision_hint is not None);invalidate_task(db,task);_say(db,task,'一次办理仅支持一项指定代扣，请分别提出需求；未更改授权。','gathering_info');return view(db,task)
 _message(db,task,'user',text,already_recorded=decision_hint is not None);version=ctx.get('subscription_version',0)+1;ctx['subscription_version']=version;task.context_json=_dump(ctx);commit(db)
 values=dict(ctx.get('subscription_slots',{})) if ctx.get('subscription_kind')==kind and task.status=='gathering_info' else {}
 values.update({k:v for k,v in explicit.items() if v is not None})
 if task.mode=='live':
  from app.agent.language import interpret,payload_for
  payload=payload_for(db,task,view(db,task));commit(db)
  try:
   decision=decision_hint if decision_hint is not None else interpret(payload)
   if decision.intent!=INTENTS[kind]:raise DomainError('MODEL_SEMANTIC_MISMATCH','这次未能准确理解订阅需求，未办理；请核对操作类型')
   for key in ('merchant','days_before','hour','minute','time_zone'):
    value=getattr(decision,key,None)
    if value is None:continue
    if key=='time_zone' and value=='北京时间':value='Asia/Shanghai'
    allowed=values.get(key,'Asia/Shanghai' if key=='time_zone' else None)
    if allowed is None or value!=allowed:raise DomainError('MODEL_SEMANTIC_MISMATCH','模型提取含未明确的信息或与明确对象、提醒时间不一致；请补充需求，未办理')
    values[key]=value
  except DomainError as error:
   with atomic_write(db):
    session,_=_session(db,sid,_now());task=_task(db,session,tid)
    if not task.stop_requested and json.loads(task.context_json).get('subscription_version')==version:
     if writing:invalidate_task(db,task)
     _say(db,task,error.message,'gathering_info')
    return view(db,task)
 with atomic_write(db):
  session,_=_session(db,sid,_now());task=_task(db,session,tid);latest=json.loads(task.context_json)
  if task.stop_requested or latest.get('subscription_version')!=version:return view(db,task)
  if db.query(AgentStep).filter(AgentStep.task_id==tid).count()>=MAX_STEPS:
   _say(db,task,'本次任务步骤已达上限，请新建对话需求；未办理新业务。','gathering_info');return view(db,task)
  if kind=='inspect':
   result={'candidates':identify(db,sid)};_record_step(db,task,'identify_subscriptions',{},result);task.intent=INTENTS[kind]
   lines=[f"{r['merchant']}：{r['amount_minor']/100:.2f}元/月，依据"+'、'.join(x['posted_at_utc'][:10] for x in r['evidence']) for r in result['candidates']]
   _say(db,task,('识别到疑似订阅：\n'+'\n'.join(lines) if lines else '目前流水未发现符合至少三次连续月同日同金额规则的疑似订阅。')+'\n这些仅为疑似周期扣费，不代表已授权或确定订阅。逐笔依据见“订阅与代扣”；单次消费和不规则购物不认定为订阅。','completed');return view(db,task)
  if kind=='status':
   result=get_portfolio(db,sid);_record_step(db,task,'read_subscriptions',{},result);pending=task_state(db,task).get('subscription_request')
   if not pending or pending['status']!='pending_confirmation':task.intent=INTENTS[kind]
   _say(db,task,'银行代扣：'+('；'.join(r['merchant']+'：'+('有效' if r['status']=='active' else '已解除') for r in result['authorizations']) or '暂无已确认授权')+'。续费提醒和站内通知请查看“订阅与代扣”。解除银行代扣不等于关闭商户会员或合同。','awaiting_confirmation' if pending and pending['status']=='pending_confirmation' else 'completed');return view(db,task)
  invalidate_task(db,task);_invalidate_other(db,session,task)
  latest['subscription_kind']=kind;latest['subscription_slots']=values;task.context_json=_dump(latest);task.intent=INTENTS[kind];task.status='gathering_info'
  try:
   rows=[r for r in get_portfolio(db,sid)['authorizations'] if r['merchant']==values.get('merchant')]
   if len(rows)!=1:
    _say(db,task,'请明确一项已确认的银行代扣商户；同名多账户需先明确账户。未从疑似流水自动创建授权。','gathering_info');return view(db,task)
   kwargs=dict(session_id=sid,kind=kind,authorization_id=rows[0]['id'],agent_task_id=tid)
   if kind=='reminder':
    if any(values.get(k) is None for k in ('days_before','hour','minute')):
     _say(db,task,'请补充续费前几天、几点提醒，例如“续费前两天上午9点”。提醒使用北京时间，站内触达。','gathering_info');return view(db,task)
    kwargs.update(days_before=values['days_before'],hour=values['hour'],minute=values['minute'],timezone=values.get('time_zone','Asia/Shanghai'))
   result=prepare(db,**kwargs);latest['subscription_request_id']=result['id'];task.context_json=_dump(latest)
   _record_step(db,task,'prepare_subscription',{'kind':kind,'authorization_id':rows[0]['id']},{'subscription_request':result});step=db.scalar(select(AgentStep).where(AgentStep.task_id==tid).order_by(AgentStep.ordinal.desc()));step.result_ref=result['id'];step.status='awaiting_confirmation'
   button='确认续费提醒' if kind=='reminder' else '确认取消代扣';detail='；只解除银行代扣，商户会员或合同须另行处理' if kind=='revoke' else '；下一次续费前提醒，通知将在站内收件箱保存'
   _say(db,task,f"已准备{result['label']}：{rows[0]['merchant']}，预计扣费{rows[0]['amount_minor']/100:.2f}元"+detail+f"。请核对固定办理单并点击“{button}”。聊天确认不授权；当前尚未办理。",'awaiting_confirmation')
  except DomainError as error:_say(db,task,error.message+'；未办理新业务。','gathering_info')
  return view(db,task)
def cancel_subscription(db,session,task):
 from app.agent.service import _say,view
 current=task_state(db,task).get('subscription_request')
 if not current or (task.intent not in ('subscription_reminder','subscription_revoke') and current['status']!='pending_confirmation'):return None
 result=cancel(db,session_id=session.id,request_id=current['id']);stopped=result['status'] in ('cancelled','invalidated','expired')
 if stopped:task.stop_requested=True
 _say(db,task,'订阅办理单已取消或失效，未办理。' if stopped else '已完成的订阅办理记录保留；取消当前对话不撤销已授权业务。','cancelled' if stopped else 'completed');return view(db,task)

