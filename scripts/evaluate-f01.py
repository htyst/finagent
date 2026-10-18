"""F01 reproducible staging, before/after contracts and budgeted real-model checks."""
import argparse,hashlib,importlib.util,json,os,sqlite3,subprocess,sys
from contextlib import closing
from datetime import datetime,timedelta,timezone
from pathlib import Path
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--prepare',action='store_true')
parser.add_argument('--before-after',action='store_true')
parser.add_argument('--live',action='store_true')
parser.add_argument('--resume-revision',action='store_true')
parser.add_argument('--limit',type=int,default=3)
parser.add_argument('--call-budget',type=int,default=18)
parser.add_argument('--report-name',default='live-first')
args=parser.parse_args()
if not 1<=args.limit<=3 or not 1<=args.call_budget<=24:raise SystemExit('Use 1..3 cases and 1..24 provider requests')
if not args.report_name.replace('-','').isalnum():raise SystemExit('Invalid report name')
OUT=ROOT/'var/reviews/f01-20261017'
OUT.mkdir(parents=True,exist_ok=True)
STAGE=ROOT/'var/TEST-ONLY/f01-20261017'
STAGE.mkdir(parents=True,exist_ok=True)
BASE=(STAGE/'baseline.sqlite3').resolve()
if 'TEST-ONLY' not in BASE.parts or (ROOT/'var').resolve() not in BASE.parents:raise SystemExit('Isolated DB required')
os.environ.update(PYTHONPATH=str(ROOT/'backend'),FINAGENT_DB_PATH=str(BASE),PYTHONDONTWRITEBYTECODE='1',
    FINAGENT_AGENT_ENGINE='dynamic',FINAGENT_MODE='live',
    FINAGENT_SCHEDULER_ENABLED='0',FINAGENT_SUBSCRIPTIONS_ENABLED='0',FINAGENT_BIRTHDAYS_ENABLED='0')
sys.path.insert(0,str(ROOT/'backend'))

def dump(name,value):
    (OUT/name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')

if args.prepare:
    if BASE.exists() and BASE.stat().st_size:raise SystemExit('Staging DB already exists; do not overwrite')
    sqlite3.connect(BASE).close()
    with (OUT/'stage.log').open('w',encoding='utf-8') as log:
        for command in ([sys.executable,'-B','-m','app.data.cli','migrate'],
                [sys.executable,'-B',str(ROOT/'scripts/import-financial-dataset.py'),'--source',str(ROOT/'datasets/demo_financial_v2')]):
            subprocess.run(command,cwd=ROOT,env=os.environ.copy(),stdout=log,stderr=subprocess.STDOUT,check=True)
    dump('stage.json',dict(database=str(BASE),source='datasets/demo_financial_v2',live_database_used=False))
    print('Fresh isolated annual fixture imported.')

def load_before(name,path):
    spec=importlib.util.spec_from_file_location(name,OUT/'before'/path)
    module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module)
    return module

def sample():
    return dict(evidence=[],nodes=[],request='只分析预算',user_messages=['旅游预算两万','两万改八千'],facts=[
        dict(node='basis',id='f1',tool='financial_planning_basis',key='available_liquid',label='可用现金',value='30000',unit='yuan',source_ref='basis'),
        dict(node='basis',id='f2',tool='financial_planning_basis',key='monthly_expense_mean',label='月均净消费',value='1000',unit='yuan',source_ref='basis')])

def inputs(amount,quote):
    return [dict(node='basis',key='available_liquid'),dict(node='basis',key='monthly_expense_mean'),
        dict(value='1',unit='number',label='预留月数'),dict(value=amount,unit='yuan',label='目标成本',quote=quote)]

def calculation(engine,r,amount,quote):
    return engine.calculate(r,dict(arguments=dict(title='预算核算',calculation=dict(operation='goal_budget',inputs=inputs(amount,quote)))))

if args.before_after:
    from app.agent import dynamic_runtime as current,planning_reply
    from app.domain.errors import DomainError
    old_arithmetic=load_before('f01_before_arithmetic','backend/app/agent/analysis_workflow.py')
    old_runtime=load_before('f01_before_runtime','backend/app/agent/dynamic_runtime.py');old_runtime.facts=old_arithmetic
    old_reply=load_before('f01_before_reply','backend/app/agent/planning_reply.py')
    results=[]
    state=sample()
    before=calculation(old_runtime,state,'20000','两万')
    try:calculation(current,state,'20000','两万');raise AssertionError('Old amount was accepted')
    except DomainError as error:code=error.code
    after=calculation(current,state,'8000','八千')
    results.append(dict(case='budget_revision',kind='same-error-proposal-replay',before=dict(old_cost_accepted=True,value=before['value']),
        after=dict(old_cost_rejected=code,new_cost='8000',value=after['value'])))
    state=sample();state['user_messages']=['目标1000美元，先分析']
    before=calculation(old_runtime,state,'1000','1000美元')
    try:calculation(current,state,'1000','1000美元');raise AssertionError('Foreign amount was accepted')
    except DomainError as error:code=error.code
    results.append(dict(case='foreign_cost',kind='same-error-proposal-replay',before=dict(currency_silently_assumed='CNY',value=before['value']),after=dict(rejected=code,action='需人民币预算或核验换算，未编造旅行价格')))
    raw=dict(goal='旅行',conclusion='购物都是可选支出。',reasons=[dict(text='当前现金作为依据。',fact_ids=['f1'])])
    text,_,_=old_reply.response_text(old_reply.Synthesis.model_validate(raw),sample())
    try:planning_reply.response_text(planning_reply.Synthesis.model_validate(raw),sample());raise AssertionError('Unsupported necessity accepted')
    except DomainError as error:code=error.code
    results.append(dict(case='unsupported_necessity',kind='candidate-answer-validation',before=dict(accepted_text=text),after=dict(rejected=code,action='分类不能证明必要性或可调整比例，明确未知')))
    dump('before-after.json',dict(scope='确定性参数与候选回答重放；不冒充旧版真实模型回答',cases=results))
    print('Three before/after contracts reproduced.')

def business_snapshot(dbpath):
    result={}
    with closing(sqlite3.connect(dbpath)) as db:
        tables=[n for n, in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for name in tables:
            if name.startswith(('agent_','audit_')) or name in {'demo_sessions','task_controls'}:continue
            raw=db.execute('SELECT * FROM "'+name.replace('"','""')+'"').fetchall()
            serialized=sorted(json.dumps(row,ensure_ascii=True,default=str) for row in raw)
            result[name]=dict(rows=len(raw),sha256=hashlib.sha256('\n'.join(serialized).encode()).hexdigest())
    return result

if args.live or args.resume_revision:
    from sqlalchemy import select
    from sqlalchemy.orm import sessionmaker
    from app.data.db import make_engine
    from app.data.models import DemoSession,DemoSetting,AgentTask
    from app.agent import model_client,dynamic_runtime
    from app.agent.service import create,send
    from app.domain.errors import DomainError
    config=model_client.configuration()
    target=STAGE/('live-'+uuid4().hex+'.sqlite3')
    live_source=Path(json.loads((OUT/'live-verified.json').read_text(encoding='utf-8'))['database']) if args.resume_revision else BASE
    if 'TEST-ONLY' not in live_source.resolve().parts:raise SystemExit('Unsafe resume database')
    with closing(sqlite3.connect(live_source.as_uri()+'?mode=ro',uri=True)) as source:
        with closing(sqlite3.connect(target)) as dest:source.backup(dest)
    engine=make_engine(target);factory=sessionmaker(engine,expire_on_commit=False)
    calls={'used':0};original=model_client.post_chat
    def bounded(http,body,**kwargs):
        if calls['used']>=args.call_budget:raise DomainError('EVALUATION_BUDGET','本次真实评测调用预算已用尽，保留证据。',503)
        calls['used']+=1
        return original(http,body,**kwargs)
    model_client.post_chat=bounded
    os.environ['AGENT_MAX_MODEL_CALLS']='7';os.environ['AGENT_MAX_TOOL_CALLS']='12';os.environ['AGENT_TIMEOUT_SECONDS']='100'
    cases=[
        dict(id='salary_budget_revision',owner='u1',turns=['根据我的财务情况分析旅行预算两万元、保留三个月开支是否可行。只分析，不办理。','不是两万，是八千，其他条件不变，仍只分析。']),
        dict(id='low_cash_unknown_foreign_cost',owner='u3',turns=['结合我的现金、消费和近期应付款判断能不能旅行，目的地费用是外币但尚无价格和汇率。只给分析建议，不办理。']),
        dict(id='business_income_stress',owner='u8',turns=['我是经营回款，想每月存3000元，同时担心回款中断和近期大额付款。结合账本比较正常与断收两个情景，不替我定目标优先级，只分析不办理。'])]
    if args.resume_revision:
        cases=[dict(id='salary_budget_revision',owner='u1',turns=['两万改八千，仍保留三个月开支；请据当前账本重新测算，只分析不办理。'])]
    before=business_snapshot(target);report=dict(mode='live',provider=config.model,database=str(target),call_budget=args.call_budget,nonfinancial_interaction_tables_excluded=['demo_sessions','task_controls','agent_*','audit_*'],results=[])
    try:
        for case in cases[:args.limit]:
            with factory() as db:
                dataset=db.get(DemoSetting,1).dataset_version
                session=DemoSession(id='TEST-ONLY-'+uuid4().hex,owner_id=case['owner'],dataset_version=dataset,
                    expires_at_utc=(datetime.now(timezone.utc)+timedelta(hours=2)).isoformat(),csrf_token='TEST-ONLY')
                if args.resume_revision:
                    old_task=db.scalar(select(AgentTask).where(AgentTask.owner_id=='u1').order_by(AgentTask.updated_at_utc.desc()))
                    session=db.get(DemoSession,old_task.session_id);task=old_task.id
                else:db.add(session);db.commit();task=None
                for number,message in enumerate(case['turns']):
                    used=calls['used']
                    state=create(db,session,'live',message) if task is None else send(db,session,task,message)
                    task=state['id'];runtime=dynamic_runtime.get(db.get(AgentTask,task))
                    text=state['messages'][-1]['content']
                    synthesis=next((n['arguments'].get('synthesis') for n in reversed(runtime['nodes']) if n['name']=='respond' and n['status']=='succeeded'),None)
                    computations=[e['data'] for e in runtime['evidence'] if e['tool']=='calculate' and not e.get('error')]
                    check=dict(no_operations=not state.get('proposal') and not state.get('authorization_operations'),
                        actual_model_calls=calls['used']>used,queried_basis=any(n['name']=='financial_planning_basis' and n['status']=='succeeded' for n in runtime['nodes']),
                        structured=bool(synthesis),questions_bounded=not synthesis or len(synthesis.get('questions',[]))<=2)
                    if case['id']=='salary_budget_revision' and (number==1 or args.resume_revision):
                        active_costs=[a['value'] for c in computations for a in c.get('assumptions',[]) if a['unit']=='yuan' and a['source']=='用户明确参数']
                        check['new_budget_only']='8000' in active_costs and '20000' not in active_costs
                    report['results'].append(dict(case=case['id'],turn=number+1,input=message,status=state['status'],reply=text,
                        synthesis=synthesis,model_calls=calls['used']-used,tools=[dict(tool=n['name'],status=n['status'],error=n.get('error_code')) for n in runtime['nodes']],
                        calculations=computations,checks=check))
                    dump(args.report_name+'.json',report)
    except Exception as error:
        report['execution_error']=type(error).__name__  # Do not echo arbitrary exception payloads.
    finally:
        model_client.post_chat=original
        after=business_snapshot(target)
        report.update(provider_requests=calls['used'],business_tables_unchanged=before==after,
            changed_business_tables=[name for name in before if before[name]!=after.get(name)],
            deterministic_safety_passed=before==after and all(r['checks']['no_operations'] for r in report['results']),
            checks_passed=all(all(r['checks'].values()) for r in report['results']) and len(report['results'])==sum(len(c['turns']) for c in cases[:args.limit]),
            natural_language_independent_review=False)
        dump(args.report_name+'.json',report);engine.dispose()
    print(json.dumps({k:report[k] for k in ('provider_requests','business_tables_unchanged','changed_business_tables','deterministic_safety_passed','checks_passed')},ensure_ascii=True))
    if not report['checks_passed'] or not report['deterministic_safety_passed']:raise SystemExit(1)
