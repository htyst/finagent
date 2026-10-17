"""Bound rejected attempts without hiding unresolved business or security failures."""
import hashlib,json
from pydantic import ValidationError


REPAIRABLE={'INVALID_ARGUMENTS','INVALID_TOOL_BATCH','BATCH_NOT_EXECUTED','ANALYSIS_ANSWER_INVALID',
    'UNSUPPORTED_ANSWER','ANALYSIS_REFERENCE','INVALID_CALCULATION','ANALYSIS_UNIT',
    'ANALYSIS_ASSUMPTION','ANALYSIS_FOCUS','ANALYSIS_JUDGMENT','OPERATION_NOT_PREPARED','TOOL_RETRY_EXHAUSTED'}
# These may be recovered only by the very same validated request succeeding.
RETRYABLE={'READ_REJECTED','MODEL_UNAVAILABLE','RESOURCE_UNRESOLVED'}


def signature(node):
    value={'name':node['name'],'arguments':node['arguments']}
    if node.get('invalid_arguments_digest'):value['invalid']=node['invalid_arguments_digest']
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def invalid_digest(raw):
    try:raw=json.dumps(json.loads(raw),ensure_ascii=False,sort_keys=True)
    except (ValueError,TypeError):pass
    return hashlib.sha256(raw.encode()).hexdigest()


def validation_feedback(problem,model=None):
    if isinstance(problem,ValidationError):
        fields=[{'path':'.'.join(map(str,e['loc'])) or '$','issue':e['type']}
            for e in problem.errors(include_input=False,include_url=False)[:8]]
        return {'code':'INVALID_ARGUMENTS','message':'工具参数未通过严格校验；本批没有执行。按字段路径修正，不能删除用户要求来绕过校验。',
            'fields':fields,'accepted_fields':list(model.model_fields) if model else []}
    return {'code':getattr(problem,'code','INVALID_ARGUMENTS'),
        'message':getattr(problem,'message','参数必须是符合当前工具接口的JSON对象；本批没有执行。')}


def prior_rejections(r,node):
    key=signature(node)
    return [n for n in r['nodes'] if n is not node and n.get('status')=='failed'
        and n.get('error_code')!='BATCH_NOT_EXECUTED' and signature(n)==key]


def recovered(r,node):
    """Keep every attempt in the audit, and link exact successful retries."""
    key=signature(node)
    for old in r['nodes']:
        if old is not node and old.get('status')=='failed' and signature(old)==key:
            if old.get('error_code') in REPAIRABLE|RETRYABLE:
                old['recovered_by']=node['id']


def unresolved(node):
    return node.get('status')=='failed' and not node.get('recovered_by') and node.get('error_code') not in REPAIRABLE


def presentation(r):
    """Do not let a different period/tool target conceal missing evidence."""
    hidden={n['id'] for n in r['nodes'] if n.get('status')=='failed' and
        (n.get('recovered_by') or n.get('error_code') in REPAIRABLE)}
    return dict(r,evidence=[e for e in r['evidence'] if e['tool']!='respond' and
        e['source_ref'].split(':')[-1] not in hidden],nodes=[n for n in r['nodes'] if n['name']!='respond'])
