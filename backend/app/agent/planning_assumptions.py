"""Deterministic grounding barriers, not business intent routing."""
import re
from app.domain.errors import DomainError

CORRECTION=re.compile(r'改(?:成|为|到)|改(?=\s*[0-9零〇一二两三四五六七八九十百千万亿])|改一下|更正|调整为|不是.{0,30}(?:是|而是)|取消.{0,30}(?:先|改)|不再.{0,20}(?:先|改)|先不.{0,20}(?:先|改)')
FOREIGN=re.compile(r'美元|美金|日元|欧元|英镑|港币|港元|USD|JPY|EUR|GBP|HKD',re.I)


def dimension(text):
    if re.search(r'取消|放弃|不再|先不|换个目标|目标改',text):return 'all'
    if re.search(r'期限|时长|时间',text):return 'time'
    if re.search(r'预算|成本|费用|钱|万|千|元|块',text):return 'money'
    if re.search(r'个月|天|周|年',text):return 'time'
    return 'all'


def barriers(messages):
    result={'money':0,'time':0,'other':0}
    for i,text in enumerate(messages):
        if CORRECTION.search(text):
            kind=dimension(text)
            for key in result:
                if kind=='all' or key==kind:result[key]=i
    return result


def scope(messages):
    indexes=barriers(messages);index=max(indexes.values())
    return dict(revision=index,invalidated_before_turn=indexes,active_messages=messages[index:],
        rule='已更正维度的旧假设失效；未更改且有原文的预算或期限可保留。每轮证据与计算重建，已完成回执不重放。')


def validate_quote(operand,messages):
    if not operand.quote:return
    kind='money' if operand.unit=='yuan' else 'time' if re.search(r'月|天|年|期限|时间',(operand.label or '')+operand.quote) else 'other'
    active=messages[barriers(messages)[kind]:]
    correction=list(CORRECTION.finditer(operand.quote))
    if correction and any(operand.quote in text for text in active):
        # Keep the exact user provenance, but validate the value only against
        # the replacement fragment, never the negated prefix.
        fragment=operand.quote[correction[-1].end():].strip(' ，,；;。')
        if not fragment:raise DomainError('ANALYSIS_STALE_ASSUMPTION','更正后的新参数缺失，需要明确新值。',422)
        if operand.unit=='yuan' and FOREIGN.search(fragment):
            raise DomainError('ANALYSIS_CURRENCY_UNKNOWN','外币新值仍需人民币预算或核验换算。',422)
        return fragment
    def valid(text):
        if operand.quote not in text:return False
        blocked=[]
        if dimension(text) in {'all',kind}:
            # Old values before a correction marker are not its replacement.
            for match in CORRECTION.finditer(text):blocked.append((0,match.end()))
        for found in re.finditer(re.escape(operand.quote),text):
            if not any(a<=found.start()<b for a,b in blocked):return True
        return False
    if not any(valid(text) for text in active):
        raise DomainError('ANALYSIS_STALE_ASSUMPTION','参数引用已被用户的预算、期限或目标更正替代；请使用仍有效的原话，不重放旧情景。',422)
    if operand.unit=='yuan' and FOREIGN.search(operand.quote):
        raise DomainError('ANALYSIS_CURRENCY_UNKNOWN','外币成本不能当成人民币。当前没有已核验汇率，请先取得人民币总预算或明确换算依据。',422)
    return operand.quote


def advice_only(text):
    return bool(re.search(r'(?:只|仅).{0,10}(?:建议|分析|咨询|测算|比较)|(?:不要|不必|先不|暂不|不需要|不)(?:自动|实际|立即)?(?:办理|操作|转账|申购|锁定|预留)|(?:能否|是否|能不能|可不可以).{0,25}(?:负担|旅游|旅行|买|预算)',text))
