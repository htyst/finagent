import json
from copy import deepcopy
from types import SimpleNamespace
import pytest
from app.agent import dynamic_runtime as runtime,dynamic_protocol as protocol,read_presentation as presentation
from app.agent.service import create
from app.domain.errors import DomainError
from app.data.models import DemoSession
from test_dynamic_tools import dynamic,sequence,calls,transfers


def period(start='2026-03-24',end='2026-09-25'):
    return {'start_date':start,'end_exclusive_date':end,'timezone':'Asia/Shanghai'}


def spending(complete=True,comparison=True):
    data={'category':'餐饮','current':{'period':period(),'totals':{'is_complete':complete,'purchase_minor':570285,
        'refund_minor':0,'net_minor':570285,'purchase_count':202,'refund_count':0},'categories':[
        {'category':'餐饮','purchase_minor':570285,'refund_minor':0,'net_minor':570285}],
        'coverage':{'status':'complete' if complete else 'incomplete'}}}
    if comparison:data['comparison']={'period':period('2025-09-24','2026-03-24'),
        'totals':{'is_complete':False,'purchase_minor':666153,'refund_minor':0,'net_minor':666153},
        'delta_minor':None,'percent_change':None}
    return data


def observed(data):
    r={'id':'test','request':'近半年内饮食相关的支出','facts':[],'evidence':[],'nodes':[],'user_messages':[]}
    node={'id':'s1','name':'summarize_spending','arguments':{'start':'2026-03-24','end':'2026-09-24'},'status':'pending'}
    runtime.observation(r,node,data);return r


def test_full_current_period_and_partial_comparison_do_not_block_advice():
    r=observed(spending());f=next(f for f in r['facts'] if f['key']=='current.totals.net_minor')
    text,_=runtime.render_response(protocol.Respond(text='本期餐饮净消费{{'+f['id']+'}}。上期数据不完整，不能据此判断实际消费下降。建议减少非必要外出就餐频次。',evidence_refs=[f['source_ref']]),r)
    assert '5,702.85 元' in text
    assert not f.get('data_complete') and any(f.get('data_complete') is False for f in r['facts'])
    with pytest.raises(DomainError):runtime.render_response(protocol.Respond(text='本期餐饮消费明显下降。',evidence_refs=[f['source_ref']]),r)


@pytest.mark.parametrize('complete',[True,False])
def test_fallback_renders_known_period_and_unique_amounts_without_internal_fields(complete):
    r=observed(spending(complete,comparison=False));text=presentation.fallback(r,'ANALYSIS_INCOMPLETE')
    assert '2026-03-24 至 2026-09-24' in text and text.count('5,702.85')==2
    assert ('已记录' in text)==(not complete)
    assert ('流水缺失' in text)==(not complete)
    assert not any(x in text for x in ['current.','comparison.','核算依据','重做付款','月份数据有缺口，只能列'])


def test_read_timeout_preserves_summary_without_unrelated_payment_message(dynamic,monkeypatch):
    db,_=dynamic
    monkeypatch.setattr(runtime.registry,'execute',lambda *a,**k:spending(comparison=False))
    def unavailable(*a):raise DomainError('MODEL_UNAVAILABLE','internal provider timeout',503)
    sequence(monkeypatch,calls(('summarize_spending',{'start':'2026-03-24','end':'2026-09-24','category':'餐饮','comparison_mode':'none'})),unavailable)
    result=create(db,db.get(DemoSession,'s1'),'live','近半年内饮食相关的支出');text=result['messages'][-1]['content']
    assert result['status']=='partially_completed' and '5,702.85 元' in text and '进一步分析暂未完成' in text
    assert not any(x in text for x in ['current.','付款','internal provider timeout']) and transfers(db)==0


def test_count_facts_and_placeholder_units_are_checked():
    r=observed(spending());count=next(f for f in r['facts'] if f['key']=='current.totals.purchase_count')
    money=next(f for f in r['facts'] if f['key']=='current.totals.purchase_minor')
    text,_=runtime.render_response(protocol.Respond(text='共{{'+count['id']+'}}笔，消费{{'+money['id']+'}}元。',evidence_refs=[count['source_ref']]),r)
    assert '202笔' in text and '元元' not in text
    with pytest.raises(DomainError):runtime.render_response(protocol.Respond(text='共{{'+money['id']+'}}笔。',evidence_refs=[money['source_ref']]),r)


def test_generic_fact_fallback_strips_field_paths_and_duplicates():
    data={'facts':[{'source_ref':'a','id':'f1','node':'s1','tool':'summarize_spending','key':key,'label':'餐饮：消费（'+key+'）','value':'5702.85','unit':'yuan'}
        for key in ('current.totals.purchase_minor','current.categories.0.purchase_minor')]}
    text=presentation.fact_block(data)
    assert text.count('5,702.85')==1 and 'current.' not in text


def test_verified_spending_answer_does_not_append_duplicate_totals(dynamic,monkeypatch):
    db,_=dynamic
    monkeypatch.setattr(runtime.registry,'execute',lambda *a,**k:spending(comparison=False))
    def answer(messages,context):
        f=next(f for f in json.loads(messages[-1]['content'])['facts'] if f['key']=='current.totals.net_minor')
        return calls(('respond',{'text':'按账本餐饮分类，净支出{{'+f['id']+'}}。'}))
    sequence(monkeypatch,calls(('summarize_spending',{'start':'2026-03-24','end':'2026-09-24','category':'餐饮','comparison_mode':'none'})),answer)
    result=create(db,db.get(DemoSession,'s1'),'live','近半年内饮食相关的支出');text=result['messages'][-1]['content']
    assert text.count('5,702.85')==1 and '2026-03-24 至 2026-09-24' in text
    assert '本期流水覆盖完整' in text and 'current.' not in text
