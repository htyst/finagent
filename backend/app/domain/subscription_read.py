"""Contract, renewal and paid-cost facts behind the existing yellow read gate."""
from datetime import datetime,timezone
from decimal import Decimal,ROUND_HALF_UP
from app.domain.errors import DomainError

def minor(value):return int(value or 0)

def report(db,session_id,*,start=None,end=None,subscription_id=None,status=None,limit=50,offset=0,contract_limit=50,contract_offset=0):
    from app.domain.subscriptions import get_portfolio
    from app.domain.financial_profile import dataset_manifest
    from app.domain.clock import parse_day
    from app.domain.operations import _session,_now
    if not 1<=limit<=100 or offset<0:raise DomainError('INVALID_INPUT','订阅分页参数无效')
    if not 1<=contract_limit<=100 or contract_offset<0:raise DomainError('INVALID_INPUT','合同分页参数无效')
    session,_=_session(db,session_id,_now());data=get_portfolio(db,session_id)
    manifest=dataset_manifest(db) or {};start=start or manifest.get('period_start');end=end or manifest.get('period_end')
    if start:parse_day(start)
    if end:parse_day(end)
    if start and end and start>end:raise DomainError('INVALID_INPUT','开始日期不能晚于结束日期')
    contracts=[dict(c) for c in data['imported']['contracts'] if not subscription_id or c['subscription_id']==subscription_id]
    if not data['imported']['contracts']:
        contracts=[dict(subscription_id=a['id'],service_name=a['merchant'],billing_cycle='monthly',cycle_months=1,current_price_minor=a['amount_minor'],
            status='active' if a['status']=='active' else 'canceled',auto_renew_enabled=a['status']=='active',native_authorization_id=a['id'],
            next_billing_date=a['next_charge_at_utc'][:10],access_end_date=None,source='native_bank_mandate_only') for a in data['authorizations'] if not subscription_id or a['id']==subscription_id]
    if subscription_id and not contracts:raise DomainError('RESOURCE_NOT_FOUND','订阅合同不存在或不属于当前用户',404)
    contract_ids={r['subscription_id'] for r in contracts}
    billing=[dict(b) for b in data['imported']['billing_events'] if (not subscription_id or b['subscription_id']==subscription_id)
             and (not start or b['scheduled_date']>=start) and (not end or b['scheduled_date']<=end)]
    links={a['id']:a for a in data['authorizations']}
    native_to_contract={c.get('native_authorization_id'):c['subscription_id'] for c in contracts}
    for charge in data['charges']:
        # Native posted charges are new events, not duplicated historic billing rows.
        cid=native_to_contract.get(charge['authorization_id'])
        if not cid:continue
        day=charge['due_at_utc'][:10]
        if (start and day<start) or (end and day>end):continue
        billing.append(dict(billing_event_id='native:'+charge['id'],subscription_id=cid,scheduled_date=day,
            amount_minor=charge['amount_minor'],status='paid' if charge['status']=='posted' else charge['status'],failure_code=charge['error_code'],cash_transaction_id=charge['transaction_id'],liability_event_id=None))
    for b in billing:b['service_name']=next((c['service_name'] for c in contracts if c['subscription_id']==b['subscription_id']),b['subscription_id'])
    active=[c for c in contracts if str(c['auto_renew_enabled']).lower()=='true' and c['status'] not in {'paused','canceled','cancelled'}]
    cost=sum((Decimal(c['current_price_minor'])/Decimal(c['cycle_months']) for c in active),Decimal(0))
    total_cost=int(cost.quantize(Decimal('1'),rounding=ROUND_HALF_UP))
    paid=[b for b in billing if b['status']=='paid'];failed=[b for b in billing if b['status']=='failed']
    grouped={}
    for c in contracts:
        key=c.get('duplicate_service_group')
        if key:grouped.setdefault(key,[]).append(c['subscription_id'])
    for c in contracts:
        c['paid_total_minor']=sum(minor(b['amount_minor']) for b in paid if b['subscription_id']==c['subscription_id'])
        c['failed_attempt_count']=sum(b['subscription_id']==c['subscription_id'] for b in failed)
        c['monthly_equivalent_minor']=int((Decimal(c['current_price_minor'])/Decimal(c['cycle_months'])).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
    filtered=[b for b in billing if not status or b['status']==status]
    if status and status not in {'paid','failed','blocked'}:raise DomainError('INVALID_INPUT','扣费状态须为paid、failed或blocked')
    filtered.sort(key=lambda b:(b['scheduled_date'],b['billing_event_id']),reverse=True)
    failed.sort(key=lambda b:(b['scheduled_date'],b['billing_event_id']),reverse=True)
    failure_page=failed[offset:offset+limit] if status=='failed' else failed[:limit]
    return {'as_of':data['imported']['as_of'],'dataset_version':session.dataset_version,'period':{'start_date':start,'end_date':end},
        'contracts':contracts[contract_offset:contract_offset+contract_limit],'contracts_total':len(contracts),'contract_limit':contract_limit,'contract_offset':contract_offset,'has_more_contracts':contract_offset+contract_limit<len(contracts),
        'summary':{'contract_count':len(contracts),'auto_renew_contract_count':len(active),'auto_renew_monthly_minor':total_cost,
            'paid_total_minor':sum(minor(b['amount_minor']) for b in paid),'paid_attempt_count':len(paid),'failed_attempt_count':len(failed),
            'trial_contracts':[c['subscription_id'] for c in contracts if c['status']=='trial'],
            'duplicate_service_groups':{k:v for k,v in grouped.items() if len(v)>1}},
        'failed_attempts':failure_page,'has_more_failures':len(failed)>len(failure_page),'billing_events':filtered[offset:offset+limit],'billing_total':len(filtered),'limit':limit,'offset':offset,'has_more':offset+limit<len(filtered),
        'authorizations':data['authorizations'],'notifications':data['notifications'],'requests':data['requests'],'reminders':data['reminders'],'charges':data['charges'],
        'cost_basis':'合成服务合同自动续费价格折月；失败不计实付；银行代扣解除不等于取消商户合同；这不是未来保证扣款金额',
        'rules':data['rules']}

def summary_text(result):
    s=result['summary'];lines=[f"订阅合同共{s['contract_count']}份；开启商户自动续费{s['auto_renew_contract_count']}份，折算月均{s['auto_renew_monthly_minor']/100:.2f}元。",
        f"查询期间已付{s['paid_attempt_count']}次、合计{s['paid_total_minor']/100:.2f}元；失败{s['failed_attempt_count']}次，失败金额不计入实付。"]
    for c in result['contracts']:
        lines.append(f"• {c['service_name']}：{({'monthly':'月付','annual':'年付','quarterly':'季付'}).get(c['billing_cycle'],c['billing_cycle'])}，每期{minor(c['current_price_minor'])/100:.2f}元，{({'non_renewing':'不再续订','active':'有效','trial':'试用中','paused':'已暂停','canceled':'已取消','past_due':'有未付记录'}).get(c['status'],c['status'])}，商户续费{'开启' if str(c['auto_renew_enabled']).lower()=='true' else '关闭'}；下次计划{c.get('next_billing_date') or '无'}，权益至{c.get('access_end_date') or '未知'}。"+
            (f"试用至{c.get('trial_ends_on')}，转收费{minor(c.get('trial_conversion_price_minor'))/100:.2f}元。" if c['status']=='trial' else ''))
    for group,ids in s['duplicate_service_groups'].items():lines.append('同类服务重叠：'+'、'.join(c['service_name'] for c in result['contracts'] if c['subscription_id'] in ids)+'。')
    for b in result['failed_attempts']:lines.append(f"失败记录：{b['service_name']}，{b['scheduled_date']}，{minor(b['amount_minor'])/100:.2f}元，原因{b.get('failure_code') or '未提供'}。")
    if result.get('has_more_contracts'):lines.append(f"合同只展示当前页；可在报告中翻到下一页，汇总仍基于全部合同。")
    if result.get('has_more_failures'):lines.append('失败明细仅展示当前页，可在报告中按失败状态翻页；失败总数按全部记录计算。')
    return '\n'.join(lines+[result['cost_basis']+f"。合同快照日期{result['as_of']}。"])
