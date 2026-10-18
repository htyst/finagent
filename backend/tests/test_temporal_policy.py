"""Temporal component rules are tested without the removed debug HTTP router."""
import pytest
from app.planning.temporal import resolve, normalize, check
from app.planning.schema import validate_asset
from test_neuro import normal_plan

QUESTION = '每月转账需要固定日期，设置为每月1号可以吗？'

def user(text): return {'role': 'user', 'content': text}
def assistant(text): return {'role': 'assistant', 'content': text}

def normalized(messages, context, raw=None):
    state = resolve(messages, context)
    plan = validate_asset(raw or normal_plan('300'))
    normalize(plan, state)
    return state, plan


@pytest.mark.parametrize('text,expected', [('每月转300给张三', True), ('每月15号转300给张三', False), ('转300给张三', False)])
def test_three_required_acceptance_cases(text, expected):
    state, plan = normalized([user(text)], {})
    result = check(plan, state)
    assert bool(result) == expected
    if expected:
        assert result[0].rule_id == 'R10'
        assert plan.recurrence.day is None
    elif state['recurrence']:
        assert plan.recurrence.day == 15
    else:
        assert plan.recurrence is None


@pytest.mark.parametrize('day', [None, 1, 15])
def test_model_cannot_invent_monthly_or_date(day):
    raw = normal_plan('300'); raw['recurrence'] = {'type': 'recurring', 'freq': 'monthly', 'day': day}
    state, plan = normalized([user('从活期转300元到储蓄账户')], {}, raw)
    assert plan.recurrence is None and state['recurrence'] is None


def test_model_default_is_reset_and_backend_asks():
    raw = normal_plan('300'); raw['recurrence'] = {'type': 'recurring', 'freq': 'monthly', 'day': 1}
    state, plan = normalized([user('每月从活期转300元到储蓄账户')], {}, raw)
    assert plan.recurrence.day is None
    assert check(plan, state)[0].rule_id == 'R10'


def pending(messages, context):
    state = resolve(messages, context)
    context['temporal_pending'] = {'anchor': state['anchor'], 'suggested_day': 1, 'message': QUESTION}
    return messages + [assistant(QUESTION)]


def test_accept_default_requires_backend_question():
    context = {}; messages = pending([user('每月从活期转300元到储蓄账户')], context)
    state, plan = normalized(messages + [user('可以')], context)
    assert state['accepted_default'] and plan.recurrence.day == 1
    assert context['temporal_accepted']['rule_id'] == 'R10'
    assert not check(plan, state)


@pytest.mark.parametrize('reject', ['不行', '不要1号', '不选1号'])
def test_reject_default_then_supply_date(reject):
    context = {}; messages = pending([user('每月从活期转300元到储蓄账户')], context)
    messages += [user(reject)]
    state, plan = normalized(messages, context)
    assert state['default_rejected'] and plan.recurrence.day is None
    assert not state['accepted_default']
    state, plan = normalized(messages + [assistant('请告诉我每月几号转账（1至31号）。'), user('15号')], context)
    assert plan.recurrence.day == 15 and not check(plan, state)


def test_switch_to_single_clears_monthly():
    context = {}; messages = pending([user('每月从活期转300元到储蓄账户')], context)
    raw = normal_plan('300'); raw['recurrence'] = {'type': 'recurring', 'freq': 'monthly', 'day': 1}
    state, plan = normalized(messages + [user('不需要每月，只转一次')], context, raw)
    assert state['recurrence'] is None and plan.recurrence is None
    assert 'temporal_pending' not in context and 'temporal_accepted' not in context


def test_assistant_cannot_create_monthly_intent_or_default():
    messages = [user('转300给张三'), assistant(QUESTION), user('可以')]
    assert resolve(messages, {})['recurrence'] is None
    messages[0]['content'] = '每月转300给张三'
    state = resolve(messages, {})
    assert state['recurrence']['day'] is None and not state['accepted_default']


@pytest.mark.parametrize('text', ['每周转300元', '每天转300元', '每年转300元'])
def test_unsupported_recurrence_is_preserved_as_unsupported(text):
    state = resolve([user(text)], {})
    assert state['recurrence'] is None and state['unsupported'] == text
