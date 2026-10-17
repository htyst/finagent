"""Regressions from the annual-data audit, using isolated databases only."""
import csv,json,os
from datetime import date,datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace
import pytest
from sqlalchemy import select,func
from test_financial_import import imported,login
from app.agent import dialogue,language,registry
from app.agent.dialogue_schema import DialogueTurn,ReadRequest,Goal
from app.agent.service import create
from app.data.models import DemoSession,AgentTask,AgentStep,Account,Transaction,DemoSetting,DataCoverage
from app.data.financial_models import FinancialEvent
from app.data.subscription_models import SubscriptionAuthorization,SubscriptionCharge
from app.domain.errors import DomainError
from app.domain import security,subscriptions
from app.domain.spending import summarize_spending
from app.domain.statement import generate_statement

def decision(intent='reply',**kwargs):
    fields={k:kwargs.pop(k) for k in list(kwargs) if k in DialogueTurn.model_fields}
    return language.Understanding(intent=intent,dialogue=DialogueTurn(behavior='insert_query',**fields),**kwargs)

def model_sequence(monkeypatch,*items):
    monkeypatch.setenv('FINAGENT_AGENT_ENGINE','legacy')
    iterator=iter(items);seen=[]
    def understand(payload):
        seen.append(payload)
        item=next(iterator)
        return item(payload) if callable(item) else item
    monkeypatch.setattr(language,'interpret',understand)
    return seen

@pytest.mark.parametrize('text,start,end',[
 ('查2026-08-15的餐饮消费','2026-08-15','2026-08-15'),
 ('查2026年8月15日餐饮','2026-08-15','2026-08-15'),
 ('查8月15号餐饮','2026-08-15','2026-08-15'),
 ('2026年7月至8月餐饮','2026-07-01','2026-08-31'),
 ('2025年12月到2026年1月','2025-12-01','2026-01-31'),
 ('八月份餐饮','2026-08-01','2026-08-31'),
 ('2026年第三季度消费','2026-07-01','2026-09-30'),
 ('最近一年全部消费','2025-09-25','2026-09-24'),
 ('2026年7月1日到8月31日','2026-07-01','2026-08-31'),
 ('2026年8月15日至20日','2026-08-15','2026-08-20'),
])
def test_explicit_date_boundaries(bank,text,start,end):
    from app.agent.analytics import resolve_read
    db,_=bank
    _,args,question=resolve_read(db,SimpleNamespace(owner_id='u1'),'spending',text,{},language.Understanding(intent='spending',start='2026-08-15',end='2026-08-15'))
    assert question is None and (args['start'],args['end'])==(start,end)

def test_aggregate_cash_and_credit_against_source_csv_all_users(imported):
    client,factory=imported
    source=Path(os.getenv('FINAGENT_DATASET_SOURCE',str(Path(__file__).resolve().parents[2]/'datasets/demo_financial_v2')))
    cash=list(csv.DictReader((source/'transactions.csv').open(encoding='utf-8-sig')))
    debt=list(csv.DictReader((source/'liability_events.csv').open(encoding='utf-8-sig')))
    from calendar import monthrange
    with factory() as db:
        for i in range(1,21):
            owner=f'u{i}'
            for month in (1,7,8,9):
                first=f'2026-{month:02d}-01';last=f'2026-{month:02d}-{monthrange(2026,month)[1]:02d}'
                begin=datetime.fromisoformat(first+'T00:00:00+08:00').astimezone(timezone.utc).isoformat(timespec='seconds')
                finish=(datetime.fromisoformat(last+'T00:00:00+08:00')+timedelta(days=1)).astimezone(timezone.utc).isoformat(timespec='seconds')
                expected=sum(int(r['amount_minor'])*(1 if r['type']=='purchase' else -1) for r in cash if r['owner_id']==owner and r['status']=='posted' and r['type'] in {'purchase','refund'} and begin<=r['posted_at_utc']<finish)
                expected+=sum(int(r['amount_minor'])*( -1 if r['event_type']=='credit_refund' else 1) for r in debt if r['owner_id']==owner and r['event_type'] in {'credit_purchase','subscription_charge','credit_refund'} and first<=r['occurred_at_local'][:10]<=last)
                result=summarize_spending(db,owner_id=owner,start=first,end=last)
                assert result['current']['totals']['net_minor']==expected,(owner,month)
        result=summarize_spending(db,owner_id='u2',start='2026-08-01',end='2026-08-31')
        assert result['current']['totals']['net_minor']==1126808
        assert result['included_sources']==['cash_ledger','credit_card_ledger']

def test_cards_filters_categories_and_ownership(imported):
    client,_=imported;login(client,'u1')
    cards=client.get('/api/cards').json()['data'];assert len(cards)==3
    assert next(c for c in cards if c['id']=='cc1')['read_only']
    credit=client.get('/api/transactions',params={'card_id':'cc1','start':'2026-08-01','end':'2026-08-31'}).json()['data']
    assert credit['total']==3 and all(r['card_id']=='cc1' for r in credit['items'])
    assert client.get('/api/transactions/'+credit['items'][0]['id']).json()['data']['card_id']=='cc1'
    assert client.get('/api/transactions',params={'card_id':'cc2'}).status_code==404
    unknown=client.get('/api/spending',params={'category':'未知类别'});assert unknown.status_code==422
    assert client.get('/api/transactions?unsupported_filter=income').status_code==422
    unc=client.get('/api/transactions?category=未分类').json()['data'];assert unc['total']==1 and unc['items'][0]['amount_minor']==1100
    subtotal=client.get('/api/spending?category=未分类&start=2025-09-25&end=2026-09-24').json()['data'];assert subtotal['current']['totals']['net_minor']==1100
    filtered=client.get('/api/transactions',params={'merchant':'不存在的商户','type':'income'}).json()['data'];assert filtered['total']==0
    top=client.get('/api/transactions?sort=amount_desc&limit=5').json()['data']['items'];assert [r['amount_minor'] for r in top]==sorted([r['amount_minor'] for r in top],reverse=True)
    login(client,'u16');assert next(c for c in client.get('/api/cards').json()['data'] if c['id']=='c16b')['status']=='expired'

@pytest.mark.parametrize('legacy',[[],[{'id':'old-candidate','evidence':[]}],{'candidates':[{'id':'old-candidate','evidence':[]}]}])
def test_subscription_old_new_result_formats_never_break_page(imported,legacy):
    client,factory=imported;headers=login(client,'u14');sid=client.cookies.get('finagent_demo_session')
    client.post('/api/subscriptions/query-confirmation',headers=headers)
    with factory() as db:
        setting=db.get(DemoSetting,1);now=datetime.now(timezone.utc).isoformat()
        t=AgentTask(id='audit-compat',session_id=sid,owner_id='u14',dataset_version=setting.dataset_version,mode='live',status='completed',context_json='{}',plan_json='[]',requested_outcomes_json='[]',created_at_utc=now,updated_at_utc=now)
        db.add(t);db.flush()
        db.add(AgentStep(id='audit-step',task_id=t.id,ordinal=1,tool_name='identify_subscriptions',args_json='{}',result_json=json.dumps(legacy),status='succeeded',created_at_utc=now));db.commit()
    response=client.get('/api/subscriptions');assert response.status_code==200
    assert len(response.json()['data']['imported']['contracts'])==5

def test_subscription_paid_cost_trial_failure_and_pagination(imported):
    client,_=imported
    for user,paid,cost in [('u14',139200,7700),('u1',67800,5650),('u18',48000,8900)]:
        headers=login(client,user)
        assert client.get('/api/subscription-analysis').status_code==403
        client.post('/api/subscriptions/query-confirmation',headers=headers)
        full=client.get('/api/subscription-analysis?limit=5').json()['data']
        assert full['summary']['paid_total_minor']==paid and full['summary']['auto_renew_monthly_minor']==cost
        next_page=client.get('/api/subscription-analysis?limit=5&offset=5').json()['data']
        assert {b['billing_event_id'] for b in full['billing_events']}.isdisjoint(b['billing_event_id'] for b in next_page['billing_events'])
    assert full['summary']['trial_contracts']==['sub18-3']
    headers=login(client,'u12');client.post('/api/subscriptions/query-confirmation',headers=headers)
    failures=client.get('/api/subscription-analysis?status=failed').json()['data'];assert failures['summary']['failed_attempt_count']>=1 and failures['billing_total']>=1

def test_subscriptions_model_reply_is_trusted_and_page_survives(imported,monkeypatch):
    client,factory=imported;headers=login(client,'u14');sid=client.cookies.get('finagent_demo_session');client.post('/api/subscriptions/query-confirmation',headers=headers)
    seen=model_sequence(monkeypatch,decision('subscription_status',tools=[ReadRequest(name='read_subscriptions')]),decision('subscription_status'))
    with factory() as db:
        result=create(db,db.get(DemoSession,sid),'live','查看我的全部订阅，月费合计多少，哪些重复？')
        reply=result['messages'][-1]['content']
        assert result['status']=='completed' and '77.00' in reply and '同类服务重叠' in reply and '1392.00' in reply
        assert 'read_financial_profile' in {t['name'] for t in seen[0]['dialogue_context']['allowed_read_tools']}
        assert len(seen[0]['dialogue_context']['allowed_read_tools'])==len(dialogue.READ_TOOLS)
    assert client.get('/api/subscriptions').status_code==200

def test_annual_model_context_and_full_history_remain_bounded(imported,monkeypatch):
    client,factory=imported;login(client,'u1');sid=client.cookies.get('finagent_demo_session')
    seen=model_sequence(monkeypatch,decision('statement',tools=[ReadRequest(name='generate_statement',arguments_json=json.dumps({'report_kind':'annual','start':'2025-09-25','end':'2026-09-24'}))]),decision('statement'))
    with factory() as db:
        result=create(db,db.get(DemoSession,sid),'live','生成2025年9月25日至2026年9月24日这一整年的报告')
        assert result['status']=='completed' and len(seen)==2
        assert max(len(json.dumps(p,ensure_ascii=False)) for p in seen)<64000
        report=result['steps'][0]['result'];assert len(report['months'])==13 and not report['missing_months']
        assert report['partial_boundary_months']==['2025-09','2026-09']
        profile=registry.execute(db,session_id=sid,owner_id='u1',name='read_financial_profile',arguments={})
        assert dialogue.envelope('read_financial_profile',{},profile,'x')['data']['history'][-1]['as_of']=='2026-09-24'

def test_card_filter_is_preserved_and_invented_card_rejected(imported):
    client,factory=imported;login(client)
    with factory() as db:
        task=SimpleNamespace(owner_id='u1',context_json='{}')
        raw=registry.validate('list_transactions',{'card_id':'c1a'})
        out=dialogue.normalize_reads(db,task,'2026年8月尾号1234卡的流水',decision('transactions'),[('list_transactions',raw)])
        assert out[0][1]['card_id']=='c1a'
        raw_tail=registry.validate('list_transactions',{'card_id':'1234'})
        out=dialogue.normalize_reads(db,task,'2026年8月尾号1234卡的流水',decision('transactions'),[('list_transactions',raw_tail)])
        assert out[0][1]['card_id']=='c1a'
        with pytest.raises(DomainError):dialogue.normalize_reads(db,task,'查8月全部流水',decision('transactions'),[('list_transactions',raw)])

def test_foreign_history_and_analysis_scope_and_ranking(imported):
    client,_=imported;login(client,'u1')
    report=client.get('/api/financial-history?table=monthly_financial_snapshots&limit=100').json()['data']
    assert report['total']==13 and report['items'][0]['as_of']=='2026-09-24'
    assert client.get('/api/financial-history?table=credit_card_statements&card_id=cc2').status_code==404
    assert client.get('/api/financial-history?table=subscription_billing_events').status_code==403
    result=client.get('/api/financial-analysis',params={'kind':'largest_expenses','start':'2026-08-01','end':'2026-08-31','top_n':5}).json()['data']
    assert result['summary']['eligible_purchase_count']>5 and len(result['items'])==5
    grouped=client.get('/api/financial-analysis?kind=expense_by_merchant&start=2026-08-01&end=2026-08-31&top_n=50').json()['data']
    assert sum(i['net_minor'] for i in grouped['items'])==grouped['summary']['net_minor']
    cash=client.get('/api/financial-analysis?kind=cashflow&start=2026-08-01&end=2026-08-31').json()['data']['summary']
    assert cash['net_cashflow_minor']==cash['inflow_minor']-cash['outflow_minor']

def test_read_error_status_and_unsupported_credit_scope_claim(bank,monkeypatch):
    db,_=bank;session=db.get(DemoSession,'s1')
    model_sequence(monkeypatch,decision('statement',tools=[ReadRequest(name='generate_statement',arguments_json=json.dumps({'report_kind':'monthly','start':'2026-08-02','end':'2026-08-31'}))]),decision('statement'))
    result=create(db,session,'live','生成2026-08-02到2026-08-31月报')
    assert result['status']=='failed' and result['steps'][0]['status']=='failed'
    wrong=decision(reply='信用卡已计入且数据完整。',evidence_refs=['x'])
    evidence=[{'tool':'summarize_spending','source_ref':'x','data':{'included_sources':['cash_ledger']}}]
    assert dialogue.safe_explanation(wrong,evidence) is None

def test_imported_card_binding_blocks_expired_charge_and_preserves_grant(imported):
    _,factory=imported
    with factory() as db:
        auth=db.get(SubscriptionAuthorization,'import-sub16-3');assert auth.payment_card_id=='c16b'
        grant=security.grant(db,'u16','subscription:'+auth.id,security.subscription_terms(auth),'yellow',(security.now()+timedelta(days=366)).isoformat(),12)
        db.commit();before=db.get(Account,auth.account_id).balance_minor
        result=subscriptions._charge_due(db,auth.id,datetime.fromisoformat(auth.next_charge_at_utc))
        assert result['status']=='failed' and result['error_code']=='CARD_EXPIRED'
        assert db.get(Account,auth.account_id).balance_minor==before

def test_new_subscription_charge_uses_same_category(bank):
    from test_p1_business import authorized_subscription
    db,_=bank;auth=authorized_subscription(db)
    security.grant(db,'u1','subscription:'+auth.id,security.subscription_terms(auth),'yellow',(security.now()+timedelta(days=366)).isoformat(),12);db.commit()
    result=subscriptions._charge_due(db,auth.id,datetime.fromisoformat(auth.next_charge_at_utc));assert result['status']=='posted'
    assert db.get(Transaction,result['transaction_id']).category=='订阅续费'
