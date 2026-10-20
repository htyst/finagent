"""Explicit fixed terms, deterministic local providers, and separately gated approvals."""
import hmac,json,secrets
from datetime import datetime,timedelta
from uuid import uuid4
from sqlalchemy import select,func
from app.data.atomic import atomic_write
from app.data.models import Card,AgentTask,AgentStep,DemoSetting,AuditEvent
from app.data.card_models import CardControl
from app.data.servicing_models import ServiceRequest,CreditFacility,MerchantContractState
from app.domain.operations import _session,_now,_stamp,_canonical,_digest
from app.domain.errors import DomainError
from app.domain import security

LABELS={'replace_card':'补发模拟借记卡','credit_limit':'申请调整信用卡授信额度','cancel_contract':'取消模拟商户会员续订'}
MAX_CREDIT_MINOR=100000000


def retired(db,owner,card_id):
    setting=db.get(DemoSetting,1)
    return bool(db.scalar(select(ServiceRequest.id).where(ServiceRequest.owner_id==owner,
        ServiceRequest.dataset_version==(setting.dataset_version if setting else ''),
        ServiceRequest.subject_id==card_id,ServiceRequest.kind=='replace_card',ServiceRequest.status=='executed')))


def credit_projection(db,owner,raw):
    setting=db.get(DemoSetting,1)
    row=db.get(CreditFacility,(setting.dataset_version,raw['id'])) if setting else None
    if not row or row.owner_id!=owner:return raw
    from app.domain.credit_cards import amount
    source=amount(raw,'credit_limit_minor')
    if source!=row.source_limit_minor:raise DomainError('STALE_RESOURCE','信用卡来源额度已变化，请核对档案与审批记录',409)
    available=amount(raw,'available_credit_minor')
    return {**raw,'credit_limit_minor':row.limit_minor,
        'available_credit_minor':None if available is None else available+row.limit_minor-source,
        'limit_origin':'local_simulated_approval','limit_updated_at_utc':row.updated_at_utc,'facility_version':row.version}


def contract_projection(db,owner,raw):
    setting=db.get(DemoSetting,1)
    state=db.get(MerchantContractState,(setting.dataset_version,raw['subscription_id'])) if setting else None
    if not state or state.owner_id!=owner:return raw
    return {**raw,'auto_renew_enabled':False,'status':'non_renewing','access_end_date':state.access_end_date,
        'next_billing_date':None,'archived_next_billing_date':raw.get('next_billing_date'),
        'access_period_state':access_period(state.access_end_date),
        'merchant_cancellation_status':'confirmed_by_local_simulator','merchant_cancelled_at_utc':state.cancelled_at_utc}


def access_period(end):
    if not end:return 'unknown'
    from app.domain.clock import SHANGHAI
    return 'expired' if end<_now().astimezone(SHANGHAI).date().isoformat() else 'within_recorded_end_date'


def merchant_stopped(db,auth):
    from app.domain.financial_profile import metadata
    link=metadata(db,auth.owner_id,'native_subscription_links',auth.id)
    if not link:return False
    row=db.get(MerchantContractState,(auth.dataset_version,link['contract_id']))
    return bool(row and row.owner_id==auth.owner_id)


def basis(db,session,kind,subject_id,selection_basis=None):
    from app.domain.financial_profile import metadata
    if kind=='replace_card':
        from app.domain.card_management import owned_card,controls
        from app.domain.ledger import owned_account
        card=owned_card(db,session.owner_id,subject_id);owned_account(db,session.owner_id,card.account_id)
        current=controls(db,card)
        if not current['lost']:raise DomainError('CARD_NOT_LOST','补卡须先完成原卡挂失；找到原卡时可单独申请解挂',409)
        return dict(card_id=card.id,card_alias=card.alias,card_mask='**'+card.card_last4,account_id=card.account_id,
            card_version=card.version,controls=current,status=card.status,
            impact='本地模拟借记卡补发；原卡永久停用，关联账户与资金保留；冻结、线上限制和单笔限额保留，代扣不自动迁移')
    if kind=='credit_limit':
        from app.domain.credit_cards import amount
        raw=metadata(db,session.owner_id,'cards',subject_id)
        if not raw or raw.get('card_type')!='credit':raise DomainError('RESOURCE_NOT_FOUND','本人信用卡不存在',404)
        if raw.get('status')!='active':raise DomainError('INVALID_CARD_STATE','非正常信用卡不能申请调额',409)
        current=credit_projection(db,session.owner_id,raw)
        values={k:amount(current,k) for k in ('credit_limit_minor','outstanding_minor','available_credit_minor')}
        if any(v is None for v in values.values()):raise DomainError('DATA_UNAVAILABLE','额度或占用资料缺失，暂不能提交调额申请',409)
        return dict(credit_card_id=raw['id'],card_alias=raw.get('alias'),card_mask='**'+raw['card_last4'],
            **values,facility_version=current.get('facility_version',0),source_digest=_digest(_canonical(raw)),
            impact='仅提交本地模拟调额申请，确认不代表获批；须独立审批。欠款与可用额度基于导入快照，不支持真实信用卡交易')
    if kind=='cancel_contract':
        security.assert_subscription_access(db,session.id)
        raw=metadata(db,session.owner_id,'subscriptions',subject_id)
        if not raw:raise DomainError('RESOURCE_NOT_FOUND','本人会员合同不存在',404)
        current=contract_projection(db,session.owner_id,raw)
        if str(current.get('auto_renew_enabled')).lower() not in {'true','1'}:
            raise DomainError('RENEWAL_ALREADY_DISABLED','该合同未开启自动续订或已取消，请查看现有状态',409)
        if current.get('status') in {'cancelled','expired','terminated'}:
            raise DomainError('INVALID_CONTRACT_STATE','合同已终止或到期，请核对原合同',409)
        end=current.get('access_end_date') or None
        basis_line={'selection_basis':selection_basis} if selection_basis else {}
        return dict(contract_id=subject_id,merchant=current['service_name'],access_end_date=end,access_period_state=access_period(end),**basis_line,
            access_note='权益截止日期来自导入快照；已过期权益不会恢复或延长，不承诺当前仍可用',
            source_digest=_digest(_canonical(raw)),provider='local_merchant_simulator',
            impact='取消本地模拟商户下一期续订，保留原权益截止日期；不退款、不提前终止本期权益，也不撤销独立银行代扣授权')
    raise DomainError('INVALID_INPUT','不支持的服务类型',422)


def view(row):
    return dict(id=row.id,kind=row.kind,label=LABELS[row.kind],status=row.status,subject_id=row.subject_id,
        summary=json.loads(row.terms_json),created_at_utc=row.created_at_utc,expires_at_utc=row.expires_at_utc,
        receipt=json.loads(row.receipt_json) if row.receipt_json else None)


def sync(db,row):
    task=db.get(AgentTask,row.agent_task_id) if row.agent_task_id else None
    if task and json.loads(task.context_json).get('service_request_id')==row.id:
        if not task.stop_requested:
            task.status={'pending_confirmation':'awaiting_confirmation','submitted':'completed','executed':'completed',
                'rejected':'completed','invalidated':'gathering_info'}.get(row.status,row.status)
        for step in db.scalars(select(AgentStep).where(AgentStep.task_id==task.id,AgentStep.result_ref==row.id)):
            step.status='awaiting_confirmation' if row.status=='pending_confirmation' else 'succeeded'
            step.result_json=_canonical({'service_request':view(row)})


def ensure_no_pending(db,sid,except_task=None):
    found=db.scalar(select(ServiceRequest).where(ServiceRequest.session_id==sid,ServiceRequest.status=='pending_confirmation',
        ServiceRequest.expires_at_utc>_stamp(_now())))
    if found and (not except_task or found.agent_task_id!=except_task):
        raise DomainError('ACTIVE_WRITE_TASK_EXISTS','请先确认或取消当前服务操作单',409)


@security.preparing
def prepare(db,*,session_id,kind,subject_id,amount_minor=None,agent_task_id=None,selection_basis=None):
    instant=_now()
    with atomic_write(db):
        session,_=_session(db,session_id,instant)
        if agent_task_id:
            task=db.get(AgentTask,agent_task_id)
            if not task or task.session_id!=session.id or task.owner_id!=session.owner_id:raise DomainError('RESOURCE_NOT_FOUND','任务不存在',404)
        source=basis(db,session,kind,subject_id,selection_basis)
        if kind=='credit_limit':
            used=max(source['outstanding_minor'],source['credit_limit_minor']-source['available_credit_minor'],0)
            if type(amount_minor) is not int or not used<=amount_minor<=MAX_CREDIT_MINOR or amount_minor<=0:
                raise DomainError('INVALID_CREDIT_LIMIT','申请额度须不低于档案已占用额度，且不超过本地模拟上限100万元',422)
            if amount_minor==source['credit_limit_minor']:raise DomainError('LIMIT_UNCHANGED','申请额度与当前额度相同，无需提交',409)
        elif amount_minor is not None:raise DomainError('INVALID_INPUT','本业务不接受金额参数',422)
        terms=dict(kind=kind,subject_id=subject_id,requested_limit_minor=amount_minor if kind=='credit_limit' else None,**source)
        digest=_digest(_canonical(terms))
        for old in db.scalars(select(ServiceRequest).where(ServiceRequest.session_id==session.id,ServiceRequest.status=='pending_confirmation')):
            if old.expires_at_utc<=_stamp(instant):old.status='expired';old.token_hash=None;sync(db,old)
            elif old.agent_task_id==agent_task_id and old.terms_digest==digest:return view(old)
            else:raise DomainError('ACTIVE_WRITE_TASK_EXISTS','请先确认或取消当前服务操作单',409)
        from app.data.models import Task
        from app.data.card_models import CardRequest
        from app.data.wealth_models import WealthOrder
        from app.data.subscription_models import SubscriptionRequest
        from app.data.birthday_models import BirthdayRequest
        from app.data.scheduled_models import ScheduledTransfer
        from app.data.collection_models import CollectionRequest
        if db.scalar(select(Task.id).where(Task.active_session_id==session.id)) or any(db.scalar(select(m.id).where(
            m.session_id==session.id,m.status=='pending_confirmation',m.expires_at_utc>_stamp(instant))) for m in
            (CardRequest,WealthOrder,SubscriptionRequest,BirthdayRequest,ScheduledTransfer,CollectionRequest)):
            raise DomainError('ACTIVE_WRITE_TASK_EXISTS','请先确认或取消其他业务操作单',409)
        row=ServiceRequest(id=uuid4().hex,owner_id=session.owner_id,session_id=session.id,dataset_version=session.dataset_version,
            agent_task_id=agent_task_id,kind=kind,subject_id=subject_id,status='pending_confirmation',terms_json=_canonical(terms),
            terms_digest=digest,created_at_utc=_stamp(instant),expires_at_utc=_stamp(instant+timedelta(minutes=5)))
        db.add(row);db.flush();security.audit(db,session.owner_id,'service_prepared',kind);return view(row)


def owned(db,sid,rid):
    session,_=_session(db,sid,_now());row=db.get(ServiceRequest,rid)
    if not row or row.owner_id!=session.owner_id or row.dataset_version!=session.dataset_version:
        raise DomainError('RESOURCE_NOT_FOUND','操作单不存在',404)
    return session,row


def fixed(row):
    if not hmac.compare_digest(row.terms_digest,_digest(row.terms_json)):raise DomainError('REQUEST_CHANGED','固定条款发生变化',409)
    terms=json.loads(row.terms_json)
    if terms['kind']!=row.kind or terms['subject_id']!=row.subject_id:raise DomainError('REQUEST_CHANGED','业务对象与固定条款不一致',409)
    return terms


def pending(db,sid,rid):
    session,row=owned(db,sid,rid)
    if row.session_id!=session.id:raise DomainError('INVALID_ORDER_SESSION','请在原会话确认',409)
    security.assert_pending(row);fixed(row)
    if row.agent_task_id:
        from app.agent.control import assert_running
        assert_running(db,row.agent_task_id)
    return session,row


def issue_token(db,*,session_id,request_id):
    with atomic_write(db):
        _,row=pending(db,session_id,request_id);token=secrets.token_urlsafe(32);row.token_hash=_digest(token)
        return {'confirmation_token':token}


@security.secured('service','request_id')
def confirm(db,*,session_id,request_id,confirmation_token):
    with atomic_write(db):
        session,row=owned(db,session_id,request_id)
        if row.session_id!=session.id:raise DomainError('INVALID_ORDER_SESSION','请在原会话确认',409)
        if row.status in {'executed','submitted','rejected'}:return view(row)
        session,row=pending(db,session_id,request_id);terms=fixed(row)
        if not isinstance(confirmation_token,str) or not row.token_hash or not hmac.compare_digest(row.token_hash,_digest(confirmation_token)):
            raise DomainError('CONFIRMATION_INVALID','确认凭据无效',403)
        current=basis(db,session,row.kind,row.subject_id)
        if any(terms.get(k)!=v for k,v in current.items()):raise DomainError('STALE_RESOURCE','业务对象状态已变化，请重新核对固定单',409)
        instant=_now();receipt=dict(request_id=row.id,kind=row.kind,provider='local_simulator')
        if row.kind=='credit_limit':
            row.status='submitted';receipt.update(message='调额申请已受理，等待独立模拟审批；当前授信额度未改变',requested_limit_minor=terms['requested_limit_minor'])
        elif row.kind=='replace_card':
            from app.domain.card_management import owned_card,controls
            from app.data.financial_models import FinancialDatasetRow
            old=owned_card(db,session.owner_id,row.subject_id)
            used=set(db.scalars(select(Card.card_last4)))|set(db.scalars(select(func.json_extract(FinancialDatasetRow.payload_json,'$.card_last4')).where(FinancialDatasetRow.table_name=='cards')))
            tail=next((str(n) for n in range(7000,10000) if str(n) not in used),None)
            if tail is None:raise DomainError('CARD_CAPACITY','模拟卡编号容量不足',409)
            card=Card(id=uuid4().hex,owner_id=session.owner_id,account_id=old.account_id,alias=old.alias+'（补发）',
                brand=old.brand,card_last4=tail,status=old.status,version=1)
            db.add(card);db.flush();c=controls(db,old)
            db.add(CardControl(card_id=card.id,limit_minor=c['limit_minor'],online_enabled=int(c['online_enabled']),lost=0,version=1))
            old.status='frozen';old.version+=1;row.status='executed'
            receipt.update(message='模拟借记卡已补发，原卡永久停用；冻结、支付限制和限额保留，代扣未迁移',
                old_card_mask=terms['card_mask'],new_card_id=card.id,new_card_mask='**'+tail,account_id=card.account_id)
        else:
            db.add(MerchantContractState(dataset_version=session.dataset_version,contract_id=row.subject_id,owner_id=session.owner_id,
                access_end_date=terms['access_end_date'],cancelled_at_utc=_stamp(instant),request_id=row.id))
            row.status='executed';receipt.update(message='本地模拟商户已取消下期续订；原权益截止日期保留，未退款、未撤销银行代扣'+
                ('；档案权益截止日期已过，没有恢复或延长权益' if terms['access_period_state']=='expired' else ''),
                contract_id=row.subject_id,merchant=terms['merchant'],access_end_date=terms['access_end_date'],access_period_state=terms['access_period_state'],auto_renew_enabled=False)
        row.token_hash=None;row.completed_at_utc=_stamp(instant);receipt.update(status=row.status,completed_at_utc=row.completed_at_utc)
        row.receipt_json=_canonical(receipt);db.flush();security.audit(db,session.owner_id,'service_confirmed',row.kind);sync(db,row);return view(row)


def decide(db,*,session_id,request_id,decision,reason):
    if decision not in {'approve','reject'} or not isinstance(reason,str) or not reason.strip() or len(reason)>200:
        raise DomainError('INVALID_INPUT','须选择审批结果并填写简短理由',422)
    with atomic_write(db):
        session,row=owned(db,session_id,request_id);security.assert_unlocked(db,session.owner_id)
        if row.kind!='credit_limit':raise DomainError('INVALID_ORDER_STATE','此入口仅审批调额申请',409)
        if row.status in {'executed','rejected'}:
            if (row.status=='executed')!=(decision=='approve'):raise DomainError('DECISION_CONFLICT','审批结果已经固定，不能改写',409)
            return view(row)
        if row.status!='submitted':raise DomainError('INVALID_ORDER_STATE','须先由本人强验证并提交申请',409)
        terms=fixed(row)
        if decision=='approve':
            current=basis(db,session,row.kind,row.subject_id)
            if any(terms.get(k)!=v for k,v in current.items()):raise DomainError('STALE_RESOURCE','原额度或档案已变化，请重新申请',409)
            facility=db.get(CreditFacility,(session.dataset_version,row.subject_id))
            if not facility:
                facility=CreditFacility(dataset_version=session.dataset_version,card_id=row.subject_id,owner_id=session.owner_id,
                    source_limit_minor=terms['credit_limit_minor'],limit_minor=terms['credit_limit_minor'],version=0,updated_at_utc=_stamp(_now()));db.add(facility)
            facility.limit_minor=terms['requested_limit_minor'];facility.version+=1;facility.updated_at_utc=_stamp(_now());row.status='executed'
        else:row.status='rejected'
        row.completed_at_utc=_stamp(_now());row.receipt_json=_canonical(dict(request_id=row.id,kind=row.kind,status=row.status,
            message='独立模拟审批已通过，模拟授信额度已更新' if decision=='approve' else '独立模拟审批未通过，授信额度保持原值',
            requested_limit_minor=terms['requested_limit_minor'],reason=reason.strip(),completed_at_utc=row.completed_at_utc))
        db.flush();security.audit(db,session.owner_id,'credit_limit_'+decision,'service');sync(db,row);return view(row)


def cancel(db,*,session_id,request_id):
    with atomic_write(db):
        session,row=owned(db,session_id,request_id)
        if row.session_id!=session.id:raise DomainError('INVALID_ORDER_SESSION','请在原会话取消',409)
        if row.status=='pending_confirmation':row.status='cancelled';row.token_hash=None;sync(db,row)
        return view(row)


def invalidate_task(db,task):
    for row in db.scalars(select(ServiceRequest).where(ServiceRequest.agent_task_id==task.id,ServiceRequest.status=='pending_confirmation')):
        row.status='invalidated';row.token_hash=None;sync(db,row)


def task_state(db,task):
    rid=json.loads(task.context_json).get('service_request_id');row=db.get(ServiceRequest,rid) if rid else None
    if row and row.owner_id==task.owner_id and row.session_id==task.session_id:
        if row.status=='pending_confirmation' and row.expires_at_utc<=_stamp(_now()):row.status='expired';row.token_hash=None
        sync(db,row);return {'service_request':view(row)}
    return {'service_request':None}


def records(db,*,session_id,kind=None):
    session,_=_session(db,session_id,_now())
    protected=False
    try:security.assert_subscription_access(db,session.id);protected=True
    except DomainError as error:
        if error.code!='SUBSCRIPTION_CONFIRMATION_REQUIRED' or kind=='cancel_contract':raise
    query=select(ServiceRequest).where(ServiceRequest.owner_id==session.owner_id,ServiceRequest.dataset_version==session.dataset_version)
    if kind:query=query.where(ServiceRequest.kind==kind)
    if not protected:query=query.where(ServiceRequest.kind!='cancel_contract')
    rows=list(db.scalars(query.order_by(ServiceRequest.created_at_utc.desc(),ServiceRequest.id.desc()).limit(100)))
    return {'items':[view(r) for r in rows],'limit':100,'contract_records_require_query_confirmation':not protected}
