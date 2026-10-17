"""Structured user supplements preserve literal names and addresses."""
from __future__ import annotations
import json
import re
from sqlalchemy import select
from app.data.atomic import atomic_write
from app.data.models import Account, AgentStep
from app.domain.errors import DomainError
from app.domain.operations import _session, _now

def payment_account(db, owner_id, reference):
    ref=reference.strip().removeprefix("付款账户").strip(' ：:')
    short=ref.removeprefix("账户").removeprefix("尾号").strip()
    rows=db.scalars(select(Account).where(Account.owner_id==owner_id,Account.status=="active")).all()
    composite=re.fullmatch(r'(.+?)\s*(?:尾号|\*{2,}|•{2,})\s*(\d{4})',ref)
    if composite:
        alias,tail=composite.groups();alias=alias.strip(' ·•，,')
        matches=[a for a in rows if alias in {a.alias,a.alias.removeprefix('账户')} and a.account_last4==tail]
    else:
        matches=[a for a in rows if ref in {a.id,a.alias,a.account_last4} or short in {a.alias.removeprefix("账户"),a.account_last4}]
    return matches[0] if len(matches)==1 else None


def transfer_values(db,task,ctx):
    slots=ctx.get('dialogue_slots',{}) if ctx.get('dialogue_intent')=='transfer' else {}
    source=payment_account(db,task.owner_id,ctx.get('source_account_id') or slots.get('source') or '')
    amount=ctx.get('amount_minor')
    if amount is None and slots.get('amount_yuan'):
        from app.domain.wealth import decimal_hundredths
        try:amount=decimal_hundredths(slots['amount_yuan'],'金额')
        except DomainError:pass
    return source,amount

def birthday_details(db,task):
    ctx=json.loads(task.context_json)
    if task.intent!="birthday_create" or ctx.get("birthday_kind")!="create" or task.stop_requested or task.status!="gathering_info" or ctx.get("clarification"):return None
    slots=ctx.get("birthday_slots",{})
    ref=slots.get("source_reference","")
    return {"source_reference":ref,"recipient":slots.get("recipient",""),"address":slots.get("address",""),
            "source_error":"该付款账户尚未关联，请填写用于扣款的本人账户名称或尾号。" if ref and not slots.get("source_account_id") else None}

def supplement_birthday(db,session,task_id,details):
    from app.agent.service import _task,view
    from app.agent.birthday_flow import dispatch
    sid=session.id
    with atomic_write(db):
        session,_=_session(db,sid,_now());task=_task(db,session,task_id)
        if birthday_details(db,task) is None:
            raise DomainError("INVALID_TASK_STATE","当前任务没有待补充的生日收货资料，请重新查询任务状态",409)
        return dispatch(db,session,task,"已通过输入框补充付款账户、收货人和收货地址。",details=details)

def transfer_details(db,task):
    ctx=json.loads(task.context_json)
    if task.intent!="transfer" or task.status!="gathering_info" or task.stop_requested or ctx.get("target",{}).get("type") in {"own_account","saved_recipient"}:return None
    partial=ctx.get("direct_recipient",{})
    source,amount=transfer_values(db,task,ctx)
    return {"name":partial.get("name",""),"bank_name":partial.get("bank_name",""),"account_number":partial.get("account_number",""),
        'source_reference':source.id if source else '', 'amount_yuan':f'{amount//100}.{amount%100:02d}' if type(amount) is int and amount>0 else ''}

def supplement_transfer(db,session,task_id,details):
    from app.agent.service import _task,_message,_say,_dump,view,_resolve,_record_step,MAX_STEPS
    from app.agent.registry import validate,execute
    from app.domain.direct_recipient import validated_target
    from app.agent.clarification import _invalidate_drafts
    recipient={key:details[key] for key in ('name','bank_name','account_number')}
    target=validated_target({"type":"external_account",**recipient})
    sid=session.id
    with atomic_write(db):
        session,_=_session(db,sid,_now());task=_task(db,session,task_id)
        if transfer_details(db,task) is None:
            raise DomainError("INVALID_TASK_STATE","当前任务没有待补充的转账收款资料",409)
        ctx=json.loads(task.context_json)
        source,amount=transfer_values(db,task,ctx)
        reference=details.get('source_reference','').strip()
        if reference:
            source=payment_account(db,task.owner_id,reference)
            if source is None:raise DomainError('ACCOUNT_UNRESOLVED','请选择本人有效的付款账户，名称与尾号须一致',422)
        if details.get('amount_yuan','').strip():
            from app.domain.wealth import decimal_hundredths
            amount=decimal_hundredths(details['amount_yuan'].strip(),'转账金额')
        ctx=_invalidate_drafts(db,session,task,clear_slots=False)
        ctx.pop("dialogue_intent",None);ctx.pop("dialogue_slots",None)
        if source:ctx['source_account_id']=source.id
        else:ctx.pop('source_account_id',None)
        if amount is not None:ctx['amount_minor']=amount
        ctx["target"]=target;ctx["direct_recipient"]=recipient;ctx["direct_recipient_active"]=True;ctx["clarification_transfer_active"]=True
        fields=ctx.setdefault('dialogue_memory',{}).setdefault('fields',{})
        for key,value in [('source',source.alias if source else None),('amount_yuan',f'{amount//100}.{amount%100:02d}' if amount else None)]:
            if value is not None:fields[key]={'value':value,'origin':'current_user' if (reference if key=='source' else details.get('amount_yuan')) else 'prior_draft','explicit_selection':True,'approved_fixed_form':False}
        task.intent="transfer"
        _message(db,task,"user",f"已通过表单补充收款人{target['name']}、开户银行{target['bank_name']}及收款账号尾号{target['account_number'][-4:]}"+(f"，付款账户{source.alias} 尾号{source.account_last4}" if source else '')+(f"，金额{amount/100:.2f}元" if amount else '')+'。')
        name,args,question=_resolve(task,"",ctx,db)
        task.context_json=_dump(ctx)
        if question:
            _say(db,task,question,"gathering_info");return view(db,task)
        if db.query(AgentStep).filter_by(task_id=task.id).count()>=MAX_STEPS:
            raise DomainError("TOOL_BUDGET_EXCEEDED","本次任务步骤已达上限",409)
        result=execute(db,session_id=sid,owner_id=session.owner_id,name=name,arguments=validate(name,args),agent_task_id=task.id)
        _record_step(db,task,name,args,result);task.proposal_id=result['proposal']['id']
        _say(db,task,"已准备新收款人的固定操作单，请核对户名、银行、完整账号和金额，再点击页面确认；当前没有扣款。","awaiting_confirmation")
        return view(db,task)
