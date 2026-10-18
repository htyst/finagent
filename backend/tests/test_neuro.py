"""Planner component contracts plus integration with the current Live tool loop.

The old /api/debug/clock, /api/debug/trace and standing-order HTTP routes are
no longer registered. Exercise retained planner/standing components directly;
exercise Live through the production default instead of patching a dead router.
"""
from datetime import datetime, timedelta, timezone
import json
import pytest
from sqlalchemy import select, func
from app import standing
from app.agent import dynamic_protocol as protocol
from app.agent.service import create, send
from app.clock import configure, get_clock
from app.data.models import Account, AgentTask, AuditEvent, BusinessResult, DemoSession, Proposal, TransferOrder
from app.domain.errors import DomainError
from app.domain.operations import confirm_proposal, issue_confirmation_token, get_proposal
from app.neuro_models import StandingOccurrence
from app.planning import engine, temporal
from app.planning.schema import PlanInvalid, validate_asset
from app.planning.provider import generate
from app.verifier import verify, register, RULES, record
from test_dynamic_tools import calls, reply, sequence, session, transfers
from test_http import client


@pytest.fixture
def neuro(bank, monkeypatch):
    monkeypatch.setenv('FINAGENT_AGENT_ENGINE', 'dynamic')
    configure('manual', '2026-09-24')
    try:
        yield bank
    finally:
        configure('manual', '2026-09-24')


def normal_plan(amount='100'):
    return {'steps': [{'id': 'read', 'tool': 'list_accounts'},
        {'id': 'write', 'tool': 'prepare_transfer', 'depends_on': ['read'],
         'params': {'source': '活期账户', 'target': '储蓄账户', 'amount_yuan': amount}}]}


def params(amount=30000):
    return {'source_account_id': 'a1c', 'target': {'type': 'own_account', 'account_id': 'a1s'},
            'amount_minor': amount, 'currency': 'CNY'}


def activate(db, order):
    cid = order['confirmation']['id']
    proof = standing.issue_token(db, session(db), cid)['confirmation_token']
    return standing.confirm(db, session(db), cid, proof)


def prepare_order(db, day=1):
    return standing.prepare_order(db, session(db), params(), {'freq': 'monthly', 'day': day})


def page_confirm(db, proposal):
    pid = proposal['id']
    token = issue_confirmation_token(db, session_id='s1', proposal_id=pid)['confirmation_token']
    return confirm_proposal(db, session_id='s1', proposal_id=pid, confirmation_token=token)


def test_r1_and_observe(neuro):
    db, _ = neuro
    result = verify('prepare_transfer', {}, {'input': '如果工资到账才转', 'emotion': None})
    assert not result.approved and result.violations[0].rule_id == 'R1'
    @register('OBS_TEST', '观察演练')
    def rule(action, params, context):
        from app.verifier import Violation
        return Violation('OBS_TEST', 'amount', 1, 0, '仅观察')
    try:
        result = verify('list_accounts', {}, {})
        assert result.approved and result.would_block[0].rule_id == 'OBS_TEST'
        record(db, 'u1', None, 'test', result); db.commit()
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.event == 'would_block')) == 1
    finally:
        RULES.pop('OBS_TEST')


def test_read_write_evidence_and_no_model_commit(neuro, monkeypatch):
    db, _ = neuro
    sequence(monkeypatch, calls(('list_accounts', {})),
             calls(('prepare_transfer', {'source': '活期账户', 'target': '储蓄账户', 'amount_yuan': '100'})))
    result = create(db, session(db), 'live', '查余额，然后从活期账户转100元到储蓄账户')
    assert result['status'] == 'awaiting_confirmation'
    assert [s['tool_name'] for s in result['steps']] == ['list_accounts', 'prepare_transfer']
    assert result['proposal']['summary']['amount_minor'] == 10000
    assert transfers(db) == 0
    assert len(result['analysis_workflow']['nodes']) == 2


def test_bad_plan_repair_budget():
    raw = {'steps': [{'id': 'bad', 'tool': 'prepare_transfer'}]}; seen = []
    def generator(payload, feedback):
        seen.append(feedback); return raw
    with pytest.raises(PlanInvalid) as exc:
        generate({}, generator=generator)
    assert len(seen) == 3 and seen[1]['violations'][0]['rule_id'] == 'P_READ_BEFORE_WRITE'
    assert exc.value.violations[-1]['rule_id'] == 'P_REPAIR_LIMIT'
    values = iter([raw, normal_plan()])
    plan, rounds, history = generate({}, generator=lambda p, f: next(values))
    assert rounds == 1 and history and plan.steps[1].depends_on == ['read']


@pytest.mark.parametrize('text', ['如果工资到账才从活期账户转100元到储蓄账户',
                                  '若本月餐饮至少500元才从活期账户转100元到储蓄账户'])
def test_condition_cannot_become_unconditional(neuro, monkeypatch, text):
    db, _ = neuro
    sequence(monkeypatch, calls(('prepare_transfer', {'source': '活期账户', 'target': '储蓄账户', 'amount_yuan': '100'})),
             reply('条件尚未核实，没有准备转账。'))
    result = create(db, session(db), 'live', text)
    assert not result['proposal'] and transfers(db) == 0


def test_structured_condition_is_preserved_in_compiler(neuro):
    db, _ = neuro
    raw = {'steps': [{'id': 'spend', 'tool': 'summarize_spending', 'params': {'category': '餐饮', 'time': {'type': 'period', 'period': 'this_month'}}},
        {'id': 'write', 'tool': 'prepare_transfer', 'depends_on': ['spend'],
         'params': {'source': '活期', 'target': '储蓄', 'amount_yuan': '100'},
         'condition': {'step_ref': 'spend', 'field': 'net_minor', 'op': 'gt', 'value': '500'}}]}
    plan = validate_asset(raw)
    compiled = engine.compile_step(db, session(db), plan.steps[1], plan)
    assert compiled['amount_minor'] == 10000
    assert compiled['condition']['threshold_minor'] == 50000
    assert compiled['condition']['start'] == '2026-09-01'


def test_proposal_uses_real_expiry_not_debug_clock(neuro, monkeypatch):
    db, _ = neuro
    from app.domain.operations import prepare_transfer
    proposal = prepare_transfer(db, session_id='s1', **params(10000))['proposal']
    get_clock().advance(days=32)
    assert get_proposal(db, session_id='s1', proposal_id=proposal['id'])['status'] == 'pending_confirmation'
    import app.domain.operations as operations
    monkeypatch.setattr(operations, '_now', lambda value=None: value or datetime.now(timezone.utc) + timedelta(minutes=6))
    assert get_proposal(db, session_id='s1', proposal_id=proposal['id'])['status'] == 'expired'


def test_removed_debug_routes_are_not_exposed(neuro, client):
    client.cookies.set('finagent_demo_session', 's1')
    for path in ['/api/debug/clock', '/api/debug/trace/test', '/api/standing-orders']:
        assert client.get(path).status_code == 404


def test_standing_full_flow_idempotency_and_insufficient_funds(neuro):
    db, _ = neuro
    activate(db, prepare_order(db)); get_clock().advance(days=7)
    notices = standing.tick(db, session(db))
    assert notices and notices[0]['status'] == 'awaiting_confirmation'
    assert transfers(db) == 0 and standing.tick(db, session(db)) == []
    # Resolve the first draft before testing a later occurrence's funds check.
    from app.domain.operations import cancel_proposal
    cancel_proposal(db, session_id='s1', proposal_id=notices[0]['proposal_id'])
    db.get(Account, 'a1c').balance_minor = 100; db.commit()
    get_clock().advance(days=31)
    notices = standing.tick(db, session(db))
    assert notices[0]['status'] == 'skipped' and '余额不足' in notices[0]['message']
    # Restore the synthetic balance so the shared ledger invariant still holds.
    db.get(Account, 'a1c').balance_minor = 1000000; db.commit()


@pytest.mark.parametrize('year,day', [(2027, '2027-02-28'), (2028, '2028-02-29')])
def test_standing_month_end(year, day):
    assert standing.monthly_due(year, 2, 31).isoformat() == day


def test_standing_missing_day_requires_explicit_choice(neuro):
    db, _ = neuro
    with pytest.raises(DomainError): prepare_order(db, None)
    assert not db.scalar(select(func.count()).select_from(Proposal))


def test_standing_pause_resume_and_cancel_require_confirmation(neuro):
    db, _ = neuro
    order = activate(db, prepare_order(db)); oid = order['id']
    assert standing.manage(db, session(db), oid, 'pause')['status'] == 'paused'
    assert standing.manage(db, session(db), oid, 'resume')['status'] == 'active'
    cancellation = standing.manage(db, session(db), oid, 'cancel')
    assert cancellation['status'] == 'active'
    assert activate(db, cancellation)['status'] == 'cancelled'
    assert transfers(db) == 0


def test_multiwrite_requires_separate_fixed_authorizations(neuro, monkeypatch):
    db, _ = neuro
    sequence(monkeypatch, calls(('prepare_transfer', {'source': '活期账户', 'target': '储蓄账户', 'amount_yuan': '100'}),
                               ('prepare_transfer', {'source': '活期账户', 'target': '储蓄账户', 'amount_yuan': '200'})),
             reply('请逐笔办理并核对各自操作单。'))
    result = create(db, session(db), 'live', '从活期账户转100元到储蓄账户，再从活期账户转200元到储蓄账户')
    assert transfers(db) == 0
    assert all(op['status'] != 'executed' for op in result['authorization_operations'])


@pytest.mark.parametrize('raw', [
    {'steps': [{'id': 'x', 'tool': 'list_accounts', 'depends_on': ['x']}]},
    {'steps': [{'id': 'x', 'tool': 'summarize_spending', 'params': {'start': '2026-09-24'}}]},
    {'steps': [{'id': 'x', 'tool': 'confirm_proposal'}]},
])
def test_schema_dag_and_authority_rejected(raw):
    with pytest.raises(PlanInvalid): validate_asset(raw)


def test_urgent_request_still_requires_consent_and_owner_isolation(neuro, client, monkeypatch):
    db, _ = neuro
    sequence(monkeypatch, calls(('prepare_transfer', {'source': '活期账户', 'target': '储蓄账户', 'amount_yuan': '100'})))
    result = create(db, session(db), 'live', '赶紧从活期账户转100元到储蓄账户')
    assert result['status'] == 'awaiting_confirmation' and transfers(db) == 0
    client.cookies.set('finagent_demo_session', 'other')
    assert client.get('/api/tasks/' + result['id']).status_code == 404
    assert client.get('/api/proposals/' + result['proposal']['id']).status_code == 404


def test_snapshot_rule_still_blocks_execution(neuro, monkeypatch):
    db, _ = neuro
    sequence(monkeypatch, calls(('prepare_transfer', {'source': '活期账户', 'target': '储蓄账户', 'amount_yuan': '100'})))
    result = create(db, session(db), 'live', '从活期账户转100元到储蓄账户')
    db.get(Account, 'a1c').version += 1; db.commit()
    with pytest.raises(DomainError) as exc: page_confirm(db, result['proposal'])
    assert exc.value.code == 'STALE_RESOURCE' and transfers(db) == 0


def test_model_cannot_use_condition_threshold_as_transfer_amount(neuro):
    db, _ = neuro
    plan = validate_asset(normal_plan('500'))
    with pytest.raises(PlanInvalid) as exc:
        engine.semantic_check(db, session(db), plan, '如果本月餐饮超过500元，就从活期账户转100元到储蓄账户')
    errors = exc.value.violations
    assert any(v['rule_id'] == 'P_AMOUNT_BINDING' for v in errors)


def test_missing_source_stops_at_input_checkpoint(neuro, monkeypatch):
    db, _ = neuro
    sequence(monkeypatch, calls(('prepare_transfer', {'target': '储蓄账户', 'amount_yuan': '100'})))
    result = create(db, session(db), 'live', '转100元到储蓄账户')
    assert not result['proposal'] and transfers(db) == 0
    assert result['status'] == 'gathering_info'


def test_month_end_real_scheduler_occurrence(neuro):
    db, _ = neuro
    get_clock().set('2027-02-01'); activate(db, prepare_order(db, 31))
    get_clock().advance(days=27)
    notices = standing.tick(db, session(db))
    assert notices[0]['status'] == 'awaiting_confirmation' and transfers(db) == 0


def test_plan_provider_outage_never_falls_back_to_mock(neuro, monkeypatch):
    db, _ = neuro
    def failure(*args): raise DomainError('MODEL_UNAVAILABLE', '模型不可用', 503)
    sequence(monkeypatch, failure)
    result = create(db, session(db), 'live', '从活期账户转100元到储蓄账户')
    assert not result['proposal'] and transfers(db) == 0
    assert '已回退旧单场景模板' not in result['messages'][-1]['content']


def test_period_cannot_silently_change_to_default(neuro):
    db, _ = neuro
    raw = {'steps': [{'id': 'spend', 'tool': 'summarize_spending', 'params': {'category': '餐饮'}}]}
    with pytest.raises(PlanInvalid) as exc:
        engine.semantic_check(db, session(db), validate_asset(raw), '查上个月餐饮消费')
    errors = exc.value.violations
    assert any(v['rule_id'] == 'P_TIME_GROUNDING' for v in errors)


def test_calendar_jump_discovers_due_month(neuro):
    db, _ = neuro
    activate(db, prepare_order(db)); get_clock().advance(days=32)
    notices = standing.tick(db, session(db))
    assert notices[0]['period'] == '2026-10' and notices[0]['status'] == 'awaiting_confirmation'
    assert transfers(db) == 0


@pytest.mark.parametrize('amount', ['-100元', '1.234元', '负一百元', '一百二元', '100元5角', '两块五'])
def test_invalid_amount_never_becomes_positive_different_amount(neuro, amount):
    db, _ = neuro
    result = create(db, session(db), 'mock', '从活期账户转' + amount + '到储蓄账户')
    assert not result['proposal'] and result['status'] == 'gathering_info' and transfers(db) == 0


def test_task_status_compiler_rejects_unknown_owned_task(neuro):
    db, _ = neuro
    plan = validate_asset({'steps': [{'id': 'status', 'tool': 'get_task_status', 'params': {'task_id': 'unknown'}}]})
    with pytest.raises(engine.Clarify): engine.compile_step(db, session(db), plan.steps[0], plan)


def test_recurrence_on_read_step_is_schema_semantic_error():
    raw = {'steps': [{'id': 'read', 'tool': 'list_accounts', 'recurrence': {'type': 'recurring', 'freq': 'monthly', 'day': 1}},
                     {'id': 'write', 'tool': 'prepare_transfer', 'depends_on': ['read']}]}
    with pytest.raises(PlanInvalid) as error: validate_asset(raw)
    assert any(v['rule_id'] == 'P_RECURRENCE' for v in error.value.violations)
