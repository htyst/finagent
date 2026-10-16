"""Ground time in user evidence. Dialogue policy and defaults are backend-owned."""
import hashlib
import re
from app.agent.guardrails import chinese_number
from app.planning.schema import TimeExpression
from app.verifier import clarification_rules

MONTHLY = re.compile(r'每(?:个)?月')
SINGLE = re.compile(r'只转(?:这)?一(?:次|笔)|只转一次|单次|一次性|(?:不用|不要|不需要|不是|取消|不再).{0,4}(?:每(?:个)?月|定期|周期)')
DAY = re.compile(r'(?<![0-9])([0-9]{1,2}|[一二两三四五六七八九十]{1,3})\s*[号日]')
TIME_QUESTION = re.compile(r'每月|每个月|每周|周期|定期|单次|自动转账|扣款日|日期|几号|哪天|转账时间')
YES = re.compile(r'(?:可以|同意|好的|好|是的|对|就1号|设置为1号|用1号)[。！!，,\s]*')
NO = re.compile(r'不行|不同意|不要1号|不选1号|换一个|不能|^不[。！!\s]*$')

def resolve(messages, context):
    recurrence=None
    anchor=None
    evidence=None
    unsupported=None
    for i,m in enumerate(messages):
        if m['role']!='user': continue
        text=m['content']
        if SINGLE.search(text):
            recurrence=None; anchor=None; evidence=text; unsupported=None
            continue
        if re.search(r'每周|每星期|每天|每年|每季度',text):
            recurrence=None; anchor=None; unsupported=text
            continue
        if MONTHLY.search(text):
            recurrence={'type':'recurring','freq':'monthly','day':None}
            anchor=hashlib.sha256((str(m.get('ordinal',i))+':'+text).encode()).hexdigest()
            evidence=text; unsupported=None
        if recurrence:
            found=next((m for m in DAY.finditer(text) if not re.search(r'(?:不要|不选|不是|不同意|不能|不行)[\s，,]*$', text[:m.start()])),None)
            if found:
                day=int(chinese_number(found[1]))
                if 1<=day<=31:
                    recurrence['day']=day
                    evidence=text
    accepted=context.get('temporal_accepted',{})
    if recurrence and recurrence['day'] is None and accepted.get('anchor')==anchor:
        recurrence['day']=accepted['day']
    current=messages[-1]['content'] if messages and messages[-1]['role']=='user' else ''
    assistants=[m['content'] for m in messages[:-1] if m['role']=='assistant']
    pending=context.get('temporal_pending',{})
    matching=bool(recurrence and pending.get('anchor')==anchor and assistants and assistants[-1]==pending.get('message'))
    if matching and YES.fullmatch(current):
        recurrence['day']=pending['suggested_day']
        accepted={'anchor':anchor,'day':pending['suggested_day'],'rule_id':'R10','user_reply':current}
        context['temporal_accepted']=accepted
        context.pop('temporal_pending',None)
    elif matching and NO.search(current):
        context['temporal_rejected_anchor']=anchor
        context.pop('temporal_pending',None)
    if not recurrence:
        for key in ('temporal_pending','temporal_accepted','temporal_rejected_anchor',
                    'standing_day_question','standing_day','standing_text','standing_slots'):
            context.pop(key,None)
    state={'recurrence':recurrence,'anchor':anchor,'evidence':evidence,'unsupported':unsupported,
           'default_rejected':bool(anchor and context.get('temporal_rejected_anchor')==anchor),
           'accepted_default':bool(anchor and accepted.get('anchor')==anchor)}
    context['temporal_intent']=state
    return state

def normalize(plan,state):
    """Model cannot introduce a periodic intent or supply an unaccepted default."""
    corrections=[]
    expected=state['recurrence']
    previous=plan.recurrence.model_dump(exclude_none=True) if plan.recurrence else None
    for step in plan.steps:
        if step.recurrence:
            corrections.append({'field':f'steps.{step.id}.recurrence',
                                'observed':step.recurrence.model_dump(),'resolved':expected})
        step.recurrence=None
    applies=bool(expected and (any(s.tool=='prepare_transfer' for s in plan.steps)
                              or not plan.steps or plan.clarification))
    resolved=expected if applies else None
    if previous!=resolved:
        corrections.append({'field':'recurrence','observed':previous,'resolved':resolved,
                            'reason':'周期与日期必须有当前用户依据或后端默认值确认记录'})
    plan.recurrence=TimeExpression.model_validate(resolved) if resolved else None
    return corrections

def check(plan,state):
    return clarification_rules('plan',{'recurrence':plan.recurrence.model_dump() if plan.recurrence else None},
        {'recurrence_grounded':bool(state['recurrence']), 'default_rejected':state['default_rejected']})

def is_time_question(text):
    return bool(text and TIME_QUESTION.search(text))
