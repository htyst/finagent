import json
from unittest.mock import patch
import httpx
import pytest
from app.planning.provider import request_plan,generate,strict_schema,completion_url
from app.planning.schema import Plan,PlanInvalid,validate_asset
from app.domain.errors import DomainError
from test_neuro import neuro
from test_m4 import demo,login,start,say

@pytest.mark.parametrize('status,code',[(400,'MODEL_BAD_REQUEST'),(401,'MODEL_AUTH_ERROR'),
    (402,'MODEL_QUOTA_ERROR'),(403,'MODEL_AUTH_ERROR'),(429,'MODEL_RATE_LIMIT'),(503,'MODEL_UNAVAILABLE')])
def test_http_errors_are_not_repairable_plan_errors(monkeypatch,status,code):
    monkeypatch.setenv('MODEL_API_KEY','sk-private-secret')
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(status,json={'error':{'message':'schema not accepted','param':'tools','code':'bad_request'}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DomainError) as exc:
            generate({'input':'转账'},generator=lambda p,f:request_plan(p,f,client))
    assert exc.value.code==code
    assert exc.value.provider_error['http_status']==status
    assert len(calls)==1

def test_wire_schema_subset_keeps_local_rules_and_property_names():
    raw=Plan.model_json_schema()
    wire=strict_schema(raw)
    def visit(node):
        if isinstance(node,list):
            for n in node: visit(n)
        if isinstance(node,dict):
            assert not set(node)&{'minLength','maxLength','minItems','maxItems','$ref','$defs','default','const'}
            if node.get('type')=='object':
                assert node['additionalProperties'] is False
                assert set(node['required'])==set(node['properties'])
            for n in node.values(): visit(n)
    visit(wire)
    assert raw['properties']['steps']['maxItems']==12
    with pytest.raises(PlanInvalid): validate_asset({'steps':[]})
    custom=strict_schema({'type':'object','properties':{'minLength':{'type':'string','minLength':2}}})
    assert 'minLength' in custom['properties'] and 'minLength' not in custom['properties']['minLength']

def test_forced_tool_request_and_success(monkeypatch):
    monkeypatch.setenv('MODEL_API_KEY','sk-test')
    raw={'steps':[{'id':'read','tool':'list_accounts'}]}
    def handler(request):
        body=json.loads(request.content)
        assert body['tool_choice']['function']['name']=='plan_request'
        assert body['tools'][0]['function']['strict'] is True
        assert body['thinking']['type']=='disabled'
        return httpx.Response(200,json={'choices':[{'message':{'tool_calls':[{'function':{'name':'plan_request','arguments':json.dumps(raw)}}]}}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert request_plan({'input':'查余额'},client=client)==raw

def test_http_error_diagnostics_are_redacted(monkeypatch):
    monkeypatch.setenv('MODEL_API_KEY','sk-private-secret')
    text='转账给敏感姓名100元'
    response=httpx.Response(400,json={'error':{'message':'sk-private-secret Bearer other-secret '+text,'param':'tools[0].function.parameters'}},headers={'x-request-id':'test-request'})
    with httpx.Client(transport=httpx.MockTransport(lambda r:response)) as client:
        with pytest.raises(DomainError) as exc: request_plan({'input':text},client=client)
    detail=json.dumps(exc.value.provider_error,ensure_ascii=False)
    assert 'sk-private-secret' not in detail and 'other-secret' not in detail and text not in detail
    assert exc.value.provider_error['request_id']=='test-request'

def test_malformed_success_response_still_enters_plan_repair(monkeypatch):
    monkeypatch.setenv('MODEL_API_KEY','sk-test')
    with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'choices':[{'message':{}}]}))) as client:
        with pytest.raises(PlanInvalid): request_plan({},client=client)

@pytest.mark.parametrize('base',['https://api.deepseek.com','https://api.deepseek.com/v1','https://api.deepseek.com/beta'])
def test_official_strict_endpoint(monkeypatch,base):
    monkeypatch.setenv('MODEL_BASE_URL',base)
    assert completion_url()=='https://api.deepseek.com/beta/chat/completions'

def test_trace_records_http_error_without_repair_or_blame(neuro):
    client,_=neuro
    headers=login(client)
    with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(400,json={'error':{'message':'Invalid schema: minItems not supported','param':'tools'}}))) as http:
        original=request_plan
        with patch.dict('os.environ',{'FINAGENT_MODE':'live','MODEL_API_KEY':'sk-test'}),patch('app.planning.provider.request_plan',side_effect=lambda p,f:original(p,f,http)) as mocked:
            task=start(client,headers,'转账给林晓100元')
    trace=client.get('/api/debug/trace/'+task['trace_id']).json()['data']
    assert mocked.call_count==1
    assert trace['model_error']['http_status']==400
    assert trace['repair_rounds']==0 and trace['repair_history']==[]
    assert trace['verification']['violations']==[] and task['proposal'] is None
    assert 'HTTP 400' in task['messages'][-1]['content'] and '两轮' not in task['messages'][-1]['content']


def test_empty_clarification_asset_is_valid_but_empty_execution_is_not():
    plan=validate_asset({'steps':[],'clarification':'请问从哪个账户转出100元？'})
    assert plan.steps==[] and plan.clarification
    with pytest.raises(PlanInvalid): validate_asset({'steps':[]})
    with pytest.raises(PlanInvalid): validate_asset({'steps':[],'clarification':'   '})

def test_missing_information_is_model_question_not_repair_failure(neuro):
    client,engine=neuro
    headers=login(client)
    raw={'steps':[],'clarification':'林晓不在当前预置收款人中，请选择收款人并说明付款账户。'}
    with patch.dict('os.environ',{'FINAGENT_MODE':'live'}),patch('app.planning.provider.request_plan',return_value=raw) as mocked:
        task=start(client,headers,'转账给林晓100元')
    assert mocked.call_count==1
    assert task['status']=='gathering_info' and task['proposal'] is None
    assert task['messages'][-1]['content']==raw['clarification']
    trace=client.get('/api/debug/trace/'+task['trace_id']).json()['data']
    assert trace['repair_rounds']==0 and trace['model_error'] is None
    assert trace['plan']['source']=='model'
