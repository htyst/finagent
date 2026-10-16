"""Reproducible, separately labelled Mock/Live evaluation on synthetic TEST-ONLY data."""
import argparse
from datetime import date, datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import sys
from time import perf_counter
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['mock', 'live'], default='mock')
    parser.add_argument('--split', choices=['dev', 'holdout'], default='dev')
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--ids', default='', help='Comma-separated case IDs; never mixes dev and holdout')
    args = parser.parse_args()
    dataset = json.loads((ROOT / 'backend/evals/dialogue-v1.json').read_text(encoding='utf-8'))
    cases = [c for c in dataset['cases'] if c['split'] == args.split]
    if args.ids:
        selected = set(args.ids.split(',')); cases = [c for c in cases if c['id'] in selected]
        if {c['id'] for c in cases} != selected: parser.error('Unknown ID or ID outside selected split')
    if args.limit > 0: cases = cases[:args.limit]
    run = ROOT / 'var/TEST-ONLY' / ('dialogue-eval-' + uuid4().hex)
    run.mkdir(parents=True)
    os.environ['FINAGENT_DB_PATH'] = str(run / 'eval.sqlite3')
    os.environ['FINAGENT_SCHEDULER_ENABLED'] = '0'
    from sqlalchemy import func, select
    from sqlalchemy.orm import sessionmaker
    from app.data.models import Base, DemoSession, DemoSetting, Transaction, TransferOrder
    from app.data import security_models  # noqa: F401
    from app.data.db import make_engine
    from app.data.seed import reset_demo, check_ledger
    from app.agent.service import create, send
    from app.agent.telemetry import collect
    from app.domain.errors import DomainError
    engine = make_engine(Path(os.environ['FINAGENT_DB_PATH']))
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db: reset_demo(db, date(2026, 9, 24))
    results, usages, latencies = [], [], []
    for case in cases:
        with factory() as db:
            sid = uuid4().hex
            session = DemoSession(id=sid, owner_id='u1', dataset_version=db.get(DemoSetting, 1).dataset_version,
                expires_at_utc=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat(), csrf_token='evaluation-only')
            db.add(session); db.commit()
            if case.get('setup') == 'risk_r1':
                from app.domain.wealth import submit_assessment
                submit_assessment(db, session_id=sid, answers=[0,0,0,0])
            before = db.scalar(select(func.count()).select_from(Transaction))
            started = perf_counter()
            result, error_code = None, None
            with collect() as attempts:
                try:
                    for text in case.get('turns', [case['text']]):
                        result = send(db, session, result['id'], text) if result and result['status'] not in {'completed','cancelled','failed','expired'} else create(db, session, args.mode, text)
                except DomainError as error: error_code = error.code
            elapsed = perf_counter()-started
            latencies.append(elapsed)
            usages.extend(a['usage'] for a in attempts if a.get('usage'))
            after = db.scalar(select(func.count()).select_from(Transaction))
            matched = result is not None and result['status'] in case['statuses']
            if matched and case.get('tool'): matched = case['tool'] in [s['tool_name'] for s in result['steps']]
            if matched and case.get('intent'): matched = result['intent'] == case['intent']
            if matched and case.get('reply_contains'):
                matched = all(word in result['messages'][-1]['content'] for word in case['reply_contains'])
            if matched and case.get('target_id'):
                proposal = result.get('proposal')
                matched = bool(proposal and proposal['summary']['target'].get('id') == case['target_id'])
            if check_ledger(db): raise RuntimeError('Isolated evaluation ledger mismatch')
            results.append({'id': case['id'], 'category': case['category'], 'passed': bool(matched and after == before),
                'status': result['status'] if result else None, 'intent': result['intent'] if result else None,
                'error_code': error_code, 'unauthorized_postings': after-before,
                'model_attempts': len(attempts), 'seconds': round(elapsed, 6)})
    def percentile(p):
        return round(sorted(latencies)[max(0, math.ceil(len(latencies)*p)-1)], 6) if latencies else None
    passed = sum(r['passed'] for r in results)
    report = {'mode': args.mode, 'split': args.split, 'dataset_version': dataset['version'],
        'review_status': dataset['review_status'], 'denominator': len(results), 'passed': passed,
        'goal_or_safe_clarification_rate': passed/len(results) if results else None,
        'unauthorized_postings': sum(r['unauthorized_postings'] for r in results),
        'p50_seconds': percentile(.5), 'p95_seconds': percentile(.95),
        'provider_usage_records': len(usages),
        'provider_tokens': {k: sum(u.get(k, 0) for u in usages) for k in ('prompt_tokens','completion_tokens','total_tokens')} if usages else None,
        'cost': None, 'cost_note': 'No provider billing record; cost not estimated',
        'model_attempts': sum(r['model_attempts'] for r in results),
        'plan_repairs': None, 'plan_repairs_note': 'Not inferred from HTTP attempts; inspect task plan violations separately',
        'results': results}
    output = run / 'report.json'; output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'report': str(output), **{k:v for k,v in report.items() if k != 'results'}}, ensure_ascii=False, indent=2))
    engine.dispose()
    raise SystemExit(0 if passed == len(results) else 1)


if __name__ == '__main__': main()
