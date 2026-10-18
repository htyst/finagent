"""Incremental checkpoints in the ONE dynamic_runtime document, never a payment executor."""
from copy import deepcopy
from hashlib import sha256
import json
from datetime import datetime,timezone
from typing import Literal
from pydantic import Field
from sqlalchemy import select,func
from app.agent.dialogue_schema import SemanticModel
from app.domain.errors import DomainError

class Constraint(SemanticModel):
    key: str = Field(min_length=1,max_length=60)
    value: str = Field(min_length=1,max_length=300)
    quote: str = Field(min_length=1,max_length=500)

class ReviseGoals(SemanticModel):
    goal_ids: list[str] = Field(min_length=1,max_length=8)
    action: Literal['update','cancel','continue']
    instruction_quote: str = Field(min_length=1,max_length=1000)
    constraints: list[Constraint] = Field(default_factory=list,max_length=16)


def version(db,task):
    # Append-only ledger revision plus mutable owned resource versions. Never a
    # cross-user cache key; imported snapshots are immutable within dataset_version.
    from app.data.models import Account,Card,Recipient,Transaction
    from app.data.card_models import CardControl
    from app.data.financial_models import FinancialEvent
    values={'owner':task.owner_id,'session':task.session_id,'dataset':task.dataset_version}
    for cls in (Account,Card,Recipient):
        values[cls.__tablename__]=[list(row) for row in db.execute(select(cls.id,cls.version).where(cls.owner_id==task.owner_id).order_by(cls.id))]
    values['controls']=[list(row) for row in db.execute(select(CardControl.card_id,CardControl.version).join(Card,Card.id==CardControl.card_id).where(Card.owner_id==task.owner_id).order_by(CardControl.card_id))]
    for cls in (Transaction,FinancialEvent):
        values[cls.__tablename__]=list(db.execute(select(func.count(),func.sum(cls.amount_minor),func.max(cls.posted_at_utc)).where(cls.owner_id==task.owner_id)).one())
    # Reservations and mutable orders can alter available funds without cash legs.
    from app.data.models import Base
    for name,table in sorted(Base.metadata.tables.items()):
        if 'owner_id' not in table.c or name in {'accounts','cards','recipients','transactions','financial_events'}:continue
        if not any(k in name for k in ('reserv','schedule','birthday','subscription','wealth','position','service_request')):continue
        columns=[c for c in table.c if c.name in {'id','status','version','amount_minor','remaining_minor','reserved_minor','updated_at_utc'}]
        if columns:values[name]=sorted([list(row) for row in db.execute(select(*columns).where(table.c.owner_id==task.owner_id))],key=str)
    return sha256(json.dumps(values,sort_keys=True,default=str).encode()).hexdigest()


def invalidate(r,seeds,reason):
    affected=set(seeds)
    while True:
        more={n['id'] for n in r['nodes'] if set(n.get('depends_on',[]))&affected and not n.get('receipt_refs')}
        if more<=affected:break
        affected|=more
    refs=[]
    for n in r['nodes']:
        if n['id'] not in affected or n.get('receipt_refs'):continue
        n.update(invalidated=True,invalidation_reason=reason)
        refs.extend(n.get('result_refs',[]))
    r['stale_evidence']=list(dict.fromkeys(r.get('stale_evidence',[])+refs))
    for g in r['goals']:
        if any(g['id'] in n.get('goal_ids',[]) for n in r['nodes'] if n['id'] in affected):g.update(answer_observed=False)
    return sorted(affected)


def reconcile(db,session,task,r):
    """Consume actual persisted receipts; not a single executor is called here."""
    from app.agent import dynamic_runtime as rt
    from app.agent.service import view
    from app.agent.authorization import RESOURCES
    business=view(db,task)
    for node in r['nodes']:
        ref=node.get('resource_ref');record=business.get(RESOURCES.get((ref or {}).get('kind'),('unknown',))[0])
        if ref and record and record.get('id')==ref['id']:
            if record.get('receipt') and not node.get('receipt_refs'):
                rt.observation(r,node,dict(resource_kind=ref['kind'],resource_id=ref['id'],status=record['status'],receipt=record['receipt']))
                r.setdefault('completed_operations',[]).append(dict(name=node['name'],resource_kind=ref['kind'],resource_id=ref['id'],status=record['status']))
                if (r.get('wait') or {}).get('node_id')==node['id']:r['wait']=None
            elif record.get('status') in {'cancelled','expired','invalidated'}:
                node.update(outcome='cancelled',invalidated=True,invalidation_reason=record['status'])
                if node['status'] not in {'succeeded','failed'}:node['status']='cancelled'
                if (r.get('wait') or {}).get('node_id')==node['id']:r['wait']=None
        if node['status']=='running':
            node['status']='failed'
            unknown=node.get('policy',{}).get('effect')=='prepare'
            node['error_code']='RESULT_UNKNOWN' if unknown else 'INTERRUPTED_READ'
            node['verification_required']=unknown
            node['next_step']='结果待核实，请查看原操作记录或人工核对；不能自动重复办理。' if unknown else '读取被中断，可重新查询；未自动重放。'
    # Queued, not-yet-executed calls from an interrupted request are discarded.
    r['queue']=[]
    current=version(db,task)
    if r.get('evidence_version') and r['evidence_version']!=current:
        seeds=[n['id'] for n in r['nodes'] if n.get('policy',{}).get('effect') in {'read','calculate'} and n['name']!='search_knowledge']
        invalidate(r,seeds,'owned_resource_version_changed')
    protected=[n['id'] for n in r['nodes'] if n.get('policy',{}).get('risk_floor')=='yellow' and n.get('policy',{}).get('effect')=='read']
    if protected:invalidate(r,protected,'protected_query_requires_current_scope_confirmation')
    r['evidence_version']=current
    return business


def continuation(db,session,task,previous,text,history):
    from app.agent import execution_graph as graph
    if not previous or not graph.typed(previous) or not previous.get('nodes'):return None
    r=deepcopy(previous);reconcile(db,session,task,r)
    if (r.get('wait') or {}).get('kind') in {'subscription_query','subscription_precondition'}:
        node=next((n for n in r['nodes'] if n['id']==r['wait']['node_id']),None)
        if node and node['status'] not in {'succeeded','failed'}:node['status']='cancelled';node['invalidated']=True;node['invalidation_reason']='protected_query_scope_replaced'
        r['wait']=None
    r.setdefault('user_requests',[previous['request']]);r['user_requests'].append(text)
    sources=[g['quote'] for g in r['goals']]+[c['quote'] for g in r['goals'] for c in g.get('constraints',[])]
    r['user_requests']=list(dict.fromkeys(sources+r['user_requests'][-32:]))
    r.update(request=text,messages=history,user_messages=[m['content'] for m in history if m['role']=='user'],queue=[],status='running')
    # Invalidate old user-derived amounts at the existing F01 provenance barrier,
    # even if the model forgets to call revise_goals. This is validation, not routing.
    from app.agent import analysis_workflow as facts
    seeds=[]
    for n in r['nodes']:
        if n['name']!='calculate' or n.get('invalidated'):continue
        for operand in n['arguments'].get('calculation',{}).get('inputs',[]):
            if operand.get('node') or not operand.get('quote'):continue
            try:facts.calculate({'arguments':{'operation':'sum','inputs':[operand]}},[],r['user_messages'])
            except DomainError:
                seeds.append(n['id']);r.setdefault('revision_pending',[]).extend(n.get('goal_ids',[]));break
    if seeds:invalidate(r,seeds,'user_assumption_superseded_before_model')
    r['revision_pending']=list(dict.fromkeys(r.get('revision_pending',[])))
    # Preserve explicit pending goals; a fresh implicit goal is only a candidate
    # for this message until the model binds/revises actual goal IDs.
    r['turn_start_node_count']=len(r['nodes']);r['turn_revision']=r.get('turn_revision',0)+1
    r['turn_goal_ids']=[]
    checkpoint(r,task,'user_message')
    return r


def checkpoint(r,task,reason):
    if not r.get('graph_schema'):return
    r['checkpoint']={'plan_version':task.plan_version,'at_utc':datetime.now(timezone.utc).isoformat(),'reason':reason,
        'evidence_version':r.get('evidence_version'),'wait':deepcopy(r.get('wait')),
        'failure_codes':list(dict.fromkeys(n['error_code'] for n in r['nodes'] if n.get('error_code') and not n.get('recovered_by'))),
        'continuation':'继续当前未完成目标；已成功业务只核对回执，不再次提交。'}


def revise(db,session,task,r,args):
    args=ReviseGoals.model_validate(args).model_dump();ids=set(args['goal_ids']);known={g['id'] for g in r['goals']}
    if not ids<=known:raise DomainError('UNKNOWN_GOAL','目标不存在或不属于当前任务',422)
    quote=args['instruction_quote']
    def grounded(c):
        if c['quote'] not in r['request']:return False
        if c['value'] in c['quote']:return True
        # Reuse the SAME authoritative Chinese-number/unit/correction grounding
        # as financial calculations; normalizing 八千 to 8000 is not invention.
        from app.agent.requirements import yuan_minor
        from app.agent import analysis_workflow as facts
        try:
            amount=yuan_minor(c['value'].strip().removesuffix('元').strip())
            if amount is None:return False
            facts.calculate({'arguments':{'operation':'sum','inputs':[dict(value=str(facts.decimal(amount)/100),unit='yuan',label=c['key'],quote=c['quote'])]}},[],[r['request']])
            return True
        except (DomainError,ValueError,TypeError):return False
    if quote not in r['request'] or any(not grounded(c) for c in args['constraints']):
        raise DomainError('UNGROUNDED_GOAL_CHANGE','变更及约束必须来自本轮用户明确原话',422)
    action=args['action']
    if action=='cancel':
        import re
        if re.search(r'不要取消|别取消|不取消|不想取消|勿取消',quote):raise DomainError('UNGROUNDED_GOAL_CHANGE','用户明确否定取消，没有取消目标',422)
    if action in {'update','cancel'}:
        if action=='cancel' and args['constraints']:raise DomainError('INVALID_ARGUMENTS','取消目标不接受新约束',422)
        for n in r['nodes']:
            if not ids.intersection(n.get('goal_ids',[])) or n.get('policy',{}).get('effect')!='prepare' or n.get('receipt_refs'):continue
            if set(n['goal_ids'])-ids:raise DomainError('SHARED_OPERATION','该操作支持多个目标，请明确所有受影响目标后再变更',409)
            ref=n.get('resource_ref')
            if ref:
                from app.agent.authorization import describe,handlers
                op=describe(db,session,task,ref['kind'],ref['id'])
                if op['status']=='pending_confirmation':
                    _,_,cancel,field=handlers(ref['kind']);cancel(db,session_id=session.id,**{field:ref['id']})
            n.update(invalidated=True,invalidation_reason='user_goal_'+action)
            if n['status'] not in {'succeeded','failed'}:n['status']='cancelled'
            if (r.get('wait') or {}).get('node_id')==n['id']:r['wait']=None
        def dimension(text):
            import re
            if re.search('budget|amount|money|cost|预算|金额|成本',text,re.I):return 'money'
            if re.search('deadline|horizon|time|month|期限|时间|月数|天数',text,re.I):return 'time'
            return 'other'
        changed={dimension(c['key']) for c in args['constraints']} or {'other'}
        def changed_calculation(n):
            if action=='cancel':return False # retain already-completed shared calculations
            if 'other' in changed:return True # uncertain dependency is invalidated conservatively
            inputs=n['arguments'].get('calculation',{}).get('inputs',[])
            return any(not i.get('node') and dimension((i.get('label') or '')+(i.get('quote') or '')) in changed for i in inputs)
        seeds=[n['id'] for n in r['nodes'] if ids.intersection(n.get('goal_ids',[])) and
            (n.get('policy',{}).get('effect')=='answer' or n.get('policy',{}).get('effect')=='calculate' and changed_calculation(n))]
        invalidate(r,seeds,'user_goal_'+action);task.plan_version+=1;r['plan_version']=task.plan_version
        wait=r.get('wait')
        if wait and wait.get('kind')=='authorization':
            from app.agent.authorization import describe
            op=describe(db,session,task,wait['resource_kind'],wait['resource_id'])
            wait.update(plan_version=task.plan_version,action_id=op['action_id'])
        for g in r['goals']:
            if g['id'] not in ids:continue
            if g.get('receipt_refs') and action=='cancel':continue # cancellation cannot reverse payment
            g.update(user_cancelled=action=='cancel',answer_observed=False,needs_input=False,next_step=None)
            existing={c['key']:c for c in g.get('constraints',[])}
            for c in args['constraints']:existing[c['key']]=dict(c,source='user',plan_version=task.plan_version)
            g['constraints']=list(existing.values())
    elif any(g.get('user_cancelled') for g in r['goals'] if g['id'] in ids):
        raise DomainError('GOAL_CANCELLED','已取消目标不能隐式重放；请明确新建需求',409)
    r['revision_pending']=[gid for gid in r.get('revision_pending',[]) if gid not in ids]
    r['turn_goal_ids']=list(args['goal_ids']);checkpoint(r,task,'goal_'+action)
    return {'goal_ids':args['goal_ids'],'action':action,'plan_version':task.plan_version,'note':'仅更新未执行目标；已发生业务和回执保留，不撤回付款。'}
