"""Typed projection on the existing dynamic runtime; never an authorization engine."""
from copy import deepcopy
from typing import Literal
from pydantic import Field
from app.agent.dialogue_schema import SemanticModel
from app.domain.errors import DomainError

SCHEMA_VERSION='g01-v1'
ID=r'^[A-Za-z][A-Za-z0-9_-]{0,39}$'

class ExecutionBinding(SemanticModel):
    goal_ids: list[str] = Field(default_factory=list,max_length=8)
    depends_on: list[str] = Field(default_factory=list,max_length=32,description='仅列真实前置调用ID；独立查询没有依赖，不把串行顺序当数据依赖。')

class GoalDeclaration(SemanticModel):
    id: str = Field(pattern=ID)
    text: str = Field(min_length=1,max_length=300)
    quote: str = Field(min_length=1,max_length=500,description='本轮用户原话中的连续片段，不添加目标。')
    kind: Literal['answer','query','analysis','operation']

class DeclareGoals(SemanticModel):
    goals: list[GoalDeclaration] = Field(min_length=1,max_length=8)

GoalStatus=Literal['pending','running','waiting_input','waiting_authorization','completed','failed','cancelled','paused','waiting_verification']

class GoalContract(GoalDeclaration):
    status: GoalStatus
    constraints: list[dict] = Field(default_factory=list,max_length=16)
    user_cancelled: bool = False
    implicit: bool = False
    answer_observed: bool = False
    needs_input: bool = False
    next_step: str | None = None
    evidence_refs: list[str] = Field(default_factory=list,max_length=64)
    receipt_refs: list[str] = Field(default_factory=list,max_length=64)

class PolicyContract(SemanticModel):
    effect: Literal['read','calculate','prepare','answer','control','metadata','rejected']
    risk_floor: Literal['green','yellow','red','dynamic','none','deny']
    risk_policy: str
    policy_version: str
    can_submit: Literal[False]

class NodeContract(SemanticModel):
    id: str = Field(pattern=ID)
    task_id: str
    plan_version: int = Field(ge=1)
    result_plan_version: int | None = None
    name: str = Field(min_length=1,max_length=100)
    title: str = Field(max_length=80)
    arguments: dict
    goal_ids: list[str] = Field(max_length=8)
    depends_on: list[str] = Field(max_length=64)
    dependency_sources: dict[str,str]
    observed_refs: list[str] = Field(max_length=24)
    result_refs: list[str] = Field(max_length=64)
    receipt_refs: list[str] = Field(max_length=64)
    status: Literal['pending','running','waiting_input','waiting_authorization','completed','failed','cancelled']
    attempts: int = Field(ge=0,le=64)
    policy: PolicyContract
    error_code: str | None = None
    resource_ref: dict[str,str] | None = None
    domain_status: str | None = None
    evaluated_risk: Literal['green','yellow','red'] | None = None
    authorization_expires_at: str | None = None
    next_step: str | None = None
    completion_basis: Literal['domain_receipt'] | None = None
    reused_from: str | None = None
    invalidated: bool = False
    invalidation_reason: str | None = None
    verification_required: bool = False
    recovered_by: str | None = None
    recovery_reason: str | None = None
    failure_policy: dict | None = None
    blocked_by: list[str] = Field(default_factory=list,max_length=64)

class GraphContract(SemanticModel):
    schema_version: Literal['g01-v1']
    task_id: str
    runtime_id: str
    plan_version: int = Field(ge=1)
    status: Literal['pending','running','waiting_input','waiting_authorization','completed','partially_completed','failed','cancelled','paused','waiting_verification']
    goals: list[GoalContract] = Field(min_length=1,max_length=8)
    nodes: list[NodeContract] = Field(max_length=64)
    serial: Literal[True]

NODE_TRANSITIONS={
 'pending':{'pending','running','failed','cancelled','awaiting_confirmation','succeeded'},
 'running':{'running','pending','awaiting_confirmation','succeeded','failed','cancelled'},
 'awaiting_confirmation':{'awaiting_confirmation','pending','running','succeeded','failed','cancelled'},
 'succeeded':{'succeeded'},'failed':{'failed'},'cancelled':{'cancelled'},
}

def reject(message):raise DomainError('EXECUTION_GRAPH_INVALID',message,422)

def initialize(task,r):
    r.update(graph_schema=SCHEMA_VERSION,task_id=task.id,owner_id=task.owner_id,session_id=task.session_id,
        dataset_version=task.dataset_version,plan_version=task.plan_version,
        goals=[dict(id='g1',text=r['request'][:300],quote=r['request'][:500],kind='answer',status='pending',implicit=True)])

def typed(r):return r.get('graph_schema')==SCHEMA_VERSION

def declare(r,args):
    if not typed(r):reject('历史运行仅兼容恢复，请新建问题后拆分目标')
    proposed=DeclareGoals.model_validate(args).model_dump()['goals']
    if len({g['id'] for g in proposed})!=len(proposed):reject('目标ID重复')
    if any(g['quote'] not in r['request'] for g in proposed):reject('目标必须关联本轮用户原话')
    prior_turn=r.get('turn_start_node_count',0)
    if any(n.get('policy',{}).get('effect')=='prepare' for n in r['nodes'][prior_turn:]):reject('已有业务操作节点，不能用重新声明覆盖已有目标或回执')
    if not prior_turn and any(not g.get('implicit') for g in r['goals']):reject('目标已经声明，请使用有效目标ID；本阶段不在同轮覆盖目标定义')
    renamed={}
    if prior_turn:
        # Reused model-chosen IDs are a naming mistake, not an intent to overwrite.
        # Renaming keeps the user's instruction alive and hands the real IDs back.
        used={old['id'] for old in r['goals']}
        for g in proposed:
            if g['id'] in used:
                hint=g['id'];index=1
                while f'g{index}' in used:index+=1
                g['id']=f'g{index}';used.add(g['id']);renamed[hint]=g['id']
        if len(r['goals'])+len(proposed)>8:reject('当前目标达到上限，请完成已有目标或新建任务')
        r['goals'] += [dict(g,status='pending',implicit=False) for g in proposed]
        r['turn_goal_ids']=[g['id'] for g in proposed]
    else:r['goals']=[dict(g,status='pending',implicit=False) for g in proposed]
    # Refining the initial implicit goal never rewrites edges/results. Earlier
    # observations remain unassigned until an answer actually cites them.
    if not prior_turn:
        for n in r['nodes']:n['goal_ids']=[]
    result={'goals':[dict(g) for g in r['goals']],'next':'分别关联goal_ids；共享查询可关联多个目标，完成状态由后端证据判定。'}
    if renamed:
        result['renamed_goal_ids']=renamed
        result['next']=('目标ID已由服务端重新分配：'+'、'.join(f'{k}→{v}' for k,v in renamed.items())
            +'。请用返回的id绑定goal_ids，不要继续使用旧ID。'+result['next'])
    return result

def references(r,name,args):
    """Only explicit typed references; no edges invented from chronological proximity."""
    refs=[];sources=[]
    if name=='calculate':refs += [i['node'] for i in (args.get('calculation') or {}).get('inputs',[]) if i.get('node')]
    if name=='respond':
        # Structured synthesis has always derived provenance exclusively from
        # validated fact_ids; finish() discards redundant model source strings.
        if not args.get('synthesis'):sources+=args.get('evidence_refs',[])
        ids=[]
        def walk(value):
            if isinstance(value,dict):
                ids.extend(value.get('fact_ids',[]))
                for item in value.values():walk(item)
            elif isinstance(value,list):
                for item in value:walk(item)
        walk(args.get('synthesis'))
        import re
        ids+=re.findall(r'\{\{(f\d+)\}\}',args.get('text',''))
        sources += [f['source_ref'] for f in r['facts'] if f['id'] in ids]
    prefix='call:'+r['id']+':'
    for ref in sources:
        if not ref.startswith(prefix):reject('evidence_refs只接受当前工具的source_ref，例如'+prefix+'s1；不能填facts.id如f1。text用{{f1}}时可省略evidence_refs。')
        refs.append(ref[len(prefix):])
    return list(dict.fromkeys(refs))

def bind(task,r,node,binding,candidates=None):
    if not typed(r):return
    from app.agent.registry import execution_policy
    b=ExecutionBinding.model_validate(binding or {})
    allowed=candidates if candidates is not None else r['goals']
    goals=b.goal_ids or r.get('turn_goal_ids') or [g['id'] for g in allowed if g['status'] not in {'completed','cancelled'}] or [g['id'] for g in allowed]
    if node['name']=='declare_goals':goals=[]
    if any(g not in {v['id'] for v in allowed} for g in goals) or len(goals)!=len(set(goals)):reject('节点关联了未知或重复目标；当前有效ID为：'+','.join(g['id'] for g in allowed)+'。多个目标须先declare_goals，不得猜ID。')
    inferred=[] if node.get('batch_error') else references(r,node['name'],node['arguments']);deps=list(dict.fromkeys(b.depends_on+inferred))
    available={n['id'] for n in r['nodes']+r['queue']}
    if any(d not in available or d==node['id'] for d in deps):reject('前置节点必须存在且不能引用自身或未来节点')
    policy=execution_policy(node['name'],node['arguments'])
    if policy['effect']=='prepare' and any(n.get('verification_required') and set(n.get('goal_ids',[]))&set(goals) for n in r['nodes']):reject('该目标存在结果待核实的操作，不能自动重复准备或付款')
    node.update(task_id=task.id,plan_version=task.plan_version,result_plan_version=None,
        owner_id=task.owner_id,session_id=task.session_id,dataset_version=task.dataset_version,
        goal_ids=goals,depends_on=deps,dependency_sources={d:'argument_reference' if d in inferred else 'explicit_model_dependency' for d in deps},
        observed_refs=[e['source_ref'] for e in r.get('evidence',[]) if e['source_ref'] not in r.get('stale_evidence',[])][-24:],
        result_refs=[],receipt_refs=[],attempts=0,policy=policy)
    for goal in r['goals']:
        if goal['id'] not in goals:continue
        if policy['effect']=='prepare':goal['kind']='operation'
        elif goal.get('implicit') and goal['kind'] not in {'operation','analysis'} and policy['effect']=='read':goal['kind']='analysis' if node['name']=='financial_planning_basis' else 'query'
        elif goal.get('implicit') and node['name']=='calculate':goal['kind']='analysis'

def next_ready(r):
    """Select an actual ready node; queue order alone is not a dependency check.

    Independent work may proceed while another queued node is blocked. When no
    work is ready, return a blocked node so the usual observation protocol can
    report the dependency failure instead of spinning or silently succeeding.
    """
    prior={n['id']:n for n in r['nodes']}
    for i,node in enumerate(r['queue']):
        if node['name']=='respond' or all(d in prior and prior[d]['status']=='succeeded' and not prior[d].get('invalidated') for d in node.get('depends_on',[])):
            return r['queue'].pop(i)
    return r['queue'].pop(0)


def start(r,node):
    if not typed(r):return
    prior={n['id']:n for n in r['nodes']}
    node['blocked_by']=[d for d in node['depends_on'] if d not in prior or prior[d]['status']!='succeeded' or prior[d].get('invalidated')]
    if node['name']!='respond' and node['blocked_by']:
        raise DomainError('DEPENDENCY_NOT_READY','真实前置调用未成功，当前节点没有执行；请先处理失败或缺失条件。',409)
    node['attempts']=1+sum(n['name']==node['name'] and n['arguments']==node['arguments'] for n in r['nodes'] if n['id']!=node['id'])
    node['status']='running'

def observed(r,node,result):
    if not typed(r):return
    node['result_refs']=list(dict.fromkeys(node.get('result_refs',[])+[result['source_ref']]))
    data=result.get('data') or {}
    if isinstance(data,dict) and data.get('resource_kind') and data.get('resource_id'):
        node['resource_ref']={'kind':data['resource_kind'],'id':data['resource_id']}
        if data.get('receipt'):node['receipt_refs']=[f"{data['resource_kind']}:{data['resource_id']}"]
        node['domain_status']=data.get('status')
    if isinstance(data,dict) and data.get('status') in {'cancelled','expired','invalidated'}:node['outcome']='cancelled'
    if node['name']=='calculate' and node['status']=='succeeded':
        # Recover rejected arithmetic attempts only when the verified replacement
        # uses the same actual data and keeps every explicit user constant.
        def inputs(n):return (n['arguments'].get('calculation') or {}).get('inputs',[])
        def refs(n):return {(v.get('node'),v.get('key')) for v in inputs(n) if v.get('node') and v.get('key')}
        def constants(n):return {(v.get('value'),v.get('unit'),v.get('quote')) for v in inputs(n) if v.get('quote')}
        for old in r['nodes']:
            if old['name']!='calculate' or old['status']!='failed' or old.get('recovered_by'):continue
            hypothesis_repair=False
            if old.get('error_code')=='ANALYSIS_STALE_ASSUMPTION':
                quoted=[v for v in inputs(old) if v.get('quote')]
                users=r.get('user_messages',[])
                same_values={(v.get('value'),v.get('unit')) for v in inputs(old) if not v.get('node')}=={(v.get('value'),v.get('unit')) for v in inputs(node) if not v.get('node')}
                same_operation=old['arguments'].get('calculation',{}).get('operation')==node['arguments'].get('calculation',{}).get('operation')
                # Repair only a wrongly quoted DEMONSTRATION hypothesis, keeping
                # identical numbers, operation, real sources and goals. A real user
                # quote/changed budget can never disappear through this exception.
                hypothesis_repair=bool(quoted) and same_values and same_operation and all('假设' in ((v.get('label') or '')+v['quote']) and not any(v['quote'] in text for text in users) for v in quoted) and not constants(node)
            if old.get('error_code') not in {'EXECUTION_GRAPH_INVALID','INVALID_CALCULATION','ANALYSIS_UNIT','ANALYSIS_ASSUMPTION','INVALID_ARGUMENTS'} and not hypothesis_repair:continue
            if not old.get('goal_ids') or not set(old['goal_ids']).issubset(node['goal_ids']):continue
            if refs(old) and refs(old).issubset(refs(node)) and (constants(old).issubset(constants(node)) or hypothesis_repair):
                old['recovered_by']=node['id'];old['recovery_reason']='same_goal_verified_calculation_same_sources_user_constants_preserved'


def waiting(task,r,node,wait,state):
    if not typed(r):return
    node['result_plan_version']=task.plan_version
    node['waiting_for']='authorization' if wait['kind']!='input' else 'input'
    if wait.get('resource_id'):
        node['resource_ref']={'kind':wait['resource_kind'],'id':wait['resource_id']}
        op=next((o for o in state.get('authorization_operations',[]) if o['resource_id']==wait['resource_id']),{})
        node['evaluated_risk']=op.get('level');node['authorization_expires_at']=op.get('expires_at')
    node['next_step']='请核对页面固定操作单并手动授权；需要强验证时使用独立验证窗口。' if wait['kind']!='input' else '请补充页面标出的业务字段；补充不代表授权。'

def responded(r,response):
    if not typed(r):return
    nodes=[n for n in r['nodes'] if n['name']=='respond' and n['status'] in {'pending','running'}]
    ids=nodes[-1]['goal_ids'] if nodes else r.get('turn_goal_ids') or [g['id'] for g in r['goals'] if g['status'] not in {'completed','cancelled'}]
    by_id={n['id']:n for n in r['nodes']}
    def support(nid):
        n=by_id[nid]
        if n.get('policy',{}).get('effect') in {'read','calculate'} and n['status']=='succeeded':
            # An answer citing several goals must not reassign every calculation
            # to all of them. Explicit ownership survives presentation.
            if not n['goal_ids']:n['goal_ids']=list(ids)
            for dep in n['depends_on']:support(dep)
    if nodes:
        for dep in nodes[-1]['depends_on']:support(dep)
    for g in r['goals']:
        if g['id'] in ids:
            g['answer_observed']=True;g['needs_input']=response.needs_input or bool(response.synthesis and response.synthesis.questions)
            g['next_step']='请补充回答中指出的关键条件。' if g['needs_input'] else None
    for n in nodes:
        n['status']='succeeded';n['result_refs']=['message:answer:'+r['id']]


def refresh(r):
    """Derive status from executed observations, not a model supplied completion flag."""
    if not typed(r):return
    for goal in r['goals']:
        linked=[n for n in r['nodes'] if goal['id'] in n.get('goal_ids',[]) and n['name'] not in {'declare_goals','revise_goals','respond','manage_task'} and not n.get('invalidated')]
        # Rejected arguments never created a business operation that could have
        # a receipt. Keep failed attempts in the audit, but do not require a
        # fictitious receipt for them after the corrected operation completes.
        writes=[n for n in linked if n['policy']['effect']=='prepare' and (n['status']!='failed' or n.get('resource_ref'))]
        success=[n for n in linked if n['status']=='succeeded' and n.get('result_refs') and not n.get('recovered_by')]
        from app.agent.runtime_attempts import unresolved
        failures=[n for n in linked if unresolved(n)]
        wait=[n for n in linked if n['status']=='awaiting_confirmation']
        cancelled=[n for n in linked if n.get('outcome')=='cancelled' or n['status']=='cancelled']
        goal['evidence_refs']=list(dict.fromkeys(ref for n in success for ref in n.get('result_refs',[]) if ref not in r.get('stale_evidence',[])))
        goal['receipt_refs']=list(dict.fromkeys(ref for n in writes for ref in n.get('receipt_refs',[])))
        if goal.get('user_cancelled') and not goal.get('receipt_refs'):status='cancelled'
        elif any(n.get('verification_required') for n in linked):status='waiting_verification'
        elif writes and all(n.get('receipt_refs') and n.get('outcome')!='cancelled' for n in writes):status='completed'
        elif cancelled and (not writes or all(n in cancelled or n.get('receipt_refs') for n in writes)):status='cancelled'
        elif r.get('status')=='cancelled':status='cancelled'
        elif r.get('status') in {'paused','superseded'} and goal.get('status')!='completed':status='paused' if r['status']=='paused' else 'cancelled'
        elif wait:status='waiting_authorization'
        elif r.get('failure_classification'):status='failed' if failures else 'waiting_input'
        elif not writes and success and goal['kind']!='operation' and (goal['kind']=='query' or goal.get('answer_observed')) and not goal.get('needs_input') and goal.get('evidence_refs') and not failures:status='completed'
        elif goal['kind']=='query' and success and goal.get('evidence_refs') and not failures and (not goal.get('needs_input') or not goal.get('implicit')):status='completed'
        elif failures:status='failed'
        elif goal.get('needs_input') or any(n['status']=='pending' and n.get('waiting_for')=='input' for n in linked):status='waiting_input'
        elif writes:status='pending'
        elif goal['kind']=='answer' and goal.get('answer_observed'):status='completed'
        elif success and goal['kind']!='operation' and (goal['kind']=='query' or goal.get('answer_observed')) and goal.get('evidence_refs'):status='completed'
        elif r['status']=='running':status='running'
        else:status='pending'
        goal['status']=status
        if status=='waiting_authorization':goal['next_step']=wait[-1].get('next_step','请在页面确认查询范围。')
        elif status=='failed':goal['next_step']='本项调用失败；请按错误原因修正或继续补查，其他结果仍保留。'
        elif status=='completed':goal['next_step']=None


def validate(task,r,previous=None):
    if not typed(r):return
    for key,value in [('task_id',task.id),('owner_id',task.owner_id),('session_id',task.session_id),('dataset_version',task.dataset_version)]:
        if r.get(key)!=value:reject('执行图不属于当前任务、用户、会话或数据版本')
    nodes=r['nodes']+r.get('queue',[]);index={n['id']:n for n in nodes}
    if len(index)!=len(nodes):reject('节点ID重复')
    goal_ids={g['id'] for g in r['goals']}
    if len(goal_ids)!=len(r['goals']):reject('目标ID重复')
    pending=r.get('queue',[])
    if pending and pending[0]['name']=='declare_goals' and not pending[0].get('batch_error'):
        # References are prevalidated against the leading declaration. Business
        # nodes still execute serially only after that declaration succeeds.
        goal_ids.update(g['id'] for g in pending[0]['arguments'].get('goals',[]))
    for g in r['goals']:
        GoalDeclaration.model_validate({k:g[k] for k in ('id','text','quote','kind')})
        if not any(g['quote'] in text for text in r.get('user_requests',[])+[r['request']]) or g['status'] not in {'pending','running','waiting_input','waiting_authorization','completed','failed','cancelled','paused','waiting_verification'}:reject('目标原话或状态非法')
    for n in nodes:
        if not isinstance(n.get('arguments'),dict) or not isinstance(n.get('id'),str) or not isinstance(n.get('title'),str) or len(n['title'])>80:reject('节点字段类型或长度非法')
        if any(not isinstance(n.get(key),list) or len(n[key])>64 or any(not isinstance(v,str) for v in n[key]) for key in ('goal_ids','depends_on','result_refs','receipt_refs')):reject('节点引用字段非法')
        if n.get('task_id')!=task.id or n.get('owner_id')!=task.owner_id or n.get('session_id')!=task.session_id or n.get('dataset_version')!=task.dataset_version:reject('节点归属不符')
        if not isinstance(n.get('plan_version'),int) or not 1<=n['plan_version']<=task.plan_version:reject('节点计划版本非法')
        if n.get('status') not in NODE_TRANSITIONS or not isinstance(n.get('attempts'),int) or n['attempts']<0:reject('节点状态或尝试次数非法')
        if any(g not in goal_ids for g in n['goal_ids']) or any(d not in index for d in n['depends_on']):reject('目标或依赖引用不存在')
        actual={e['source_ref'] for e in r['evidence']}
        if any(ref not in actual and not (n['name']=='respond' and ref=='message:answer:'+r['id']) for ref in n['result_refs']):reject('结果引用不存在或不属于当前执行')
        if n['receipt_refs']:
            ref=n.get('resource_ref') or {}
            expected=str(ref.get('kind'))+':'+str(ref.get('id'))
            backed=any(isinstance(e.get('raw_data'),dict) and e['raw_data'].get('resource_id')==ref.get('id') and e['raw_data'].get('receipt') for e in r['evidence'])
            if n['receipt_refs']!=[expected] or not backed:reject('回执引用没有实际业务结果支持')
        if n.get('recovered_by') and (n['recovered_by'] not in index or index[n['recovered_by']]['status']!='succeeded' or n['recovered_by']==n['id']):reject('修复引用必须对应真实成功的其他调用')
        from app.agent.registry import execution_policy
        if n['policy']!=execution_policy(n['name'],n['arguments']):reject('节点副作用与风险策略只能来自受信任注册表')
    seen=set();active=set()
    def visit(nid):
        if nid in active:reject('执行图存在环')
        if nid in seen:return
        active.add(nid)
        for dep in index[nid]['depends_on']:visit(dep)
        active.remove(nid);seen.add(nid)
    for nid in index:visit(nid)
    if previous and typed(previous) and previous['id']==r['id']:
        for old in previous['nodes']:
            new=index.get(old['id'])
            if not new:reject('不能删除已经执行的节点')
            if new['status'] not in NODE_TRANSITIONS[old['status']]:reject('节点状态转换非法')
            if old['status']=='pending' and new['status'] in {'awaiting_confirmation','succeeded'} and old.get('waiting_for')!='input':reject('未执行节点不能跳过执行或补资料检查点')
            for key in ('task_id','owner_id','session_id','dataset_version','plan_version','name','arguments','policy'):
                if old[key]!=new[key]:reject('已接受节点的条款或归属不能被改写')


def public(task,business=None,control=None):
    import json
    r=deepcopy(json.loads(task.context_json).get('dynamic_runtime') or {})
    if not typed(r):return None
    validate(task,r)
    # A domain write may commit before synthesis or its callback. Show its real
    # receipt even when that model request never returns; never execute here.
    if business:
        from app.agent.authorization import RESOURCES
        for n in r['nodes']:
            ref=n.get('resource_ref')
            if not ref or ref['kind'] not in RESOURCES:continue
            record=business.get(RESOURCES[ref['kind']][0])
            if not record or record.get('id')!=ref['id']:continue
            n['domain_status']=record.get('status')
            if record.get('receipt'):
                n['receipt_refs']=[ref['kind']+':'+ref['id']];n['status']='succeeded'
                n['result_refs']=list(dict.fromkeys(n.get('result_refs',[])+n['receipt_refs']))
                n['completion_basis']='domain_receipt'
            elif record.get('status') in {'cancelled','expired','invalidated'}:n['outcome']='cancelled';n['status']='cancelled'
    if task.stop_requested or control and control.get('state') in {'terminated','paused','handoff'}:
        r['status']='cancelled' if task.stop_requested or control['state']=='terminated' else 'paused'
        for n in r['nodes']:
            if n['status'] in {'pending','running','awaiting_confirmation'} and not n.get('receipt_refs'):n['status']='cancelled' if r['status']=='cancelled' else n['status']
    refresh(r)
    statuses=[g['status'] for g in r['goals']]
    overall='completed' if all(s=='completed' for s in statuses) else 'partially_completed' if 'completed' in statuses else next((s for s in ['waiting_verification','waiting_authorization','waiting_input','failed','paused','cancelled','running'] if s in statuses),'pending')
    keys=('id','task_id','plan_version','result_plan_version','name','title','arguments','goal_ids','depends_on','dependency_sources','observed_refs','result_refs','receipt_refs','status','attempts','policy','error_code','resource_ref','domain_status','evaluated_risk','authorization_expires_at','next_step','completion_basis','reused_from','recovered_by','recovery_reason','invalidated','invalidation_reason','verification_required','failure_policy','blocked_by')
    nodes=[{k:n[k] for k in keys if k in n} for n in r['nodes']]
    for n in nodes:
        n['status']='cancelled' if n.get('domain_status') in {'cancelled','expired','invalidated'} else 'waiting_authorization' if n['status']=='awaiting_confirmation' else 'waiting_input' if n['status']=='pending' and next(v for v in r['nodes'] if v['id']==n['id']).get('waiting_for')=='input' else 'completed' if n['status']=='succeeded' else n['status']
    from app.agent.privacy import model_payload
    result=dict(schema_version=SCHEMA_VERSION,task_id=task.id,runtime_id=r['id'],plan_version=task.plan_version,status=overall,goals=r['goals'],nodes=nodes,serial=True)
    GraphContract.model_validate(result)
    return model_payload(result)
