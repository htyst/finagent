"""Read-only credit limits from the current user's imported financial snapshot."""
import re
from app.domain.errors import DomainError
from app.domain.operations import _session,_now
from app.domain.financial_profile import rows,dataset_manifest
from app.data.models import DemoSetting

AMOUNTS=('credit_limit_minor','outstanding_minor','available_credit_minor',
         'statement_balance_minor','unbilled_balance_minor','minimum_payment_due_minor')


def amount(raw,key):
    value=raw.get(key)
    if value is None or value=='':return None
    if isinstance(value,bool) or not re.fullmatch(r'-?\d+',str(value)):
        raise DomainError('DATA_INCONSISTENT','信用卡档案金额格式异常，未提供推算金额',409)
    return int(value)


def read(db,*,session_id,card=None):
    session,_=_session(db,session_id,_now())
    archive=rows(db,session.owner_id,'cards')
    cards=[r for r in archive if r.get('card_type')=='credit']
    if card is not None:
        from app.domain.financial_queries import resolve_card_reference
        chosen=resolve_card_reference(db,session.owner_id,card)
        if chosen['card_type']!='credit':
            raise DomainError('INVALID_CARD_TYPE','该卡为借记卡，信用额度查询仅适用于信用卡',422)
        cards=[r for r in cards if r['id']==chosen['id']]
    setting=db.get(DemoSetting,1)
    manifest=dataset_manifest(db) or {}
    snapshot=manifest.get('as_of') or manifest.get('snapshot_as_of') or manifest.get('period_end') or setting.demo_date
    statements=rows(db,session.owner_id,'credit_card_statements')
    result=[]
    for raw in cards:
        from app.domain.servicing import credit_projection
        raw=credit_projection(db,session.owner_id,raw)
        eligible=[s for s in statements if s.get('card_id')==raw['id'] and s.get('statement_date') and s['statement_date']<=snapshot[:10]]
        latest=max(eligible,key=lambda s:(s['statement_date'],s.get('statement_id',''))) if eligible else None
        result.append({'card_id':raw['id'],'alias':raw.get('alias'),'bank_name':raw.get('bank_name'),
            'card_mask':'**'+raw['card_last4'],'status':raw.get('status'),'read_only':True,'limit_origin':raw.get('limit_origin','imported_snapshot'),'limit_updated_at_utc':raw.get('limit_updated_at_utc'),
            **{k:amount(raw,k) for k in AMOUNTS},
            'statement_day':raw.get('statement_day') or None,'due_day':raw.get('due_day') or None,
            'repayment_status':raw.get('repayment_status') or None,
            'latest_statement':{'statement_id':latest.get('statement_id'),'statement_date':latest['statement_date'],
                'due_date':latest.get('due_date') or None,'status':latest.get('status'),
                'statement_balance_minor':amount(latest,'statement_balance_minor'),'payment_minor':amount(latest,'payment_minor')} if latest else None})
    return {'credit_cards':result,'recorded_card_count':len(result),'dataset_version':session.dataset_version,
        'archive_available':bool(archive),'snapshot_as_of':snapshot,'scope':'imported_credit_card_snapshot',
        'currency':'CNY','queried_at_utc':_now().isoformat(),
        'limitations':['欠款和账单为导入快照，额度可含独立审批的本地模拟调整；可用额度按原快照加审批差额展示，不是实时可刷金额',
            'outstanding_minor为档案欠款口径，不自行推算包含待入账占额的实时已用额度；可用额度直接取档案',
            '缺失金额和日期为未知，不填零、不根据账单日推算还款日',
            '最新账单为快照日之前的最近记录，不据此认定当前逾期；查询不提交授信调整或还款']}
