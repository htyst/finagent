"""Isolated deterministic provider fixture. Never imported by normal startup."""
import json,os
from pathlib import Path
from uuid import uuid4
from app.main import app
from app.agent import dynamic_protocol as protocol,registry,dynamic_runtime
from app.data.models import AgentTask
from app.domain.errors import DomainError
if 'TEST-ONLY' not in Path(os.environ.get('FINAGENT_DB_PATH','')).resolve().parts or os.environ.get('FINAGENT_G01_BROWSER_FIXTURE')!='1':
    raise RuntimeError('G01 model substitute requires explicit TEST-ONLY environment')
CASES={'先从活期账户转100元到储蓄账户，再申请虚拟卡，申请人为本人，关联活期账户':'forms','测试长回复布局':'long','预算改成八千元':'revision','你好':'greeting','查询本人余额':'balance','分析旅行是否可行，预算待定':'missing','查余额，并分析预算待定的旅行':'multiple','测试余额读取失败':'failure','从活期账户转100元到储蓄账户':'authorization'}
requests={}
CASES['创建五日只读复查草稿']='review'
CASES['测试回答校验恢复']='answer_repair'
def call(name,args=None):return {'role':'assistant','content':None,'tool_calls':[{'id':uuid4().hex,'type':'function','function':{'name':name,'arguments':json.dumps(args or {},ensure_ascii=False)}}]}
def model(messages,context):
    g=context['execution_graph'];rid=g['runtime_id']
    text=next((m['content'] for m in reversed(messages) if m['role']=='user' and m['content'] in CASES),'')
    if rid not in requests or requests[rid]['text']!=text:requests[rid]={'text':text,'start':len(g['nodes'])}
    kind=CASES.get(text);nodes=g['nodes'][requests[rid]['start']:]
    if kind=='answer_repair':
        if not nodes:return call('list_accounts')
        if not any(n['name']=='respond' for n in nodes):return call('respond',dict(text='余额为999999.99元。'))
        return call('respond',dict(text='本人账户已核对，请以查询结果为准。'))
    if kind=='review':
        from datetime import datetime,timedelta,timezone
        now=datetime.now(timezone.utc)
        return call('prepare_review_plan',dict(title='五日余额复查',run_at=(now+timedelta(days=1)).isoformat(),end_at=(now+timedelta(days=31)).isoformat(),interval_days=5,target_yuan='5000',instruction_quote=text))
    if kind=='long':return {'role':'assistant','content':'这是隔离浏览器的长回复布局样例，不是业务结果。\n\n'+('可展开任务详情查看实际状态，涉及资金和卡片的办理仍需独立固定单授权。\n'*60)}
    if kind=='forms':
        if not nodes:return call('declare_goals',dict(goals=[dict(id='pay',text='从活期账户转100元到储蓄账户',quote='从活期账户转100元到储蓄账户',kind='operation'),dict(id='card',text='申请虚拟卡',quote='申请虚拟卡',kind='operation')]))
        if not any(n['name']=='prepare_transfer' for n in nodes):return call('prepare_transfer',dict(source='活期账户',target='储蓄账户',amount_yuan='100',operation_context={'instruction_quote':'从活期账户转100元到储蓄账户'},execution={'goal_ids':['pay']}))
        if not any(n['name']=='prepare_virtual_card_application' for n in nodes):return call('prepare_virtual_card_application',dict(source='活期账户',applicant='本人',operation_context={'instruction_quote':'申请虚拟卡，申请人为本人，关联活期账户'},execution={'goal_ids':['card']}))
        return call('respond',dict(text='各项办理结果以固定单为准。'))
    if kind=='revision':
        if not nodes:return call('revise_goals',dict(goal_ids=['g1'],action='update',instruction_quote=text,constraints=[dict(key='budget',value='八千',quote=text)]))
        return call('respond',dict(text='已更新旅行预算，还需明确出行时间；原查询结果保留。',needs_input=True))
    if kind=='greeting':return {'role':'assistant','content':'你好，可以说说你想了解什么。'}
    if kind=='multiple':
        if not nodes:return call('declare_goals',{'goals':[dict(id='cash',text='查余额',quote='查余额',kind='query'),dict(id='plan',text='分析预算待定的旅行',quote='分析预算待定的旅行',kind='analysis')]})
        if len(nodes)==1:return call('financial_planning_basis',{'execution':{'goal_ids':['cash','plan']}})
        return call('respond',dict(text='本人资金已核对；旅行还需要你明确预算。',needs_input=True,execution={'goal_ids':['plan']}))
    if not nodes:
        if kind=='authorization':return call('prepare_transfer',dict(source='活期账户',target='储蓄账户',amount_yuan='100'))
        return call('financial_planning_basis' if kind=='missing' else 'list_accounts')
    if kind=='authorization':return call('respond',dict(text='已收到真实业务回执，结果见固定单。'))
    return call('respond',dict(text='读取失败，暂时无法提供余额。' if kind=='failure' else '本人现金与收支已核对，还需要你明确旅行预算。' if kind=='missing' else '本人账户已查询，余额见数据卡片。',needs_input=kind=='missing'))
original=registry.execute
def execute(*args,**kwargs):
    db=args[0];task=db.get(AgentTask,kwargs.get('agent_task_id'))
    if task and dynamic_runtime.get(task)['request']=='测试余额读取失败':raise DomainError('G01_BROWSER_READ_FAILURE','浏览器验收注入读取失败',503)
    return original(*args,**kwargs)
protocol.step=model;registry.execute=execute
