"""Regressions for the 2026-10-19 dialogue sweep findings.

Covers:
 - natural-language task control ("先别转了") reaching manage_task
 - a reused goal id being renamed instead of voiding the whole batch
 - the prepared operation surviving such a rename
 - failure reasons being persisted on the execution-graph node
"""
import json
from uuid import uuid4

import pytest

from app.agent import dynamic_protocol as protocol, dynamic_runtime as runtime
from app.agent import execution_graph as graph
from app.agent.service import create, send
from app.data.models import AgentTask, DemoSession, TransferOrder
from sqlalchemy import func, select


@pytest.fixture
def dynamic(bank, monkeypatch):
    monkeypatch.setenv('FINAGENT_AGENT_ENGINE', 'dynamic')
    return bank


def calls(*items):
    return {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': uuid4().hex, 'type': 'function',
         'function': {'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}}
        for name, args in items]}


def reply(text='已核对，具体金额见下方。', **kw):
    return calls(('respond', {'text': text, **kw}))


def sequence(monkeypatch, *steps):
    it = iter(steps)

    def step(messages, context):
        item = next(it)
        return item(messages, context) if callable(item) else item

    monkeypatch.setattr(protocol, 'step', step)


def session(db):
    return db.get(DemoSession, 's1')


def state(db, tid):
    return runtime.get(db.get(AgentTask, tid))


def transfers(db):
    return db.scalar(select(func.count()).select_from(TransferOrder))


# ---------------------------------------------------------------- task control

@pytest.mark.parametrize('quote,utterance,expected', [
    ('先别转了', '先别转了', True),
    ('先不要转', '先不要转那笔钱', True),
    ('等等别转', '等等别转，我再看一下', True),
    ('暂停一下', '暂停一下', True),
    ('先别做', '先别做别的', True),
    ('先放着吧', '先放着吧', True),
    ('怎么暂停', '怎么暂停呢', False),
    ('如果暂停会怎样', '如果暂停会怎样', False),
    ('为什么先别转', '为什么先别转', False),
    ('先别转了', '帮我查下余额', False),
    ('', '先别转了', False),
])
def test_control_grounded_recognises_ordinary_pause_phrasing(quote, utterance, expected):
    assert runtime.control_grounded('pause', quote, utterance) is expected


@pytest.mark.parametrize('action,quote,utterance,expected', [
    ('cancel', '算了不转了', '算了不转了', True),
    ('cancel', '全部取消吧', '全部取消吧', True),
    ('cancel', '不要取消', '不要取消', False),
    ('continue', '继续吧', '继续吧', True),
    ('continue', '别继续了', '别继续了', False),
    ('handoff', '帮我转人工', '帮我转人工', True),
    ('handoff', '不要转人工', '不要转人工', False),
])
def test_control_grounded_keeps_negation_and_question_guards(action, quote, utterance, expected):
    assert runtime.control_grounded(action, quote, utterance) is expected


def test_pause_by_natural_phrasing_actually_pauses_the_task(dynamic, monkeypatch):
    db, _ = dynamic
    sequence(monkeypatch,
             calls(('manage_task', {'action': 'pause',
                                    'instruction_quote': '先别转了'})))
    result = create(db, session(db), 'live', '先别转了')
    assert result['status'] == 'paused'
    codes = [n.get('error_code') for n in state(db, result['id'])['nodes']]
    assert 'UNGROUNDED_CONTROL' not in codes, codes


def test_control_action_is_resolved_from_the_users_own_words(dynamic, monkeypatch):
    """The model mislabelling "先别转了" as cancel must not lose the instruction."""
    db, _ = dynamic
    sequence(monkeypatch,
             calls(('manage_task', {'action': 'cancel',
                                    'instruction_quote': '先别转了'})))
    result = create(db, session(db), 'live', '先别转了')
    assert result['status'] == 'paused', result['status']
    codes = [n.get('error_code') for n in state(db, result['id'])['nodes']]
    assert 'UNGROUNDED_CONTROL' not in codes, codes


def test_control_phrase_grounding_to_no_action_is_still_rejected(dynamic, monkeypatch):
    db, _ = dynamic
    sequence(monkeypatch,
             calls(('manage_task', {'action': 'cancel',
                                    'instruction_quote': '帮我查下余额'})),
             reply('这只是一次查询，没有任何任务被取消。'))
    result = create(db, session(db), 'live', '帮我查下余额')
    codes = [n.get('error_code') for n in state(db, result['id'])['nodes']]
    assert 'UNGROUNDED_CONTROL' in codes, codes
    assert result['status'] != 'cancelled'


def test_pause_phrasing_is_rejected_when_it_is_only_a_question(dynamic, monkeypatch):
    db, _ = dynamic
    sequence(monkeypatch,
             calls(('manage_task', {'action': 'pause',
                                    'instruction_quote': '怎么暂停'})),
             reply('暂停需要你直接说“先别转了”这类明确指令。'))
    result = create(db, session(db), 'live', '怎么暂停')
    assert result['status'] != 'paused'
    codes = [n.get('error_code') for n in state(db, result['id'])['nodes']]
    assert 'UNGROUNDED_CONTROL' in codes, codes


# ------------------------------------------------------- answer number guard (P1-1)

from datetime import date
from types import SimpleNamespace

from app.agent import analysis_workflow as aw
from app.agent import execution_graph as _graph


def _runtime():
    return {
        'id': 'r1',
        'graph_schema': _graph.SCHEMA_VERSION,
        'request': '618那阵子我网购花了多少，最近三个月',
        'user_messages': ['618那阵子我网购花了多少，最近三个月'],
        'goals': [dict(id='g1', text='查消费', quote='最近三个月', kind='query',
                       status='running', implicit=True)],
        'nodes': [], 'queue': [],
        'facts': [dict(id='f1', key='net_spending', label='净消费', value='1084.02',
                       unit='yuan', source_ref='call:r1:s1'),
                  dict(id='f2', key='transaction_count', label='笔数', value='35',
                       unit='number', source_ref='call:r1:s1')],
        'evidence': [dict(tool='summarize_spending', source_ref='call:r1:s1',
                          query={'start': '2026-09-01', 'end': '2026-09-24'},
                          data={'period_start': '2026-09-01', 'period_end': '2026-09-24'})],
    }


@pytest.mark.parametrize('prose,expected', [
    # verified amount + verified count
    ('餐饮共 1,084.02 元，共 35 笔。', []),
    # partial date renderings of a verified range
    ('统计区间为 2026-09-01 至 09-24，覆盖完整。', []),
    ('区间 9月1日 到 9月24日。', []),
    # the user's own colloquial number
    ('618期间消费 1,084.02 元。', []),
    # a stray number must not ride on a verified date on the same line
    ('2026-09-01 之后花了 24 元。', ['24']),
    # genuinely unsupported numbers stay rejected
    ('你大概花了 900 元。', ['900']),
    ('平均每笔约 31 元。', ['31']),
    ('收益 5% 以上。', ['5']),
    # the user's own duration/quantities may be echoed back
    ('最近三个月共 1,084.02 元。', []),
    # but a span the user never said is still a claim
    ('最近两个月共 1,084.02 元。', ['两个月']),
])
def test_unverified_claims_traces_numbers_to_facts_dates_or_user_words(prose, expected):
    assert aw.unverified_claims(prose, _runtime()) == expected


@pytest.mark.parametrize('prose', [
    '已成功转账 1,084.02 元。',
    '这笔申购保证收益。',
    '已确认盗刷。',
    '可以绕过确认直接办理。',
])
def test_business_assertions_stay_forbidden_even_with_verified_numbers(prose):
    with pytest.raises(Exception) as caught:
        aw.render(aw.Answer(answer_template=prose, evidence_refs=['call:r1:s1']), _runtime())
    assert 'ANALYSIS_ANSWER_INVALID' in str(getattr(caught.value, 'code', caught.value))


def _respond_and_render(text, r):
    return runtime.render_response(protocol.Respond(text=text, evidence_refs=[]), r)


@pytest.mark.parametrize('text', [
    '最近三个月餐饮共 {{f1}}，共 {{f2}} 笔，统计区间 2026-09-01 至 09-24，覆盖完整。',
    '618那阵子消费 {{f1}}，共 {{f2}} 笔。',
    '统计区间 9月1日 到 9月24日，餐饮共 {{f1}}，共 {{f2}} 笔。',
])
def test_sanitiser_lets_natural_phrasings_through(text):
    shown, used = _respond_and_render(text, _runtime())
    assert '{{' not in shown and '}}' not in shown
    assert '1,084.02' in shown and '35' in shown
    assert used


def test_count_fact_without_its_unit_is_rejected_not_rendered_bare():
    """The reported defect: "共 982.93 元、32，" left a unitless number in the answer."""
    with pytest.raises(Exception) as caught:
        _respond_and_render('上个月餐饮共 {{f1}}、{{f2}}，没有退款。', _runtime())
    assert 'ANALYSIS_UNIT' in str(getattr(caught.value, 'code', caught.value))


def test_sanitiser_still_rejects_a_fabricated_amount():
    with pytest.raises(Exception) as caught:
        _respond_and_render('餐饮共 {{f1}}，另外大概又多花了 900 元。', _runtime())
    assert 'ANALYSIS_ANSWER_INVALID' in str(getattr(caught.value, 'code', caught.value))


def _runtime_with_goal(gid='g1'):
    return {'graph_schema': graph.SCHEMA_VERSION, 'request': '从活期账户转100元到储蓄账户',
            'goals': [dict(id=gid, text='旧查询', quote='旧查询', kind='answer',
                           status='waiting_input', implicit=True)],
            'nodes': [], 'turn_start_node_count': 1, 'facts': [], 'evidence': []}


def test_reused_goal_id_is_renamed_not_rejected():
    r = _runtime_with_goal()
    out = graph.declare(r, {'goals': [{'id': 'g1', 'text': '给张三转子账户500元',
                                       'quote': '从活期账户转100元到储蓄账户',
                                       'kind': 'operation'}]})
    assert out['renamed_goal_ids'] == {'g1': 'g2'}
    ids = [g['id'] for g in r['goals']]
    assert ids == ['g1', 'g2'], ids
    # The pre-existing goal keeps its own meaning; it is never overwritten.
    assert r['goals'][0]['text'] == '旧查询'
    assert r['turn_goal_ids'] == ['g2']
    assert 'g1→g2' in out['next']


def test_fresh_goal_id_declares_without_renaming():
    r = _runtime_with_goal()
    out = graph.declare(r, {'goals': [{'id': 'g2', 'text': '转账',
                                       'quote': '从活期账户转100元到储蓄账户',
                                       'kind': 'operation'}]})
    assert 'renamed_goal_ids' not in out
    assert [g['id'] for g in r['goals']] == ['g1', 'g2']


def test_reused_goal_id_keeps_the_prepared_transfer_alive(dynamic, monkeypatch):
    """The reported defect: a reused id discarded a fully specified transfer."""
    db, _ = dynamic
    sequence(monkeypatch, calls(('list_accounts', {})), reply('余额见下方。'))
    first = create(db, session(db), 'live', '查看我的账户余额')
    assert first['status'] == 'completed'

    sequence(monkeypatch,
             calls(('declare_goals', {'goals': [
                 {'id': 'g1', 'text': '从活期账户转100元到储蓄账户',
                  'quote': '从活期账户转100元到储蓄账户', 'kind': 'operation'}]}),
                 ('prepare_transfer', {
                     'source': '活期账户', 'target': '储蓄账户', 'amount_yuan': '100',
                     'operation_context': {'instruction_quote': '从活期账户转100元到储蓄账户'}})))
    result = send(db, session(db), first['id'], '从活期账户转100元到储蓄账户')
    tid = result['id']

    assert result['proposal'] is not None
    assert result['proposal']['status'] == 'pending_confirmation'
    assert result['proposal']['summary']['amount_minor'] == 10000
    nodes = state(db, tid)['nodes']
    declare = next(n for n in nodes if n['name'] == 'declare_goals')
    assert declare['status'] == 'succeeded' and not declare.get('error_code')
    prepared = next(n for n in nodes if n['name'] == 'prepare_transfer')
    assert prepared.get('error_code') is None, prepared.get('error_code')
    assert transfers(db) == 0, 'preparation must never move money'


# --------------------------------------------------- demo-clock period grounding (3.1)

DEMO_DAY = date(2026, 9, 24)


@pytest.mark.parametrize('request_text,expected', [
    ('这个月餐饮花了多少', ('这个月', '2026-09-01', '2026-09-24')),
    ('本月消费情况', ('这个月', '2026-09-01', '2026-09-24')),
    ('帮我看看上个月账单', ('上个月', '2026-08-01', '2026-08-31')),
    ('上月花了多少', ('上个月', '2026-08-01', '2026-08-31')),
    ('今天的消费', ('今天', '2026-09-24', '2026-09-24')),
    ('昨天花了多少', ('昨天', '2026-09-23', '2026-09-23')),
    # not policed: comparisons, rolling windows, and requests with no period word
    ('这个月和上个月比', None),
    ('上个月比这个月少多少', None),
    ('最近三个月餐饮', None),
    ('随便查查', None),
])
def test_expected_period_resolves_against_the_demo_clock(request_text, expected):
    assert runtime.expected_period(request_text, DEMO_DAY) == expected


def test_real_time_dates_are_rejected_for_a_demo_clock_period():
    message = runtime.period_mismatch_message(
        'summarize_spending', {'start': '2026-10-01', 'end': '2026-10-19'},
        '这个月餐饮花了多少', DEMO_DAY)
    assert message and '2026-09-01' in message and '2026-09-24' in message


def test_matching_demo_clock_dates_pass():
    assert runtime.period_mismatch_message(
        'summarize_spending', {'start': '2026-09-01', 'end': '2026-09-24'},
        '这个月餐饮花了多少', DEMO_DAY) is None


def test_last_month_may_not_omit_the_range_because_the_default_is_this_month():
    message = runtime.period_mismatch_message('summarize_spending', {}, '帮我看看上个月账单', DEMO_DAY)
    assert message and '2026-08-01' in message


def test_this_month_may_omit_the_range_because_the_default_already_matches():
    assert runtime.period_mismatch_message('summarize_spending', {}, '这个月餐饮花了多少', DEMO_DAY) is None


def test_period_guard_only_covers_the_reads_that_take_a_range():
    assert runtime.period_mismatch_message('list_accounts', {'start': '2026-10-01'},
                                          '这个月餐饮花了多少', DEMO_DAY) is None


def test_wrong_month_is_rejected_at_execution(dynamic, monkeypatch):
    db, _ = dynamic
    sequence(monkeypatch,
             calls(('summarize_spending', {'category': '餐饮',
                                           'start': '2026-10-01', 'end': '2026-10-19'})),
             reply('已改为按演示日期所在月份统计。'))
    result = create(db, session(db), 'live', '这个月餐饮花了多少')
    codes = [n.get('error_code') for n in state(db, result['id'])['nodes']]
    assert 'PERIOD_UNGROUNDED' in codes, codes


def test_uncovered_window_cannot_be_reported_as_a_zero_total():
    uncovered = {'coverage': {'status': 'incomplete', 'segments': []},
                 'totals': {'is_complete': False, 'expense_minor': 0}}
    r = _runtime()
    r['evidence'] = [dict(tool='summarize_spending', source_ref='call:r1:s1', query={},
                          raw_data=uncovered, data=uncovered)]
    r['facts'] = [dict(id='f1', key='net_spending', label='净消费', value='0.00',
                       unit='yuan', source_ref='call:r1:s1')]
    with pytest.raises(Exception) as caught:
        _respond_and_render('这个区间餐饮净消费 {{f1}}，覆盖完整。', r)
    assert 'ANALYSIS_INCOMPLETE' in str(getattr(caught.value, 'code', caught.value))


# ------------------------------------------------ closing text and subject scope (3.2/3.3)


def _goal(kind, status):
    return dict(id='g9', text='办理', quote='办理', kind=kind, status=status)


def test_closing_text_reports_a_prepared_order_instead_of_please_supply():
    text = runtime.closing_text({'proposal': {'status': 'pending_confirmation'}}, {'goals': []}, [])
    assert '已准备' in text and '转账或卡片操作单' in text
    assert '待补充' not in text


def test_closing_text_admits_no_order_was_generated_for_an_operation_goal():
    text = runtime.closing_text({}, {'goals': [_goal('operation', 'waiting_input')]}, [])
    assert '还没有生成办理固定单' in text
    assert '待补充' not in text


def test_closing_text_shows_both_order_and_query_result():
    visible = [{'tool': 'list_accounts', 'source_ref': 'call:r1:s1', 'data': [], 'raw_data': {}, 'query': {}}]
    text = runtime.closing_text({'proposal': {'status': 'pending_confirmation'}}, {'goals': []}, visible)
    assert '已准备' in text and '独立查询已完成' in text


def test_closing_text_stays_silent_when_only_a_query_answered():
    visible = [{'tool': 'list_accounts', 'source_ref': 'call:r1:s1', 'data': [], 'raw_data': {}, 'query': {}}]
    assert '还没有生成办理固定单' not in runtime.closing_text({}, {'goals': []}, visible)


def test_asking_about_another_person_is_refused_not_answered_as_self(dynamic, monkeypatch):
    db, _ = dynamic
    sequence(monkeypatch,
             calls(('list_accounts', {})),
             reply('只能查询你本人（林晓）的账户，不能查询他人账户。'))
    result = create(db, session(db), 'live', '查一下陈予的账户余额')
    codes = [n.get('error_code') for n in state(db, result['id'])['nodes']]
    assert 'FOREIGN_SUBJECT' in codes, codes
    assert '只能查询你本人' in result['messages'][-1]['content']


def test_same_name_payee_is_not_treated_as_another_person(dynamic, monkeypatch):
    """u1 has two payees named 张三; that is disambiguation, not a subject refusal."""
    db, _ = dynamic
    sequence(monkeypatch,
             calls(('prepare_transfer', {'source': '活期账户', 'target': '张三', 'amount_yuan': '500'})))
    result = create(db, session(db), 'live', '从活期账户给张三转500')
    codes = [n.get('error_code') for n in state(db, result['id'])['nodes']]
    assert 'FOREIGN_SUBJECT' not in codes, codes


# ------------------------------------------------ stale prohibition lifecycle (A2)


@pytest.mark.parametrize('prohibitions,request_text,expected', [
    # an earlier "先别取消" no longer blocks an explicit later request
    (['先别取消'], '确认取消刚才那个', []),
    (['先别取消'], '算了，全部取消吧', []),
    (['先别取消'], '把最贵的订阅取消', []),
    # it still applies while the user has not contradicted it
    (['先别取消'], '先别取消', ['先别取消']),
    (['先别取消'], '帮我查下余额', ['先别取消']),
    # only cancel-class actions are superseded, not transfer prohibitions
    (['别转给张三'], '给张三转500', ['别转给张三']),
    # unrelated prohibitions survive
    (['不解除银行代扣'], '取消订阅', ['不解除银行代扣']),
    ([], '取消订阅', []),
])
def test_stale_prohibition_is_superseded_only_by_a_contradicting_request(prohibitions, request_text, expected):
    assert runtime.supersede_stale_prohibitions(prohibitions, request_text) == expected


# ------------------------------------------------- ordinal disambiguation (A3)


@pytest.mark.parametrize('text,expected', [
    ('第一个', '一'), ('第1个', '1'), ('1', '1'), ('第一个吧', '一'),
    ('第二个。', '二'), ('选第2个', '2'), ('第三个', '三'), ('第十个', '十'),
    # must not swallow anything that is not a bare ordinal choice
    ('两个都要', None), ('随便一个', None), ('3821', None), ('尾号3821', None), ('第一个和第二个', None),
])
def test_ordinal_choice_pattern_matches_only_a_bare_ordinal(text, expected):
    pattern = r'\s*(?:选)?\s*(?:第)?\s*([一二三四五六七八九十]|[1-9])\s*(?:个|项|位)?\s*(?:吧|呢|啊)?\s*[。！!，,、\s]*'
    match = __import__('re').fullmatch(pattern, text)
    assert (match.group(1) if match else None) == expected


# ------------------------------------------ three product decisions (2026-10-19)

from app.agent import analysis_workflow as _aw
from app.domain import security as _security


def test_comparison_window_returned_by_the_tool_is_traceable():
    """决策三：工具自己算出的对比区间应被允许，模型自造的日期仍被拒。"""
    r = _runtime()
    r['evidence'] = [dict(tool='summarize_spending', source_ref='call:r1:s1', query={}, raw_data={},
                          data={'period': {'start_date': '2026-06-24', 'end_exclusive_date': '2026-09-25'},
                                'comparison': {'period': {'start_date': '2026-03-24',
                                                          'end_exclusive_date': '2026-06-24'}}})]
    forms = _aw._verified_date_forms(r)
    for probe in ('2026-03-24', '6月23日', '3-24', '2026-06-23', '2026-09-24'):
        assert probe in forms, probe
    assert '2026-01-05' not in forms


def test_fabricated_window_is_still_rejected_after_comparison_support():
    r = _runtime()
    r['evidence'] = [dict(tool='summarize_spending', source_ref='call:r1:s1', query={}, raw_data={},
                          data={'period': {'start_date': '2026-06-24', 'end_exclusive_date': '2026-09-25'},
                                'comparison': {'period': {'start_date': '2026-03-24',
                                                          'end_exclusive_date': '2026-06-24'}}})]
    found = _aw.unverified_claims('对比区间 2026-01-05 至 2026-02-05 下降明显。', r)
    assert '01' in found and '05' in found and '02' in found


def test_subscription_access_slides_on_each_successful_check(dynamic):
    """决策二：一次确认覆盖一次连续操作，但不会复活已过期的授权。"""
    from datetime import datetime, timedelta, timezone
    from app.data.security_models import SecurityAuthorization
    from app.domain.errors import DomainError
    db, _ = dynamic
    _security.confirm_subscription_access(db, 's1')
    db.commit()
    row = db.scalar(__import__('sqlalchemy').select(SecurityAuthorization).where(
        SecurityAuthorization.resource == 'subscription-query:s1'))
    # near expiry, a successful check must extend the window
    row.expires_at = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
    db.commit()
    before = row.expires_at
    _security.assert_subscription_access(db, 's1')
    db.commit()
    db.refresh(row)
    assert datetime.fromisoformat(row.expires_at) > datetime.fromisoformat(before)
    # once genuinely expired, it must NOT be revived by the check itself
    row.expires_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    db.commit()
    with pytest.raises(DomainError) as caught:
        _security.assert_subscription_access(db, 's1')
    assert 'SUBSCRIPTION_CONFIRMATION_REQUIRED' in str(getattr(caught.value, 'code', caught.value))


def test_cancel_order_shows_why_that_contract_was_chosen(dynamic, monkeypatch):
    """决策一：取消单必须让用户能核对"最贵"这个指代，而不只是核对动作。"""
    from app.agent.conversation_reads import confirm_subscription_query
    from app.data.servicing_models import MerchantContractState
    db, _ = dynamic
    for key, name, amount, enabled in [('cheap', '音乐', 1500, True), ('chosen', '影音', 2500, True),
                                       ('off', '工具', 3900, False)]:
        from test_servicing import add
        add(db, 'subscriptions', key, dict(subscription_id=key, service_name=name, owner_id='u1',
            auto_renew_enabled=str(enabled).lower(), status='active', current_price_minor=str(amount),
            cycle_months='1', billing_cycle='monthly', access_end_date='2026-10-30',
            next_billing_date='2026-10-30'))
    sequence(monkeypatch, calls(('read_subscriptions', {})), calls(('prepare_merchant_contract_cancel', {})))
    result = create(db, session(db), 'live', '查一下我的订阅扣费，把最贵的那项取消')
    if result.get('next_actions'):
        result = confirm_subscription_query(db, session(db), result['id'], result['next_actions'][0]['pending_id'])
    summary = result['service_request']['summary']
    assert summary['subject_id'] == 'chosen'
    basis = summary.get('selection_basis') or ''
    assert '最贵' in basis and '影音' in basis
    assert '工具' in basis and '已关闭续费' in basis
    assert not db.scalar(__import__('sqlalchemy').select(__import__('sqlalchemy').func.count()).select_from(MerchantContractState))


# ------------------------------------------------------------------- audit trail

def test_failed_node_keeps_a_human_readable_reason(dynamic, monkeypatch):
    db, _ = dynamic
    sequence(monkeypatch,
             calls(('summarize_spending', {'category': '网购', 'comparison_mode': 'none'})),
             reply('该分类不在账本里，请改用账本分类。'))
    result = create(db, session(db), 'live', '618网购花了多少')
    nodes = state(db, result['id'])['nodes']
    failed = next(n for n in nodes if n.get('error_code'))
    assert failed['error_code'] == 'INVALID_CATEGORY'
    assert failed.get('error_message'), 'audit must retain why the call failed'
