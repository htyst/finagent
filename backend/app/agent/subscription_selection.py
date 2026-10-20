"""Ground a narrow relative selection in current, owned subscription contracts."""
from decimal import Decimal, InvalidOperation
import re
from app.domain.errors import DomainError


CANCEL_VERB=r'(?:取消|关闭|停止|退订|停掉|停用|不再续订|不再续费)'
SUBSCRIPTION_NOUN=r'(?:订阅|续费|续订|会员|合同)'
_HEDGE=r'(?:怎么|如何|能否|是否可以|是否|假设|如果|举例|比如|假如|为什么)'
_REFUSAL=r'(?:不要|别|不想|不用|不需要|暂不|先不)\s*' + CANCEL_VERB
_ALREADY=r'(?:已经|已)\s*(?:成功)?\s*' + CANCEL_VERB


def cancels_subscription(text):
    """Did the user ask to cancel a subscription — in any word order?

    Chinese routinely puts the object first ("把最贵的那一项取消"), so requiring the
    verb to precede the noun silently rejected ordinary phrasings and left the whole
    relative-cancellation capability unreachable.
    """
    if not text:return False
    for sentence in re.split(r'[。；;!！?\n]',text):
        if not sentence.strip():continue
        if not re.search(CANCEL_VERB,sentence) or not re.search(SUBSCRIPTION_NOUN,sentence):continue
        if re.search(_HEDGE,sentence) or re.search(_REFUSAL,sentence) or re.search(_ALREADY,sentence):continue
        return True
    return False


def requested_cancellation(text):
    from app.agent.planning_assumptions import advice_only
    if advice_only(text):return False
    return cancels_subscription(text)


def validate_completion(r,node,response):
    """A successful lookup is not completion of an explicitly requested action."""
    if not requested_cancellation(r['request']):return
    ids=set((node or {}).get('goal_ids',[]))
    goals=[g for g in r.get('goals',[]) if (not ids or g['id'] in ids) and not g.get('user_cancelled')]
    operation_goals=[g for g in goals if requested_cancellation(g['quote'])]
    if not operation_goals:return
    for g in operation_goals:g['kind']='operation'
    if any(n.get('resource_ref') and not n.get('invalidated') and n['name']=='prepare_merchant_contract_cancel'
        and set(n.get('goal_ids',[]))&{g['id'] for g in operation_goals} for n in r.get('nodes',[])):return
    for e in r.get('evidence',[]):
        if e.get('error') or e['tool'] not in {'read_subscriptions','identify_subscriptions'} or e['source_ref'] in r.get('stale_evidence',[]):continue
        data=e.get('raw_data') or e.get('data') or {};data=data.get('confirmed',data)
        if data.get('has_more_contracts'):continue
        try:chosen=most_expensive(data.get('contracts',[]),r['request'])
        except DomainError:
            if response.needs_input:return # A tie/period legitimately needs input.
            raise DomainError('OPERATION_NOT_PREPARED','订阅选择尚不明确，请说明缺项并设置needs_input；不能把查询当作取消完成。',422) from None
        if chosen:
            raise DomainError('OPERATION_NOT_PREPARED','查询不能代替取消办理。仍在续费的唯一最高价合同是'+chosen['service_name']+'；请准备取消固定单，merchant可省略。已关闭续费的合同不作为候选。最终仍由用户手动确认。',422)
    if not response.needs_input:
        raise DomainError('OPERATION_NOT_PREPARED','用户要求取消订阅，尚未生成固定单。请准备操作单，或在确有缺项时明确needs_input；不能把查询当作办理完成。',422)


def most_expensive(contracts, text):
    if not re.search(r'最贵|价格最高|费用最高', text):
        return None
    active = [c for c in contracts if str(c.get('auto_renew_enabled')).lower() in {'true','1'}
        and c.get('status') not in {'cancelled','canceled','expired','terminated','paused','non_renewing'}]
    if not active:
        raise DomainError('RENEWAL_ALREADY_DISABLED', '没有仍开启自动续订的合同，无需再取消。', 409)
    categories = {c.get('service_category') for c in active if c.get('service_category') and c['service_category'] in text}
    if len(categories) == 1:
        active = [c for c in active if c.get('service_category') in categories]
    if any(c.get('currency','CNY') != 'CNY' for c in active):
        raise DomainError('RESOURCE_UNRESOLVED', '订阅币种不同，请明确要取消的合同，不能直接比较价格。', 422)
    monthly = bool(re.search(r'折月|月均|每月费用', text))
    per_charge = bool(re.search(r'每期|单次扣费|单次价格', text))
    cycles = {str(c.get('cycle_months') or {'monthly':1,'quarterly':3,'annual':12}.get(c.get('billing_cycle'))) for c in active}
    if len(cycles)>1 and not monthly and not per_charge:
        raise DomainError('RESOURCE_UNRESOLVED', '候选合同付款周期不同：请说明按每期价格还是折月费用比较，或点名合同。', 422)
    try:
        def price(c):
            amount = Decimal(str(c['current_price_minor']))
            months = Decimal(str(c.get('cycle_months') or {'monthly':1,'quarterly':3,'annual':12}.get(c.get('billing_cycle'))))
            if not amount.is_finite() or amount<0 or not months.is_finite() or months<=0:
                raise ValueError()
            return amount/months if monthly else amount
        prices = [(c,price(c)) for c in active]
    except (KeyError, InvalidOperation, TypeError, ValueError):
        raise DomainError('DATA_UNAVAILABLE', '订阅价格或周期缺失，无法确定最贵的一项，请点名合同。', 422) from None
    highest = max(p for _,p in prices)
    chosen = [c for c,p in prices if p==highest]
    if len(chosen)!=1:
        raise DomainError('RESOURCE_UNRESOLVED', '仍在续费的合同中有多项并列最高价，请点名要取消的一项。', 422)
    return chosen[0]


def continuation_quote(r, node, quote):
    """Only a short follow-up to one unfinished cancellation goal may inherit it."""
    request = r['request']
    if not re.fullmatch(r'\s*(?:确认|同意)(?:取消|退订|停止续订)(?:最贵的?(?:那)?一项|该订阅|这项订阅)?[。！!\s]*', request):
        return quote
    ids = set(node.get('goal_ids',[]))
    goals = [g for g in r.get('goals',[]) if g['id'] in ids and not g.get('user_cancelled')
        and not g.get('receipt_refs') and g.get('status') not in {'completed','cancelled'}]
    originals = [g['quote'] for g in goals if re.search(r'(?:取消|关闭|停止|退订).{0,60}(?:订阅|续费|续订|会员|合同)', g['quote'])]
    if len(originals)!=1:
        return quote
    if any(n.get('receipt_refs') and ids.intersection(n.get('goal_ids',[])) for n in r.get('nodes',[])):
        return quote
    original = originals[0]
    if quote not in request and quote not in original:
        return quote
    return original + '；' + request
