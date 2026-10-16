import json
import pytest
from sqlalchemy import select
from app.agent.plans import validate_plan
from app.agent.orchestrator import default_plan, generate_plan, split_goals
from app.agent.registry import validate, execute
from app.data.models import AgentTask, AgentStep, DemoSession, Proposal
from app.domain.errors import DomainError


@pytest.mark.parametrize('plan', [
    [{'ordinal': 1, 'tool_name': 'shell'}],
    [{'ordinal': 1, 'tool_name': 'list_accounts', 'depends_on': [1]}],
    [{'ordinal': 1, 'tool_name': 'prepare_transfer'}],
    [{'ordinal': 1, 'tool_name': 'list_cards'}, {'ordinal': 2, 'tool_name': 'prepare_wealth', 'depends_on': [1]}],
    [{'ordinal': 1, 'tool_name': 'list_accounts'}, {'ordinal': 2, 'tool_name': 'list_cards', 'depends_on': [1], 'input_refs': [{'step': 1, 'path': 'secret'}]}],
])
def test_invalid_semantics_rejected(plan):
    with pytest.raises(DomainError):
        validate_plan(plan)


def test_goal_omission_and_parameter_rewrite_rejected():
    goals = [{'id': 'g1', 'tool': 'list_accounts', 'params': {}},
             {'id': 'g2', 'tool': 'read_wealth', 'params': {}}]
    plan = default_plan(goals)
    assert len(plan) == 2
    with pytest.raises(DomainError):
        validate_plan(plan[:1], goals)
    plan[1]['tool_name'] = 'prepare_transfer'
    with pytest.raises(DomainError):
        validate_plan(plan, goals)


def test_repairs_are_bounded_and_directed():
    goals = [{'id': 'g1', 'tool': 'list_accounts', 'params': {}}]
    calls = []
    def invalid(goals, violations, attempt):
        calls.append((attempt, list(violations)))
        return [{'ordinal': 1, 'tool_name': 'run_code'}]
    with pytest.raises(DomainError) as error:
        generate_plan(goals, 'live', generator=invalid)
    assert error.value.code == 'PLAN_REPAIR_EXHAUSTED'
    assert len(calls) == 3
    assert calls[1][1][0]['code'] == 'INVALID_PLAN'


def test_business_denial_never_causes_parameter_repair():
    calls = []
    def fail(*args):
        calls.append(1)
        raise DomainError('INSUFFICIENT_FUNDS', '余额不足')
    with pytest.raises(DomainError) as error:
        generate_plan([{'id': 'g1', 'tool': 'list_accounts', 'params': {}}], 'live', generator=fail)
    assert error.value.code == 'INSUFFICIENT_FUNDS'
    assert len(calls) == 1


def test_model_cannot_supply_owner_or_permission():
    with pytest.raises(ValueError):
        validate('list_accounts', {'owner_id': 'u2', 'authorized': True})
    with pytest.raises(DomainError):
        validate('confirm_proposal', {})


def test_registry_checks_session_owner(bank):
    db, _ = bank
    with pytest.raises(DomainError):
        execute(db, session_id='s1', owner_id='u2', name='list_accounts', arguments={})


def test_composite_reads_preserve_all_goals(bank):
    from app.agent.service import create
    db, _ = bank
    result = create(db, db.get(DemoSession, 's1'), 'mock', '先查余额，然后查理财持仓')
    assert len(result['requested_outcomes']) == 2
    assert result['status'] == 'completed'
    assert {'list_accounts', 'read_wealth'} <= {step['tool_name'] for step in result['steps']}


def test_multiwrite_is_listed_without_partial_draft(bank):
    from app.agent.service import create
    db, _ = bank
    result = create(db, db.get(DemoSession, 's1'), 'mock', '从活期账户转给张三100元，然后挂失尾号1234的卡')
    assert len(result['requested_outcomes']) == 2
    assert result['status'] == 'gathering_info'
    assert db.scalar(select(Proposal)) is None


def test_pause_handoff_and_termination_block_execution(bank):
    from app.agent.service import create
    from app.agent.control import change, assert_running
    db, _ = bank
    result = create(db, db.get(DemoSession, 's1'), 'mock', '查询余额')
    task = db.get(AgentTask, result['id'])
    change(db, task, 'pause', '测试中断')
    with pytest.raises(DomainError):
        assert_running(db, task.id)
    change(db, task, 'resume')
    assert_running(db, task.id)
    change(db, task, 'handoff', '需要澄清')
    with pytest.raises(DomainError):
        change(db, task, 'resume')
    change(db, task, 'resume', operator=True)
    change(db, task, 'terminate', operator=True)
    with pytest.raises(DomainError):
        assert_running(db, task.id)


def test_checkpoint_survives_dialogue_context_replacement(bank):
    from app.agent.service import create
    from app.agent.control import change, assert_running
    db, _ = bank
    result = create(db, db.get(DemoSession, 's1'), 'mock', '查询余额')
    task = db.get(AgentTask, result['id'])
    change(db, task, 'pause')
    task.context_json = '{}'
    db.commit()
    with pytest.raises(DomainError):
        assert_running(db, task.id)


def test_user_cannot_escape_operator_handoff_by_pausing(bank):
    from app.agent.service import create
    from app.agent.control import change, state
    db, _ = bank
    task = db.get(AgentTask, create(db, db.get(DemoSession, 's1'), 'mock', '查询余额')['id'])
    change(db, task, 'handoff', '需要坐席澄清')
    with pytest.raises(DomainError) as error:
        change(db, task, 'pause')
    assert error.value.code == 'OPERATOR_REQUIRED'
    assert state(db, task.id)['state'] == 'handoff'
    with pytest.raises(DomainError):
        change(db, task, 'resume')
    change(db, task, 'resume', operator=True)
    assert state(db, task.id)['state'] == 'running'


def test_new_request_and_pause_invalidate_late_plan(bank):
    from app.agent.service import create
    from app.agent.control import begin_request, assert_request, change
    db, _ = bank
    task = db.get(AgentTask, create(db, db.get(DemoSession, 's1'), 'mock', '查询余额')['id'])
    old = begin_request(db, task.id)
    current = begin_request(db, task.id)
    with pytest.raises(DomainError):
        assert_request(db, task.id, old)
    assert_request(db, task.id, current)
    change(db, task, 'pause')
    change(db, task, 'resume')
    with pytest.raises(DomainError):
        assert_request(db, task.id, current)


def test_composite_already_frozen_card_does_not_wait_for_nonexistent_order(bank):
    from app.agent.service import create, view
    from app.agent.orchestrator import install, run
    from app.data.models import Card
    db, _ = bank
    task = db.get(AgentTask, create(db, db.get(DemoSession, 's1'), 'mock', '查询余额')['id'])
    db.get(Card, 'c1a').status = 'frozen'
    db.commit()
    goals = [{'id': 'g1', 'tool': 'prepare_card_action', 'params': {'card_id': 'c1a', 'action': 'freeze'}},
             {'id': 'g2', 'tool': 'list_accounts', 'params': {}}]
    install(db, task, goals, default_plan(goals), [])
    run(db, task)
    result = view(db, task)
    assert result['status'] == 'completed'
    assert all(step['status'] == 'succeeded' for step in result['steps'])
    assert result['proposal'] is None


def test_composite_resumes_only_remaining_reads_after_user_confirmation(bank):
    from app.agent.service import create, send
    from app.agent.orchestrator import install, run
    from app.domain.operations import issue_confirmation_token, confirm_proposal
    from app.data.models import TransferOrder
    from sqlalchemy import func
    db, _ = bank
    task = db.get(AgentTask, create(db, db.get(DemoSession, 's1'), 'mock', '查询余额')['id'])
    goals = [{'id': 'g1', 'tool': 'prepare_transfer', 'params': {'source_account_id': 'a1c',
              'target': {'type': 'own_account', 'account_id': 'a1s'}, 'amount_minor': 10000}},
             {'id': 'g2', 'tool': 'read_wealth', 'params': {}}]
    install(db, task, goals, default_plan(goals), [])
    run(db, task)
    pid = task.proposal_id
    assert pid is not None
    token = issue_confirmation_token(db, session_id='s1', proposal_id=pid)['confirmation_token']
    confirm_proposal(db, session_id='s1', proposal_id=pid, confirmation_token=token)
    result = send(db, db.get(DemoSession, 's1'), task.id, '继续')
    assert result['status'] == 'completed'
    assert result['steps'][-1]['tool_name'] == 'read_wealth'
    assert result['steps'][-1]['status'] == 'succeeded'
    assert db.scalar(select(func.count()).select_from(TransferOrder)) == 1
