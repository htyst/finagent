"""Budgeted REAL DeepSeek evaluation, isolated ledger and per-turn assertions.

No mocked semantic responses here. Fault injection is separately labelled pytest.
"""
import argparse
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from time import perf_counter
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--ids',default='',help='Comma-separated U01..U13, default all')
    parser.add_argument('--api-budget',type=int,default=48,help='Hard total HTTP call cap, including retries')
    args=parser.parse_args()
    if not 1 <= args.api_budget <= 100: parser.error('Budget must be 1..100')
    dataset=json.loads((ROOT/'backend/evals/unified-dialogue-v1.json').read_text(encoding='utf-8'))
    selected=set(args.ids.split(',')) if args.ids else {c['id'] for c in dataset['cases']}
    cases=[c for c in dataset['cases'] if c['id'] in selected]
    if {c['id'] for c in cases} != selected: parser.error('Unknown case ID')
    run=ROOT/'var/TEST-ONLY'/('unified-live-'+uuid4().hex); run.mkdir(parents=True)
    os.environ['FINAGENT_DB_PATH']=str(run/'eval.sqlite3')
    os.environ['FINAGENT_SCHEDULER_ENABLED']='0'
    from sqlalchemy import func,select
    from sqlalchemy.orm import sessionmaker
    from app.data import security_models  # noqa
    from app.data.models import Base,DemoSession,DemoSetting,AgentTask,TransferOrder
    from app.data.db import make_engine
    from app.data.seed import reset_demo,check_ledger
    from app.agent.service import create,send
    from app.agent import model_client
    from app.agent.privacy import model_payload
    from app.agent.telemetry import collect
    from app.domain.errors import DomainError
    from app.domain.operations import get_proposal,issue_confirmation_token,confirm_proposal
    # Bound BEFORE credentials leave the process; no raw request/response logging.
    original=model_client.measured_post
    counter={'attempts':0}
    def measured(http,url,**kwargs):
        if counter['attempts'] >= args.api_budget:
            raise DomainError('EVALUATION_BUDGET_EXCEEDED','真实API评测预算已用完',503)
        counter['attempts']+=1
        return original(http,url,**kwargs)
    model_client.measured_post=measured
    engine=make_engine(Path(os.environ['FINAGENT_DB_PATH'])); Base.metadata.create_all(engine)
    factory=sessionmaker(engine,expire_on_commit=False)
    results=[]; traces=[]; all_usage=[]
    try:
        for case in cases:
            with factory() as db:
                reset_demo(db,date(2026,9,24))
                sid=uuid4().hex; session=DemoSession(id=sid,owner_id='u1',dataset_version=db.get(DemoSetting,1).dataset_version,
                    expires_at_utc=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),csrf_token='evaluation-only')
                db.add(session);db.commit()
                state=None; case_checks=[]; first_receipt=None
                started=perf_counter()
                with collect() as attempts:
                    for expected in case['turns']:
                        previous=(state or {}).get('proposal'); old=previous['id'] if previous else None
                        try:
                            text=expected['text']
                            state=send(db,session,state['id'],text) if state and state['status'] not in {'completed','cancelled','expired','failed'} else create(db,session,'live',text)
                            checks={'reply_returned': bool(state['messages'] and state['messages'][-1]['role']=='assistant')}
                            proposal=state.get('proposal'); card=state.get('card_request')
                            checks['no_unauthorized_execution']=db.scalar(select(func.count()).select_from(TransferOrder))==(1 if first_receipt else 0)
                            if 'status' in expected:checks['status']=state['status'] in expected['status']
                            if expected.get('no_proposal'):checks['no_proposal']=proposal is None
                            if expected.get('no_pending'):checks['no_pending']=not proposal or proposal['status']!='pending_confirmation'
                            if 'amount_minor' in expected:checks['amount']=bool(proposal and proposal['summary']['amount_minor']==expected['amount_minor'])
                            if 'target_id' in expected:checks['target']=bool(proposal and proposal['summary']['target']['id']==expected['target_id'])
                            if expected.get('old_invalidated'):checks['old_invalidated']=bool(old and get_proposal(db,session_id=sid,proposal_id=old)['status']=='invalidated')
                            if 'tool' in expected:checks['tool']=expected['tool'] in [s['tool_name'] for s in state['steps']]
                            for field in ('card_kind','card_id','limit_minor'):
                                if field in expected:checks[field]=bool(card and (card['kind'] if field=='card_kind' else card[field] if field=='card_id' else card['summary'][field])==expected[field])
                            if 'min_goals' in expected:checks['goals']=len(state['requested_outcomes'])>=expected['min_goals']
                            if 'preserved_terms' in expected:
                                memory=json.loads(db.get(AgentTask,state['id']).context_json).get('dialogue_memory',{})
                                serialized=json.dumps({'goals':state['requested_outcomes'],'conditions':memory.get('conditions'),
                                    'times':memory.get('time_requirements'),'original':memory.get('last_instruction')},ensure_ascii=False)
                                checks['terms']=all(term in serialized for term in expected['preserved_terms'])
                            if 'reply_contains' in expected:checks['explanation']=all(word in state['messages'][-1]['content'] for word in expected['reply_contains'])
                            if expected.get('same_receipt'):checks['same_receipt']=bool(first_receipt and first_receipt['order_id'] in state['messages'][-1]['content'])
                            if 'start' in expected:
                                steps=[s for s in state['steps'] if s['tool_name']=='summarize_spending']; query=steps[-1]['arguments'] if steps else {}
                                checks['query_fields']=all(query.get(k)==expected[k] for k in ('start','end','category'))
                            if expected.get('confirm_yellow') and proposal:
                                token=issue_confirmation_token(db,session_id=sid,proposal_id=proposal['id'])['confirmation_token']
                                first_receipt=confirm_proposal(db,session_id=sid,proposal_id=proposal['id'],confirmation_token=token)
                                replay=confirm_proposal(db,session_id=sid,proposal_id=proposal['id'],confirmation_token=token)
                                checks['repeated_confirmation_same_receipt']=first_receipt==replay
                            case_checks.append({'text':text,'passed':all(checks.values()),'checks':checks})
                            traces.append({'case':case['id'],'turn':text,'state':model_payload(state),
                                'semantic_state':model_payload(json.loads(db.get(AgentTask,state['id']).context_json).get('dialogue_memory',{}))})
                        except DomainError as error:
                            case_checks.append({'text':expected['text'],'passed':False,'error':error.code})
                            break
                elapsed=perf_counter()-started
                all_usage.extend(x['usage'] for x in attempts if x.get('usage'))
                ledger=check_ledger(db)
                results.append({'id':case['id'],'category':case['category'],'passed':all(x['passed'] for x in case_checks) and len(case_checks)==len(case['turns']) and not ledger,
                    'seconds':round(elapsed,3),'model_attempts':len(attempts),'turns':case_checks,'ledger_errors':ledger})
                print(case['id']+': '+('passed' if results[-1]['passed'] else 'FAILED'),flush=True)
    finally:
        model_client.measured_post=original
        engine.dispose()
    report={'mode':'real_deepseek','dataset':dataset['version'],'review_status':dataset['review_status'],
        'denominator':len(results),'passed':sum(c['passed'] for c in results),'http_attempts':counter['attempts'],
        'api_budget':args.api_budget,'usage_records':len(all_usage),'tokens':{k:sum(u.get(k,0) for u in all_usage) for k in ('prompt_tokens','completion_tokens','total_tokens')},
        'cost':None,'cost_note':'No billing record; not estimated','results':results}
    (run/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    (run/'transcripts.json').write_text(json.dumps(traces,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'run':str(run),**{k:v for k,v in report.items() if k!='results'}},ensure_ascii=False,indent=2))
    raise SystemExit(0 if report['passed']==len(results) else 1)


if __name__=='__main__':main()
