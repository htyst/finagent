"""Ground semantic service tools in explicit user fields, then prepare, never execute."""
import json,re
from decimal import Decimal
from sqlalchemy import select
from app.data.models import Card,AgentStep,AgentMessage
from app.domain import servicing,security
from app.domain.errors import DomainError
from app.agent.requirements import MONEY,NUMBER,yuan_minor

TOOLS={'prepare_card_replacement':'replace_card','prepare_credit_limit_change':'credit_limit',
       'prepare_merchant_contract_cancel':'cancel_contract'}


def normalized(s):return re.sub(r'[（(](?:虚构|合成|模拟)[）)]','',s or '').strip()


def validate_context(name,context,*,subject=None,amount_yuan=None):
    common={'不执行','不要执行','先不执行','暂不执行','不自动执行','不扣款','不付款','不转账','不退款'}
    supported={
        'cancel_contract':{'保留本期权益','保留当前权益','保留本期权益至当前权益截止日','保留本期权益至原到期日','不延长','不延长权益','不延长本期权益',
            '不提前终止本期权益','不解除银行代扣','不要解除银行代扣','不取消其他订阅','不要取消其他订阅','本期结束后停止续订','仅取消下期续订'},
        'replace_card':{'保留原账户资金','保留原卡限制','保留原支付限制','不迁移代扣','不自动迁移代扣'},
        'credit_limit':{'仅提交申请','须独立审批','不保证获批','不直接调整额度'},
    }[TOOLS[name]]|common
    def compatible(v):
        if v in supported:return True
        if TOOLS[name]=='credit_limit' and amount_yuan is not None:
            match=re.fullmatch(r'申请新总额度('+NUMBER+r')元(?:[（(]不是增加额[）)])?',v)
            if match and yuan_minor(match[1])==yuan_minor(amount_yuan):return True
        if TOOLS[name]!='cancel_contract':return False
        match=re.fullmatch(r'(保留(?:本期|当前)权益)[（(]权益截止日期不变[）)]',v)
        if match:return compatible(match[1])
        if subject and re.fullmatch(r'仅取消'+re.escape(normalized(subject))+r'(?:的)?(?:商户)?(?:会员)?(?:合同)?(?:的)?下期续订',normalized(v)):return True
        return bool(re.fullmatch(r'保留(?:本期|当前|现有|原有)?(?:会员)?权益(?:(?:至|到)(?:当前|原有|原档案|原)?(?:权益)?(?:截止日|截止日期|到期日|到期日期))?',v) or
            re.fullmatch(r'(?:不|不要)(?:延长|恢复)(?:或|及|和)(?:恢复|延长)(?:本期|当前|已过期)?权益',v) or
            re.fullmatch(r'(?:不|不要)(?:撤销|解除)(?:独立|原有)?银行(?:代扣|自动扣费)(?:授权)?',v) or
            re.fullmatch(r'(?:不|不要)取消(?:其他|其余)订阅(?:合同)?',v))
    for key in ('conditions','prohibitions','time_requirements'):
        clauses=[]
        for value in context.get(key,[]):
            # A separate future instruction is not authority for this request.
            if key=='prohibitions' and re.fullmatch(r'(?:不|不要)解除银行代扣[，,]除非另行明确要求',value):value=value.split('，')[0].split(',')[0]
            clauses.extend(v.strip() for v in re.split('[，,、；;]',value) if v.strip())
        if any(not compatible(v) for v in clauses):
            raise DomainError('UNSUPPORTED_SERVICE_CONDITION','该附加条件不在当前固定业务规则内，不能静默忽略；请明确即时办理需求',422)


def prepare(db,session,task,name,args,text,*,modify=False):
    from app.agent.service import _say,view,_record_step
    from app.domain.financial_profile import rows
    kind=TOOLS[name];ctx=json.loads(task.context_json);prior=ctx.get('service_draft',{})
    continuing=prior.get('kind')==kind and (prior.get('incomplete',False) or modify and bool(re.search(r'改|调|提高|降低|换|重新',text)))
    if re.search(r'美元|美金|USD|EUR|欧元|港币|港元|日元|JPY',text,re.I):
        raise DomainError('UNSUPPORTED_CURRENCY','当前服务只支持人民币案例，不能丢弃外币或擅自换算',422)
    if kind=='replace_card' and re.search(r'不换号|不换卡号|保留原卡号|原卡号不变',text):
        raise DomainError('UNSUPPORTED_SERVICE_CONDITION','当前补发会生成新卡号，不能保留已挂失原卡号继续使用',422)
    negative_text=text
    for harmless in ('不要取消其他订阅','不取消其他订阅','不要解除银行代扣','不解除银行代扣','不要提前终止本期权益','不要退款','不要执行'):
        negative_text=negative_text.replace(harmless,'')
    if re.search(r'不要|不需要|不想|别帮|不是要|不是让|不补卡|别补卡|别取消|不取消|不调整|不调额|先不申请|怎么|如何|假设|举例|如果|只有|超过.*才|明天|下周|下个?月|月底|等到|(?:每天|每月).{0,6}(?:帮|给|申请|调|提|补|取消)|[?？]',negative_text):
        raise DomainError('UNGROUNDED_OPERATION','请明确本次办理意图；未准备或执行业务',422)
    intent={'replace_card':r'补卡|补发|补办|补(?:一下|一张|新的|新)?(?:银行卡|借记卡|卡)|换.{0,4}新卡','credit_limit':r'授信|提额|降额|额度.{0,12}(?:调|提|降|改)|(?:调|提|降|改).{0,12}额度',
        'cancel_contract':r'(?:取消|关闭|停止|退订).{0,60}(?:会员|合同|订阅|续订)|不再续订'}
    if not continuing and not re.search(intent[kind],text):
        raise DomainError('UNGROUNDED_OPERATION','这项办理缺少明确用户指令，请先核对意图',422)
    if kind=='cancel_contract':security.assert_subscription_access(db,session.id)
    choices=rows(db,session.owner_id,'subscriptions') if kind=='cancel_contract' else rows(db,session.owner_id,'cards') if kind=='credit_limit' else [
        dict(id=c.id,alias=c.alias,card_last4=c.card_last4) for c in db.scalars(select(Card).where(Card.owner_id==session.owner_id))]
    if kind=='credit_limit':choices=[c for c in choices if c.get('card_type')=='credit']
    if kind=='credit_limit' and not prior and not ctx.get('service_request_id') and re.search('继续|接着',text):
        # Some old conversations asked a missing-field question without invoking
        # prepare. Recover only a uniquely named card from an explicit *user*
        # application in this task. Never inherit old amounts or assistant text.
        history=list(db.scalars(select(AgentMessage).where(AgentMessage.task_id==task.id,AgentMessage.role=='user')
            .order_by(AgentMessage.ordinal.desc()).limit(12)))[1:]
        for message in history:
            original=message.content
            if re.search('取消|算了|不办|停止|不用|不再|换张|另一张',original):break
            if re.search(r'申请|调整|调额|改',original) and re.search(r'信用卡|授信|调额',original):
                if re.search(r'不要|怎么|如何|假设|如果|明天|下周|[?？]',original):break
                matched=[c for c in choices if c.get('card_last4') and re.search(r'(?:尾号\s*|\*\*)'+re.escape(c['card_last4'])+r'(?!\d)',original)]
                if len(matched)==1:
                    prior=dict(kind=kind,subject_id=matched[0]['id'],amount_minor=None,incomplete=True);continuing=True
                break
            if re.search(r'转账|转.{0,12}元|补卡|补发|挂失|解挂|申购|赎回|取消.{0,12}订阅',original):break
    field='merchant' if kind=='cancel_contract' else 'card'
    def identifier(c):return c['subscription_id'] if kind=='cancel_contract' else c['id']
    def names(c):return [identifier(c),c.get('service_name') or c.get('alias') or '',c.get('card_last4') or '']
    target_text=re.sub(r'(?:保留|保持)[^，,；;。]*','',text)
    def mentioned(c):return any(v and normalized(v) in normalized(target_text) for v in names(c))
    candidates=[c for c in choices if mentioned(c)]
    reference=args.get(field)
    if reference:
        matched=[c for c in choices if normalized(reference) in [normalized(v) for v in names(c) if v] or
            field=='card' and c.get('card_last4') and re.fullmatch(r'(?:尾号\s*|\*\*)?'+re.escape(c['card_last4']),reference)]
        if len(matched)!=1:raise DomainError('RESOURCE_UNRESOLVED','对象不唯一或不属于本人，请提供本人卡尾号或订阅编号',422)
        chosen=matched[0]
        if not mentioned(chosen) and not (continuing and prior.get('subject_id')==identifier(chosen)):
            raise DomainError('UNGROUNDED_OPERATION','工具中的对象未得到用户明确内容支持',422)
        explicit_ids=[c for c in candidates if identifier(c) in text]
        if len(candidates)>1 and not (len(explicit_ids)==1 and identifier(explicit_ids[0])==identifier(chosen)):
            raise DomainError('RESOURCE_UNRESOLVED','本轮涉及多个对象，请明确唯一卡尾号或合同编号',422)
        candidates=[chosen]
    elif not candidates and continuing and prior.get('subject_id'):
        candidates=[c for c in choices if identifier(c)==prior['subject_id']]
    subject_id=identifier(candidates[0]) if len(candidates)==1 else None
    amount=prior.get('amount_minor') if continuing else None
    if kind=='credit_limit' and args.get('amount_yuan') is not None:
        proposed=yuan_minor(args['amount_yuan']);stated={yuan_minor(m[1]) for m in MONEY.finditer(text)}
        stated|={int(Decimal(m[1])*1000000) for m in re.finditer(r'(\d+(?:\.\d{1,2})?)\s*万(?:元)?',text)}
        for m in re.finditer(r'(?:调整?(?:到|为|至)|提高(?:到|至)|降低(?:到|至)|改(?:为|成)|额度(?:为|是))\s*('+NUMBER+r')',text):
            stated.add(yuan_minor(m[1]))
        if proposed not in stated and not (continuing and proposed==amount):
            raise DomainError('UNGROUNDED_AMOUNT','申请额度未得到用户明确金额支持',422)
        amount=proposed
    ctx['service_draft']={'kind':kind,'subject_id':subject_id,'amount_minor':amount,'incomplete':subject_id is None or kind=='credit_limit' and amount is None}
    task.context_json=json.dumps(ctx,ensure_ascii=False);task.intent='service_'+kind
    if ctx['service_draft']['incomplete']:
        _say(db,task,'请补充'+('一项本人订阅名称或合同编号' if field=='merchant' else '本人卡片尾号')+
            ('和申请的新信用额度（元）' if kind=='credit_limit' else '')+'；已有明确信息会保留，当前未办理。','gathering_info')
        return view(db,task)
    if modify:servicing.invalidate_task(db,task)
    if kind=='credit_limit':
        current=servicing.basis(db,session,kind,subject_id)['credit_limit_minor']
        increase=bool(re.search(r'提高|提升|增加|提额',text));decrease=bool(re.search(r'降低|减少|降额',text))
        if increase and not decrease and amount<=current or decrease and not increase and amount>=current:
            raise DomainError('CREDIT_DIRECTION_MISMATCH','申请的新总额度与本轮提额或降额方向不一致；请明确新的总额度，不要填写增减金额',422)
    result=servicing.prepare(db,session_id=session.id,kind=kind,subject_id=subject_id,
        amount_minor=amount if kind=='credit_limit' else None,agent_task_id=task.id)
    ctx=json.loads(task.context_json);ctx['service_request_id']=result['id'];ctx['service_draft']['incomplete']=False
    task.context_json=json.dumps(ctx,ensure_ascii=False)
    _record_step(db,task,name,{'subject_id':subject_id},{'service_request':result})
    step=db.scalar(select(AgentStep).where(AgentStep.task_id==task.id).order_by(AgentStep.ordinal.desc()))
    step.result_ref=result['id'];step.status='awaiting_confirmation'
    _say(db,task,'已准备'+result['label']+'。请核对下方固定条款并手动确认；当前尚未执行。','awaiting_confirmation')
    return view(db,task)
