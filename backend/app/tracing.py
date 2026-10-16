"""Durable request trace; deliberately excludes confirmation credentials and secrets."""
import json
import logging
import os
from time import perf_counter
from uuid import uuid4
from sqlalchemy import select
from app.clock import now
from app.neuro_models import TraceRecord,VerificationRecord
from app.data.models import AgentTask,AgentStep
from app.data.atomic import commit
from app.agent.registry import DESCRIPTIONS
from app.verifier import RULES,CLARIFICATION_RULES

logger=logging.getLogger('finagent.trace')
def emotion_template(text):
    for state,words in [('distressed',('崩溃','害怕','救命')),('frustrated',('烦死','怎么还','没用','又错')),
                        ('urgent',('急','马上','赶紧'))]:
        found=next((w for w in words if w in text),None)
        if found:
            return {'state':state,'confidence':0.7,'evidence':found}
    return {'state':'neutral','confidence':1.0,'evidence':''}
def traced_send(fn,db,session,task_id,text):
    started=perf_counter()
    if os.getenv('FINAGENT_TRACE','on')=='off':
        return fn(db,session,task_id,text)
    existing=set(db.scalars(select(VerificationRecord.id).where(VerificationRecord.owner_id==session.owner_id)))
    result=None
    failure=None
    try:
        result=fn(db,session,task_id,text)
        return result
    except Exception as error:
        failure=type(error).__name__
        raise
    finally:
        task=db.get(AgentTask,task_id)
        if task:
            context=json.loads(task.context_json)
            emotion=context.get('emotion') or emotion_template(text)
            records=db.scalars(select(VerificationRecord).where(VerificationRecord.owner_id==session.owner_id)).all()
            fresh=[json.loads(r.payload_json) for r in records if r.id not in existing]
            violations=[v for r in fresh for v in r['violations']]
            violations+=context.get('plan_violations',[]) if not result or result.get('status')=='gathering_info' else []
            clarifications=[c for r in fresh for c in r.get('clarifications',[])]
            passed=list(dict.fromkeys(rule for r in fresh if r.get('certificate') for rule in r['certificate']['rules_passed']))
            steps=db.scalars(select(AgentStep).where(AgentStep.task_id==task_id,
                            AgentStep.plan_version==task.plan_version).order_by(AgentStep.ordinal)).all()
            plan=context.get('neuro_plan')
            trace_id=uuid4().hex
            trace={'trace_id':trace_id,'input':text,
                'understanding':{'intent':task.intent,'params':context.get('standing_slots') or context.get('dialogue_slots') or {},
                                 'emotion':emotion},
                'emotion_flag':emotion['state']!='neutral',
                'plan':{'steps':plan['steps'] if plan else [{'id':str(r.ordinal),'tool':r.tool_name,
                    'params':json.loads(r.args_json),'depends_on':json.loads(r.depends_on_json)} for r in steps],
                    'source':context.get('plan_source','template'),
                    'recurrence':plan.get('recurrence') if plan else None,
                    'clarification':plan.get('clarification') if plan else None,
                    'descriptions':{r.tool_name:DESCRIPTIONS.get(r.tool_name,r.tool_name) for r in steps}},
                'verification':{'approved':bool(fresh) and not violations and not clarifications,'rules_passed':passed,'violations':violations,
                    'would_block':[v for r in fresh for v in r.get('would_block',[])],
                    'clarifications':clarifications,
                    'rule_descriptions':{k:r.description for k,r in (RULES|CLARIFICATION_RULES).items()},'records':fresh},
                'temporal':context.get('temporal_decision'),
                'model_error':context.get('model_error'),
                'repair_rounds':context.get('repair_rounds',0),'repair_history':context.get('repair_history',[]),
                'outcome':failure or (result.get('status') if result else task.status),'clock_now':now().isoformat(),
                'latency_ms':{'parse':round(context.get('parse_ms',0),3),'verify':round(context.get('verify_ms',0),3),
                              'total':round((perf_counter()-started)*1000,3)}}
            db.add(TraceRecord(id=trace_id,owner_id=session.owner_id,task_id=task_id,
                payload_json=json.dumps(trace,ensure_ascii=False),created_at=now().isoformat()))
            context['trace_id']=trace_id
            task.context_json=json.dumps(context,ensure_ascii=False)
            commit(db)
            logger.info(json.dumps(trace,ensure_ascii=False))
            if result is not None:
                result['trace_id']=trace_id
