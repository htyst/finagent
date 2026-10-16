"""Deterministic simulated suitability and atomic, confirmed buy/redemption settlement."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hmac
import json
import re
import secrets
from uuid import uuid4
from sqlalchemy import select
from app.data.atomic import atomic_write
from app.data.models import AgentTask, AgentStep, AuditEvent, Task, User
from app.data.wealth_models import WealthProduct, RiskAssessment, WealthPosition, WealthOrder
from app.domain.errors import DomainError
from app.domain.ledger import owned_account, post_entry, available_balance
from app.domain.money import MAX_MINOR
from app.domain.operations import _session, _now, _digest, _canonical, _stamp

PRODUCT_DATA = [
    dict(id='P1',name='灵活型',risk_level=1,min_minor=10000,lock_days=0,nav_minor=100,fee_minor=0,
         yield_text='测试收益展示值1.5%，非保证收益，不承诺保本',version=1),
    dict(id='P2',name='定期型',risk_level=2,min_minor=100000,lock_days=90,nav_minor=100,fee_minor=0,
         yield_text='测试收益展示值2.5%，非保证收益，不承诺保本',version=1),
    dict(id='P3',name='波动型',risk_level=4,min_minor=10000,lock_days=0,nav_minor=100,fee_minor=0,
         yield_text='历史区间收益−10%至+15%，不代表未来收益，不承诺保本',version=1),
]
QUESTIONS = [
    dict(label='投资经验',options=['没有投资经验','有少量经验','有丰富经验']),
    dict(label='可接受本金损失',options=['不能接受损失','可接受少量损失','可接受较大损失']),
    dict(label='资金使用期限',options=['一个月内可能用到','三个月内可能用到','一年以上不用']),
    dict(label='收入与应急资金',options=['应急资金不足或收入不稳定','有一定备用金','收入稳定且备用金充足']),
]
RULE='demo-risk-v1'
SETTLEMENT='授权时同步受理并结算；净值与费用以固定订单为准；无测试期间收益'

def seed_products(db):
    for item in PRODUCT_DATA:
        if db.get(WealthProduct,item['id']) is None: db.add(WealthProduct(**item))
    db.flush()

def product_view(row):
    return {key:getattr(row,key) for key in ('id','name','risk_level','min_minor','nav_minor','fee_minor','lock_days','yield_text','version')}

def assessment_view(row):
    if row is None:return None
    return dict(id=row.id,max_risk=row.max_risk,score=row.score,rule_version=row.rule_version,
                source=row.source,created_at_utc=row.created_at_utc,expires_at_utc=row.expires_at_utc,
                scope=f'仅本地模拟产品；可办理风险等级不超过R{row.max_risk}的产品，仍需满足金额和期限规则')

def current_assessment(db,owner_id,now=None):
    instant=_now(now)
    row=db.scalar(select(RiskAssessment).where(RiskAssessment.owner_id==owner_id)
                  .order_by(RiskAssessment.created_at_utc.desc(),RiskAssessment.id.desc()))
    return row if row and datetime.fromisoformat(row.expires_at_utc)>instant else None

def save_assessment(db,*,owner_id,answers,source='questionnaire',now=None):
    instant=_now(now)
    if not isinstance(answers,list) or len(answers)!=4 or any(type(a) is not int or a not in (0,1,2) for a in answers):
        raise DomainError('INVALID_INPUT','请完整回答四个风险评估问题，每题选择一个实际答案')
    if db.get(User,owner_id) is None:raise DomainError('RESOURCE_NOT_FOUND','资源不存在',404)
    score=sum(answers)
    row=RiskAssessment(id=uuid4().hex,owner_id=owner_id,answers_json=_canonical(answers),score=score,
                       max_risk=1 if score<=2 else 2 if score<=5 else 4,rule_version=RULE,source=source,
                       created_at_utc=_stamp(instant),expires_at_utc=_stamp(instant+timedelta(days=365)))
    db.add(row)
    for draft in db.scalars(select(WealthOrder).where(WealthOrder.owner_id==owner_id,WealthOrder.status=='pending_confirmation')):
        draft.status='invalidated';draft.token_hash=None;_sync_task(db,draft)
    db.flush()
    return assessment_view(row)

def submit_assessment(db,*,session_id,answers,now=None):
    instant=_now(now)
    with atomic_write(db):
        session,_=_session(db,session_id,instant)
        result=save_assessment(db,owner_id=session.owner_id,answers=answers,now=instant)
        db.add(AuditEvent(id=uuid4().hex,owner_id=session.owner_id,event='risk_assessment_completed',created_at_utc=_stamp(instant)))
        return result

def decimal_hundredths(value,label):
    if not isinstance(value,str) or not re.fullmatch(r'[0-9]{1,20}(?:\.[0-9]{1,2})?',value):
        raise DomainError('INVALID_INPUT',f'{label}须为正数十进制，最多两位小数')
    try: number=Decimal(value)
    except InvalidOperation as e:raise DomainError('INVALID_INPUT',f'{label}格式无效') from e
    if not number.is_finite() or number<=0 or number.as_tuple().exponent < -2 or number*100>MAX_MINOR:
        raise DomainError('INVALID_INPUT',f'{label}须为大于0且最多两位小数的数值')
    return int(number*100)

def compare_products(db,*,owner_id,budget_minor,horizon_days,now=None):
    if type(budget_minor) is not int or not 0<budget_minor<=MAX_MINOR or type(horizon_days) is not int or not 1<=horizon_days<=3650:
        raise DomainError('INVALID_INPUT','请提供明确预算和可能用款的天数')
    assessment=current_assessment(db,owner_id,now)
    if assessment is None:raise DomainError('RISK_ASSESSMENT_REQUIRED','请先完成风险评估，不能用自述风险偏好替代持久评估',409)
    rows=[]
    for product in db.scalars(select(WealthProduct).order_by(WealthProduct.id)):
        reasons=[]
        if budget_minor<product.min_minor:reasons.append('预算低于起购额')
        if product.lock_days>horizon_days:reasons.append(f'锁定{product.lock_days}天，与{horizon_days}天内可能用款冲突')
        if product.risk_level>assessment.max_risk:reasons.append(f'风险R{product.risk_level}不匹配评估R{assessment.max_risk}')
        rows.append({**product_view(product),'matches':not reasons,'reasons':reasons,'redemption':f'锁定期{product.lock_days}天内不可赎回' if product.lock_days else '无固定锁定期，按模拟订单同步结算'})
    return dict(assessment=assessment_view(assessment),budget_minor=budget_minor,horizon_days=horizon_days,
                products=rows,recommended_ids=[r['id'] for r in rows if r['matches']],
                yield_notice='展示收益与历史收益均非保证收益；不承诺未来收益或保本')

def _position(db,owner,product):
    return db.scalar(select(WealthPosition).where(WealthPosition.owner_id==owner,WealthPosition.product_id==product))

def ensure_no_pending_wealth(db,session_id,instant):
    from app.domain.card_management import ensure_no_pending
    ensure_no_pending(db,session_id,instant)
    if db.scalar(select(WealthOrder.id).where(WealthOrder.session_id==session_id,
                  WealthOrder.status=='pending_confirmation',WealthOrder.expires_at_utc>_stamp(instant))):
        raise DomainError('ACTIVE_WRITE_TASK_EXISTS','请先确认或取消当前理财订单',409)

def _owned_order(db,session,order_id):
    row=db.scalar(select(WealthOrder).where(WealthOrder.id==order_id,WealthOrder.owner_id==session.owner_id,
                  WealthOrder.dataset_version==session.dataset_version))
    if row is None:raise DomainError('RESOURCE_NOT_FOUND','资源不存在',404)
    return row

def _draft_session(row,session):
    if row.session_id!=session.id:raise DomainError('INVALID_ORDER_SESSION','请在准备订单的原会话确认；其他会话仅能查看',409)

def order_view(row,now=None):
    instant=_now(now)
    status='expired' if row.status=='pending_confirmation' and datetime.fromisoformat(row.expires_at_utc)<=instant else row.status
    return dict(id=row.id,kind=row.kind,status=status,product_id=row.product_id,account_id=row.account_id,
                amount_minor=row.amount_minor,units_hundredths=row.units_hundredths,expires_at_utc=row.expires_at_utc,
                summary=json.loads(row.terms_json),created_at_utc=row.created_at_utc,
                accepted_at_utc=row.accepted_at_utc,settled_at_utc=row.settled_at_utc,
                receipt=json.loads(row.receipt_json) if row.receipt_json else None)

def portfolio(db,*,session_id,now=None):
    instant=_now(now);session,_=_session(db,session_id,instant)
    return dict(assessment=assessment_view(current_assessment(db,session.owner_id,instant)),questions=QUESTIONS,
      assessment_rule='四题每题0至2分；总分0至2适配R1，3至5适配R2，6至8适配R4，有效365天；只用于模拟产品',
      products=[product_view(r) for r in db.scalars(select(WealthProduct).order_by(WealthProduct.id))],
      positions=[dict(id=r.id,product_id=r.product_id,units_hundredths=r.units_hundredths,acquired_at_utc=r.acquired_at_utc) for r in db.scalars(select(WealthPosition).where(WealthPosition.owner_id==session.owner_id))],
      orders=[order_view(r,instant) for r in db.scalars(select(WealthOrder).where(WealthOrder.owner_id==session.owner_id,WealthOrder.dataset_version==session.dataset_version).order_by(WealthOrder.created_at_utc.desc(),WealthOrder.id).limit(100))])

def _check_trade(db,owner,product,account,kind,amount,units,instant):
    assessment=current_assessment(db,owner,instant)
    if assessment is None:raise DomainError('RISK_ASSESSMENT_REQUIRED','请先完成有效风险评估',409)
    if product.risk_level>assessment.max_risk:raise DomainError('RISK_MISMATCH','产品风险等级超出当前评估适用范围',409)
    position=_position(db,owner,product.id)
    if kind=='buy':
        if amount<product.min_minor:raise DomainError('BELOW_MINIMUM','申购金额低于起购金额',409)
        if available_balance(db,account)<amount+product.fee_minor:raise DomainError('INSUFFICIENT_FUNDS','账户一般可用余额不足，预留专款不能用于申购',409)
    else:
        if position is None or position.units_hundredths<units:raise DomainError('INSUFFICIENT_HOLDING','持仓不足，不能赎回超过剩余份额的数量',409)
        if datetime.fromisoformat(position.acquired_at_utc)+timedelta(days=product.lock_days)>instant:
            raise DomainError('PRODUCT_LOCKED','持仓仍在锁定期，不能赎回',409)
        if amount<=product.fee_minor:raise DomainError('INVALID_INPUT','赎回金额不足以支付费用',409)
    return assessment,position

from app.domain.security import preparing

@preparing
def prepare_order(db,*,session_id,kind,product_id,account_id,amount_minor=None,units_hundredths=None,agent_task_id=None,now=None):
    if kind not in ('buy','redeem'):raise DomainError('INVALID_INPUT','未知理财交易类型')
    value=amount_minor if kind=='buy' else units_hundredths
    if type(value) is not int or not 0<value<=MAX_MINOR:raise DomainError('INVALID_INPUT','金额或份额须为正整数最小单位')
    instant=_now(now)
    with atomic_write(db):
        session,_=_session(db,session_id,instant)
        from app.domain.card_management import ensure_no_pending
        ensure_no_pending(db,session.id,instant)
        product=db.get(WealthProduct,product_id)
        if product is None:raise DomainError('RESOURCE_NOT_FOUND','模拟产品不存在',404)
        account=owned_account(db,session.owner_id,account_id)
        if agent_task_id:
            task=db.get(AgentTask,agent_task_id)
            if task is None or task.session_id!=session.id or task.owner_id!=session.owner_id or task.stop_requested:
                raise DomainError('INVALID_TASK_STATE','当前对话不能准备理财交易',409)
        if kind=='buy':
            if amount_minor*100 % product.nav_minor:raise DomainError('INVALID_INPUT','净值换算后份额超出0.01精度')
            units_hundredths=amount_minor*100//product.nav_minor
        else:
            if units_hundredths*product.nav_minor % 100:raise DomainError('INVALID_INPUT','份额换算后金额超出分币精度')
            amount_minor=units_hundredths*product.nav_minor//100
        if not 0<amount_minor<=MAX_MINOR or not 0<units_hundredths<=MAX_MINOR:
            raise DomainError('INVALID_INPUT','金额或份额超出允许范围')
        assessment,position=_check_trade(db,session.owner_id,product,account,kind,amount_minor,units_hundredths,instant)
        if kind=='buy' and (position.units_hundredths if position else 0)+units_hundredths>MAX_MINOR:
            raise DomainError('INVALID_INPUT','累计持仓超出允许范围')
        terms=dict(kind=kind,product=product_view(product),account=dict(id=account.id,alias=account.alias,mask='**'+account.account_last4,version=account.version),
                   assessment_id=assessment.id,amount_minor=amount_minor,units_hundredths=units_hundredths,
                   position_version=position.version if position else 0,position_units=position.units_hundredths if position else 0,
                   fee_minor=product.fee_minor,settlement=SETTLEMENT)
        digest=_digest(_canonical(terms))
        pending=list(db.scalars(select(WealthOrder).where(WealthOrder.session_id==session.id,WealthOrder.status=='pending_confirmation')))
        for old in pending:
            if datetime.fromisoformat(old.expires_at_utc)<=instant:old.status='expired';old.token_hash=None;_sync_task(db,old);continue
            if old.terms_digest==digest and old.agent_task_id==agent_task_id:return order_view(old,instant)
            if not agent_task_id or old.agent_task_id!=agent_task_id:raise DomainError('ACTIVE_WRITE_TASK_EXISTS','请先确认或取消当前理财订单',409)
            old.status='invalidated';old.token_hash=None;_sync_task(db,old)
        from app.data.scheduled_models import ScheduledTransfer
        from app.data.collection_models import CollectionRequest
        if db.scalar(select(Task.id).where(Task.active_session_id==session.id)) or any(db.scalar(select(model.id).where(model.session_id==session.id,model.status=='pending_confirmation',model.expires_at_utc>_stamp(instant))) for model in (ScheduledTransfer,CollectionRequest)):
            raise DomainError('ACTIVE_WRITE_TASK_EXISTS','请先确认或取消当前其他操作草稿',409)
        row=WealthOrder(id=uuid4().hex,owner_id=session.owner_id,session_id=session.id,dataset_version=session.dataset_version,
          agent_task_id=agent_task_id,product_id=product.id,account_id=account.id,assessment_id=assessment.id,kind=kind,
          amount_minor=amount_minor,units_hundredths=units_hundredths,terms_json=_canonical(terms),terms_digest=digest,
          status='pending_confirmation',created_at_utc=_stamp(instant),expires_at_utc=_stamp(instant+timedelta(minutes=5)))
        db.add(row);db.flush()
        db.add(AuditEvent(id=uuid4().hex,owner_id=session.owner_id,event='wealth_order_prepared',created_at_utc=_stamp(instant)))
        return order_view(row,instant)

def _fixed(row):
    if not hmac.compare_digest(row.terms_digest,_digest(row.terms_json)):
        raise DomainError('ORDER_CHANGED','订单固定条款已变化，请重新准备',409)
    terms=json.loads(row.terms_json)
    if any(getattr(row,key)!=terms[key] for key in ('kind','amount_minor','units_hundredths','assessment_id')) or row.product_id!=terms['product']['id'] or row.account_id!=terms['account']['id']:
        raise DomainError('ORDER_CHANGED','订单字段与固定条款不一致，请重新准备',409)

def issue_token(db,*,session_id,order_id,now=None):
    instant=_now(now)
    with atomic_write(db):
        session,_=_session(db,session_id,instant);row=_owned_order(db,session,order_id);_draft_session(row,session)
        if row.status!='pending_confirmation' or datetime.fromisoformat(row.expires_at_utc)<=instant:
            raise DomainError('INVALID_ORDER_STATE','订单不再等待确认或已过期',409)
        _fixed(row);token=secrets.token_urlsafe(32);row.token_hash=_digest(token)
        return dict(confirmation_token=token,expires_at_utc=row.expires_at_utc)

def _sync_task(db,row):
    if not row.agent_task_id:return
    task=db.get(AgentTask,row.agent_task_id)
    if task:
        context=json.loads(task.context_json)
        if context.get('wealth_order_id')==row.id and (task.intent or '').startswith('wealth_'):
            status={'settled':'completed','pending_confirmation':'awaiting_confirmation','invalidated':'gathering_info'}.get(row.status,row.status)
            if task.status!=status:
                task.status=status
                task.updated_at_utc=_stamp(_now())
        for step in db.scalars(select(AgentStep).where(AgentStep.task_id==task.id,AgentStep.result_ref==row.id)):
            step.status={'settled':'succeeded','pending_confirmation':'awaiting_confirmation'}.get(row.status,row.status)
            step.result_json=_canonical(dict(order=order_view(row)))

from app.domain.security import secured

@secured('wealth', 'order_id')
def confirm_order(db,*,session_id,order_id,confirmation_token,now=None):
    instant=_now(now)
    with atomic_write(db):
        session,_=_session(db,session_id,instant);row=_owned_order(db,session,order_id);_draft_session(row,session)
        if row.status=='settled':return order_view(row,instant)
        if row.status!='pending_confirmation' or datetime.fromisoformat(row.expires_at_utc)<=instant:raise DomainError('INVALID_ORDER_STATE','订单已失效或过期',409)
        _fixed(row)
        if not isinstance(confirmation_token,str) or not row.token_hash or not hmac.compare_digest(row.token_hash,_digest(confirmation_token)):
            raise DomainError('CONFIRMATION_INVALID','确认凭据无效',403)
        terms=json.loads(row.terms_json);product=db.get(WealthProduct,row.product_id);account=owned_account(db,session.owner_id,row.account_id)
        assessment,position=_check_trade(db,session.owner_id,product,account,row.kind,row.amount_minor,row.units_hundredths,instant)
        if assessment.id!=row.assessment_id or product_view(product)!=terms['product'] or account.version!=terms['account']['version'] or (position.version if position else 0)!=terms['position_version']:
            raise DomainError('STALE_RESOURCE','评估、产品、账户或持仓已变化，请重新准备订单',409)
        row.status='accepted';row.accepted_at_utc=_stamp(instant)
        # accepted is durable only with successful settlement; no partial funds or units can commit.
        total=row.amount_minor+product.fee_minor if row.kind=='buy' else row.amount_minor-product.fee_minor
        tx=post_entry(db,owner_id=session.owner_id,account_id=account.id,kind='wealth_buy' if row.kind=='buy' else 'wealth_redeem',
                      direction='debit' if row.kind=='buy' else 'credit',amount_minor=total,business_at=instant,
                      merchant=f'{product.id} {product.name}（模拟理财）',business_id=row.id,system_now=instant)
        if row.kind=='buy':
            if position is None:
                position=WealthPosition(id=uuid4().hex,owner_id=session.owner_id,product_id=product.id,units_hundredths=0,version=0,acquired_at_utc=_stamp(instant));db.add(position)
            position.units_hundredths+=row.units_hundredths
            position.acquired_at_utc=_stamp(instant) # Combined position conservatively locks until latest acquisition.
        else:position.units_hundredths-=row.units_hundredths
        position.version+=1;row.status='settled';row.settled_at_utc=_stamp(_now());row.token_hash=None
        row.receipt_json=_canonical(dict(order_id=row.id,transaction_id=tx.id,status='settled',amount_minor=row.amount_minor,
          fee_minor=product.fee_minor,units_hundredths=row.units_hundredths,remaining_units_hundredths=position.units_hundredths,
          balance_minor=account.balance_minor,accepted_at_utc=row.accepted_at_utc,settled_at_utc=row.settled_at_utc,
          message='模拟申购已受理并结算' if row.kind=='buy' else '模拟赎回已结算，资金已到账'))
        db.add(AuditEvent(id=uuid4().hex,owner_id=session.owner_id,event='wealth_order_settled',created_at_utc=_stamp(instant)))
        db.flush();_sync_task(db,row)
        return order_view(row,instant)

def cancel_order(db,*,session_id,order_id,now=None):
    instant=_now(now)
    with atomic_write(db):
        session,_=_session(db,session_id,instant);row=_owned_order(db,session,order_id)
        if row.status=='pending_confirmation':
            _draft_session(row,session);row.status='cancelled';row.token_hash=None;_sync_task(db,row)
            if row.agent_task_id:
                task=db.get(AgentTask,row.agent_task_id)
                if task and json.loads(task.context_json).get('wealth_order_id')==row.id:
                    task.stop_requested=True
        return order_view(row,instant)

def invalidate_session_orders(db,session_id):
    for row in db.scalars(select(WealthOrder).where(WealthOrder.session_id==session_id,WealthOrder.status=='pending_confirmation')):
        row.status='invalidated';row.token_hash=None;_sync_task(db,row)
        if row.agent_task_id:
            task=db.get(AgentTask,row.agent_task_id)
            if task:task.stop_requested=True

def invalidate_task_order(db,task):
    context=json.loads(task.context_json)
    row=db.get(WealthOrder,context.get('wealth_order_id')) if context.get('wealth_order_id') else None
    if row and row.status=='pending_confirmation':row.status='invalidated';row.token_hash=None;_sync_task(db,row)

def task_state(db,task):
    context=json.loads(task.context_json)
    row=db.get(WealthOrder,context.get('wealth_order_id')) if context.get('wealth_order_id') else None
    if row and row.owner_id==task.owner_id and row.session_id==task.session_id:
        if row.status=='pending_confirmation' and datetime.fromisoformat(row.expires_at_utc)<=_now():
            row.status='expired';row.token_hash=None;_sync_task(db,row)
        _sync_task(db,row)
        return {'wealth_order':order_view(row)}
    return {'wealth_order':None}
