import json
import pytest
from app.data.models import DemoSetting,DemoSession
from app.data.financial_models import FinancialDatasetRow,FinancialDatasetImport
from app.domain.credit_cards import read
from app.domain.errors import DomainError
from app.agent import registry,tool_catalog,dynamic_protocol as protocol,dynamic_runtime as runtime
from app.agent.service import create
from test_dynamic_tools import dynamic,sequence,calls,reply


def add(db,table,key,payload,owner='u1',version=None):
    version=version or db.get(DemoSetting,1).dataset_version
    db.add(FinancialDatasetRow(dataset_version=version,table_name=table,row_key=key,owner_id=owner,payload_json=json.dumps(payload)))
    db.commit()


def seed(db,identifier='credit-test',tail='9876',owner='u1',**extra):
    raw=dict(id=identifier,owner_id=owner,card_type='credit',alias='案例信用卡',bank_name='模拟银行',card_last4=tail,status='active',
        credit_limit_minor='1500000',outstanding_minor='112388',available_credit_minor='1387612',
        statement_balance_minor='68115',unbilled_balance_minor='44273',minimum_payment_due_minor='6812',statement_day='10',due_day='28',**extra)
    add(db,'cards',identifier,raw,owner)
    return raw


def test_credit_card_snapshot_and_latest_statement_do_not_mutate_data(bank):
    db,_=bank;seed(db)
    version=db.get(DemoSetting,1).dataset_version
    db.add(FinancialDatasetImport(dataset_version=version,manifest_json=json.dumps({'period_end':'2026-09-24'}),imported_at_utc='2026-09-24T00:00:00+00:00'));db.commit()
    for date,balance in [('2026-08-10','10000'),('2026-09-10','68115'),('2026-10-10','99999')]:
        add(db,'credit_card_statements',date,dict(statement_id=date,card_id='credit-test',statement_date=date,due_date=date[:7]+'-28',statement_balance_minor=balance,payment_minor='0',status='unpaid'))
    result=registry.execute(db,session_id='s1',owner_id='u1',name='read_credit_cards',arguments={'card':'尾号9876'})
    c=result['credit_cards'][0]
    assert result['snapshot_as_of']=='2026-09-24' and result['recorded_card_count']==1
    assert c['credit_limit_minor']==1500000 and c['outstanding_minor']==112388 and c['available_credit_minor']==1387612
    assert c['latest_statement']['statement_date']=='2026-09-10' and c['latest_statement']['due_date']=='2026-09-28'
    assert c['latest_statement']['statement_balance_minor']==68115 and c['latest_statement']['payment_minor']==0
    assert c['card_mask']=='**9876' and c['read_only'] and not db.new and not db.dirty and not db.deleted


def test_credit_card_owner_and_dataset_isolation(bank):
    db,_=bank;seed(db);seed(db,'other-credit','5432','u2')
    add(db,'cards','old-credit',dict(id='old-credit',card_type='credit',card_last4='1233'),version='obsolete')
    result=read(db,session_id='s1')
    assert [c['card_id'] for c in result['credit_cards']]==['credit-test']
    with pytest.raises(DomainError):read(db,session_id='s1',card='5432')
    with pytest.raises(DomainError):registry.execute(db,session_id='s1',owner_id='u2',name='read_credit_cards',arguments={})


def test_missing_credit_values_stay_unknown_and_debit_is_rejected(bank):
    db,_=bank
    add(db,'cards','missing',dict(id='missing',card_type='credit',card_last4='9876',alias='缺失字段信用卡'))
    result=read(db,session_id='s1')['credit_cards'][0]
    assert result['credit_limit_minor'] is None and result['available_credit_minor'] is None
    assert result['minimum_payment_due_minor'] is None and result['latest_statement'] is None and result['due_day'] is None
    with pytest.raises(DomainError) as debit:read(db,session_id='s1',card='c1a')
    assert debit.value.code=='INVALID_CARD_TYPE'


def test_duplicate_card_reference_requires_clarification(bank):
    db,_=bank;seed(db);seed(db,'second-credit','9876')
    with pytest.raises(DomainError):read(db,session_id='s1',card='9876')
    assert len(read(db,session_id='s1')['credit_cards'])==2


def test_no_archive_does_not_invent_credit_or_dates(bank):
    db,_=bank;result=read(db,session_id='s1')
    assert result['credit_cards']==[] and result['archive_available'] is False


@pytest.mark.parametrize('value',['12.34','bad',True])
def test_bad_amount_is_rejected_instead_of_silently_coerced(bank,value):
    db,_=bank;raw=seed(db)
    row=db.get(FinancialDatasetRow,(db.get(DemoSetting,1).dataset_version,'cards',raw['id']))
    raw['available_credit_minor']=value;row.payload_json=json.dumps(raw);db.commit()
    with pytest.raises(DomainError) as error:read(db,session_id='s1')
    assert error.value.code=='DATA_INCONSISTENT'


def test_credit_tool_strict_schema_and_model_observation(dynamic,monkeypatch):
    db,_=dynamic;seed(db)
    add(db,'credit_card_statements','latest',dict(statement_id='latest',card_id='credit-test',statement_date='2026-09-10',due_date='2026-09-28',status='unpaid'))
    with pytest.raises(ValueError):registry.TOOL_MODELS['read_credit_cards'].model_validate({'owner_id':'u2'})
    with pytest.raises(ValueError):registry.TOOL_MODELS['read_credit_cards'].model_validate({'card':9876})
    def after_read(messages,context):
        observation=json.loads(messages[-1]['content'])
        assert observation['status']=='completed' and observation['facts']
        total=next(f for f in observation['facts'] if f['key'].endswith('.credit_limit_minor'))
        assert total['unit']=='yuan' and float(total['value'])==15000
        return reply('尾号9876，档案总额度{{'+total['id']+'}}；最近账单还款日2026-09-28，账单日每月10日。额度属于导入快照，不能当作实时可刷金额。')
    sequence(monkeypatch,calls(('read_credit_cards',{'card':'9876'})),after_read)
    result=create(db,db.get(DemoSession,'s1'),'live','查询尾号9876的信用卡额度')
    assert result['status']=='completed' and not result['authorization_operations']
    assert 'read_credit_cards' in tool_catalog.catalog()['groups']['卡片管理']


@pytest.mark.parametrize('text',['还款日2026-10-28','尾号1233','总额度15000元'])
def test_credit_metadata_does_not_authorize_other_dates_identifiers_or_money(text):
    data={'credit_cards':[{'card_mask':'**9876','statement_day':'10','due_day':'28',
        'latest_statement':{'statement_date':'2026-09-10','due_date':'2026-09-28'}}]}
    r={'facts':[],'evidence':[{'tool':'read_credit_cards','source_ref':'test','data':data,'query':{}}]}
    response=protocol.models()['respond'](text=text,evidence_refs=['test'])
    with pytest.raises(DomainError):runtime.render_response(response,r)
