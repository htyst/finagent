"""Trusted conversational facts and explicit, session-scoped read continuation."""
import json
from uuid import uuid4
from app.domain.errors import DomainError

def brief_spending(result):
    from app.agent.analytics_summary import range_label
    c=result['current'];t=c['totals'];known='' if t['is_complete'] else '已知'
    lines=[f"{range_label(c['period'])}，{known}净消费 {t['net_minor']/100:,.2f} 元（消费 {t['purchase_minor']/100:,.2f} 元，退款 {t['refund_minor']/100:,.2f} 元）。"]
    top=sorted(c['categories'],key=lambda r:r['net_minor'],reverse=True)[:3]
    if top:lines.append('主要花在：'+'；'.join(f"{r['category']} {r['net_minor']/100:,.2f}元" for r in top)+'。')
    comparison=result.get('comparison')
    if comparison and comparison['delta_minor'] is not None:
        lines.append(f"对比 {range_label(comparison['period'])}，净消费变化 {comparison['delta_minor']/100:+,.2f} 元"+(f"（{comparison['percent_change']}%）" if comparison['percent_change'] is not None else '')+'。')
    sources=c.get('coverage',{}).get('included_sources',[])
    lines.append('范围：'+('现金账户与信用卡' if 'cash_ledger' in sources and 'credit_card_ledger' in sources else '信用卡' if 'credit_card_ledger' in sources else '所选现金账户')+'；还款和本金调拨不重复算消费。')
    if not t['is_complete']:lines.append('这段时间有数据缺口，上面是已知金额，不能当成完整总额。')
    lines.append('全部分类和逐笔记录可展开报告查看。')
    return '\n'.join(lines)

def brief_anomalies(result):
    n=result['candidate_count']
    lines=[f"规则检查了 {result['scanned_count']} 笔已入账消费，"+(f"有 {n} 笔需要你核对。" if n else '暂未标出需要核对的记录。')]
    for row in result['candidates'][:3]:lines.append(f"• {row['merchant'] or '商户未提供'} {row['amount_minor']/100:,.2f} 元："+'；'.join(r['reason'] for r in row['reasons'])+'。')
    lines.append('这只是规则提示，不代表已确认盗刷或重复扣款；具体依据可展开报告查看。')
    if result['coverage']['status']!='complete':lines.append('当前有数据缺口，检查只覆盖已知流水。')
    return '\n'.join(lines)

def brief_analysis(data):
    if data['analysis_kind']!='cashflow':
        from app.agent.read_evidence import analysis_text
        return analysis_text(data)
    s=data['summary']
    return (f"收入 {s['income_minor']/100:,.2f} 元；现金流入 {s['inflow_minor']/100:,.2f} 元，流出 {s['outflow_minor']/100:,.2f} 元，净现金流 {s['net_cashflow_minor']/100:,.2f} 元。\n"
            '现金流统计剔除本人转账及定期本金调拨；信用卡还款计现金流出，信用卡消费不在现金流中重复计入。')

def stage_subscription_access(task,text,evidence,decision=None):
    allowed={'read_subscriptions','identify_subscriptions','read_financial_history','read_service_requests','prepare_merchant_contract_cancel'}
    pending=[{'name':e['tool'],'arguments':e.get('query',{})} for e in evidence if e['tool'] in allowed and (e.get('error')or{}).get('code')=='SUBSCRIPTION_CONFIRMATION_REQUIRED']
    if not pending:return False
    context=json.loads(task.context_json)
    context['pending_subscription_query']={'id':uuid4().hex,'reads':pending,'request':text,'read_only':True}
    if decision is not None and decision.intent in {'subscription_reminder','subscription_revoke'}:
        context['pending_subscription_query']['resume_decision']=decision.model_dump()
    task.context_json=json.dumps(context,ensure_ascii=False)
    return True

def next_actions(task,control=None):
    pending=json.loads(task.context_json).get('pending_subscription_query')
    if not pending or task.stop_requested or (control or {}).get('state') in {'paused','handoff','awaiting_reverification','terminated'}:return []
    resume=pending.get('resume_decision',{}).get('intent') in {'subscription_reminder','subscription_revoke'} or any(r['name']=='prepare_merchant_contract_cancel' for r in pending['reads'])
    return [{'kind':'confirm_subscription_query','pending_id':pending['id'],'label':'确认查询并继续准备' if resume else '确认查询我的订阅',
             'scope':'本人订阅合同、扣费记录、代扣授权、续费提醒与站内通知；有效5分钟。',
             'effect':'只允许查询，并继续核对刚才的办理需求；生成固定单后仍需单独确认。不会在这一步扣款、设置提醒、解除代扣或取消会员续订。' if resume else '只允许查询，不开通后台扣款，不设置提醒，不解除代扣。'}]

def confirm_subscription_query(db,session,task_id,pending_id):
    from app.data.atomic import atomic_write
    from app.agent.service import _task,_say,_record_step,view,MAX_STEPS
    from app.agent.control import assert_running
    from app.agent import registry
    from app.domain.security import confirm_subscription_access
    from app.agent.dialogue import envelope,evidence_reply
    from app.agent.language import Understanding
    from app.agent.dialogue_schema import DialogueTurn
    from app.data.models import AgentStep
    task=_task(db,session,task_id)
    pending=json.loads(task.context_json).get('pending_subscription_query',{})
    if pending.get('runtime_id'):
        from app.agent.dynamic_runtime import resume_subscription
        return resume_subscription(db,session,task_id,pending_id)
    if pending.get('workflow_id'):
        from app.agent.analysis_workflow import resume
        return resume(db,session,task_id,pending_id)
    with atomic_write(db):
        task=_task(db,session,task_id);assert_running(db,task.id)
        context=json.loads(task.context_json)
        if context.get('completed_subscription_query')==pending_id:return view(db,task)
        pending=context.get('pending_subscription_query')
        if not pending or pending['id']!=pending_id:raise DomainError('STALE_RESOURCE','查询问题已变化，请使用当前问题下方的确认按钮',409)
        if db.query(AgentStep).filter_by(task_id=task.id).count()+len(pending['reads'])>MAX_STEPS:raise DomainError('TOOL_BUDGET_EXCEEDED','任务步骤已达上限，请重新提出订阅查询',409)
        # Validate the complete saved read batch before granting anything.
        reads=[]
        for r in pending['reads']:
            if r['name'] not in {'read_subscriptions','identify_subscriptions','read_financial_history'}:raise DomainError('INVALID_TOOL','此入口只支持订阅查询',422)
            args=registry.validate(r['name'],r['arguments']);reads.append((r['name'],args))
        confirm_subscription_access(db,session.id)
        evidence=[]
        for name,args in reads:
            data=registry.execute(db,session_id=session.id,owner_id=session.owner_id,name=name,arguments=args,agent_task_id=task.id)
            _record_step(db,task,name,args,data)
            evidence.append(dict(envelope(name,args,data,'query:'+pending_id),raw_data=data))
        context=json.loads(task.context_json);context.pop('pending_subscription_query',None);context['completed_subscription_query']=pending_id
        from app.agent.read_evidence import memory_reference
        context.setdefault('dialogue_memory',{})['evidence']=[memory_reference(e) for e in evidence]
        task.context_json=json.dumps(context,ensure_ascii=False)
        if pending.get('resume_decision'):
            from app.agent.subscription_flow import dispatch
            decision=Understanding.model_validate(pending['resume_decision'])
            _say(db,task,'查询范围已确认，现在继续核对刚才的需求；尚未授权办理。','gathering_info')
            return dispatch(db,session,task,pending['request'],decision_hint=decision)
        current=view(db,task)
        active=any(isinstance(current.get(k),dict) and current[k]['status']=='pending_confirmation' for k in ('proposal','wealth_order','card_request','subscription_request','birthday_request'))
        decision=Understanding(intent='subscription_inspect',dialogue=DialogueTurn(behavior='insert_query'))
        _say(db,task,'已确认查询范围，继续刚才的订阅查询：\n'+evidence_reply(decision,evidence),'awaiting_confirmation' if active else 'completed')
        return view(db,task)
