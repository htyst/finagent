"""Exercise imported data through the real scoped APIs and business guards."""
import json,os,sqlite3
from decimal import Decimal
from types import SimpleNamespace
from pathlib import Path
from datetime import datetime,timedelta,timezone
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select,func
from sqlalchemy.orm import sessionmaker
from app.main import app
from app.data.db import get_db,make_engine
from app.data.models import Account,Transaction,DemoSetting
from app.data.financial_models import FinancialDatasetRow
from app.data.subscription_models import SubscriptionAuthorization
from app.domain.errors import DomainError

@pytest.fixture
def imported(tmp_path):
    source=Path(os.getenv('FINAGENT_IMPORT_TEST_DB',str(Path(__file__).resolve().parents[2]/'var/finagent.sqlite3')))
    if not source.exists():pytest.skip('Provide FINAGENT_IMPORT_TEST_DB with imported financial staging DB')
    path=tmp_path/'financial.sqlite3'
    with sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True) as src:
        if src.execute('select seed_version from demo_settings').fetchone()[0]!='demo-financial-v2':pytest.skip('Annual data not installed')
        with sqlite3.connect(path) as dest:src.backup(dest)
    engine=make_engine(path);factory=sessionmaker(bind=engine,expire_on_commit=False)
    def override():
        with factory() as db:yield db
    app.dependency_overrides[get_db]=override
    with TestClient(app) as client:yield client,factory
    app.dependency_overrides.pop(get_db,None);engine.dispose()

def login(client,user='u1'):
    assert client.post('/api/demo/session',json={'user_id':user}).status_code==200
    csrf=client.get('/api/demo/csrf').json()['data']['csrf_token']
    return {'Origin':'http://testserver','X-FinAgent-CSRF':csrf}

def test_financial_accounts_cards_and_lossless_archive(imported):
    client,factory=imported
    assert len(client.get('/api/demo/users').json()['data'])==20
    login(client)
    result=client.get('/api/financial-profile').json()['data']
    assert result['available'] and len(result['cards'])==3 and len(result['deposits'])==1
    assert sum(c['card_type']=='credit' for c in result['cards'])==1
    assert '订阅清单' not in result['overview'] and '全年订阅实扣_元' not in result['overview']
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Account))==56
        assert db.scalar(select(func.count()).select_from(Transaction))==25471
        source=db.scalar(select(FinancialDatasetRow.payload_json).where(FinancialDatasetRow.table_name=='cards',FinancialDatasetRow.row_key=='cc1'))
        assert json.loads(source)['liability_id']=='credit1'
        from app.domain.ledger import ledger_errors
        assert ledger_errors(db)==[]
        from app.domain.financial_profile import portfolio
        for number in range(1,21):
            profile=portfolio(db,f'u{number}')
            assert profile['balances']['net_worth_minor']==int(Decimal(profile['overview']['净资产_元'])*100)
        from app.agent.dialogue import evidence_reply
        reply=evidence_reply(SimpleNamespace(reply=None,dialogue=None),[{'tool':'read_financial_profile','raw_data':result}])
        assert '净资产' in reply and '53226.53' in reply and '导入档案' in reply
        from app.agent.registry import execute
        sid=client.cookies.get('finagent_demo_session')
        assert execute(db,session_id=sid,owner_id='u1',name='read_financial_profile',arguments={})['balances']==result['balances']
        with pytest.raises(DomainError):
            execute(db,session_id=sid,owner_id='u2',name='read_financial_profile',arguments={})

def test_financial_api_auth_and_user_isolation(imported):
    client,_=imported
    assert client.get('/api/financial-profile').status_code==401
    login(client,'u1');first=client.get('/api/financial-profile').json()['data']
    login(client,'u20');second=client.get('/api/financial-profile').json()['data']
    assert all(c['owner_id']=='u1' for c in first['cards'])
    assert all(c['owner_id']=='u20' for c in second['cards'])
    assert not set(c['id'] for c in first['cards'])&set(c['id'] for c in second['cards'])

def test_subscription_archive_keeps_yellow_query_gate(imported):
    client,_=imported;headers=login(client,'u14')
    assert client.get('/api/financial-profile/subscriptions').status_code==403
    assert client.get('/api/subscriptions').status_code==403
    assert client.post('/api/subscriptions/query-confirmation',headers=headers).status_code==200
    result=client.get('/api/subscriptions').json()['data']['imported']
    assert len(result['contracts'])==5
    assert len({s['subscription_id'] for s in result['contracts'] if s['duplicate_service_group']})==2
    assert all(s['owner_id']=='u14' for s in result['contracts'])

def test_import_does_not_authorize_background_catchup(imported):
    _,factory=imported
    from app.domain.subscriptions import run_due_subscriptions
    with factory() as db:
        before=list(db.execute(select(Account.id,Account.balance_minor).order_by(Account.id)))
        count=db.scalar(select(func.count()).select_from(Transaction))
    assert run_due_subscriptions(factory,now=datetime.now(timezone.utc)+timedelta(days=800),limit=100)==[]
    with factory() as db:
        assert before==list(db.execute(select(Account.id,Account.balance_minor).order_by(Account.id)))
        assert count==db.scalar(select(func.count()).select_from(Transaction))

def test_expired_card_and_locked_term_cannot_be_used(imported):
    client,factory=imported;login(client,'u16');sid=client.cookies.get('finagent_demo_session')
    from app.domain.operations import prepare_card_action,prepare_transfer
    with factory() as db:
        with pytest.raises(DomainError,match='到期'):prepare_card_action(db,session_id=sid,card_id='c16b',action='unfreeze')
    login(client,'u19');sid=client.cookies.get('finagent_demo_session')
    with factory() as db:
        with pytest.raises(DomainError):prepare_transfer(db,session_id=sid,source_account_id='a19t',target={'type':'own_account','account_id':'a19c'},amount_minor=100)

def test_imported_monthly_contract_revoke_uses_fixed_confirmation(imported):
    client,factory=imported;headers=login(client,'u14');sid=client.cookies.get('finagent_demo_session')
    client.post('/api/subscriptions/query-confirmation',headers=headers)
    from app.domain.subscriptions import prepare,issue_token,confirm
    with factory() as db:
        request=prepare(db,session_id=sid,kind='revoke',authorization_id='import-sub14-2')
        token=issue_token(db,session_id=sid,request_id=request['id'])['confirmation_token']
        confirm(db,session_id=sid,request_id=request['id'],confirmation_token=token)
        assert db.get(SubscriptionAuthorization,'import-sub14-2').status=='revoked'
        assert db.get(SubscriptionAuthorization,'import-sub14-3').status=='active'
    contracts=client.get('/api/subscriptions').json()['data']['imported']['contracts']
    assert next(s for s in contracts if s['subscription_id']=='sub14-2')['current_bank_mandate_status']=='revoked'
