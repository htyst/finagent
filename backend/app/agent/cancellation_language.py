"""Separate cancelling a workflow goal from preparing a merchant cancellation."""
import re


def claims(text):
    # A quoted instruction or a negative/conditional clause is not a receipt.
    text = re.sub(r'「[^」]*」|“[^”]*”|"[^"\n]*"', '', text)
    result = []
    for clause in re.split(r'[，,。；;\n！？!?]', text):
        for match in re.finditer(r'(?:已经|已)(?:(?:成功|为你|为您|帮你|帮您)){0,2}取消|(?:已经|已)(?:将|把)[^，。；]{1,24}取消|取消.{0,5}(?:成功|完成|生效)', clause):
            prefix=clause[:match.start()]
            if re.search(r'如果|假如|确认后|才能|才会',prefix):continue
            if re.search(r'(?:尚未|还未|未能|没有|并未|并不|不能|无法|无需|不必|尚不|未|没|不)(?:确定|确认|说明|声称|表示)?\s*$',prefix):continue
            if re.search(r'没有|尚未|还未|未能|未|没|不',match[0]):continue
            if re.match(r'(?:后|时|才)',clause[match.end():]):continue
            result.append(clause);break
    return result


def goal_cancel_allowed(quote, request, goal_texts=()):
    if quote not in request or re.search(r'(?:不要|别|不想|勿|不)\s*(?:(?:帮|替|为)我|再|自动)?\s*(?:停止|取消|终止)|怎么|如何|假设|如果', quote):
        return False
    for match in re.finditer(re.escape(quote),request):
        if re.search(r'(?:不要|别|不想|勿|不)\s*$',request[max(0,match.start()-8):match.start()]):return False
    if not re.search(r'取消|停止|终止|算了|不办|不转了', quote):
        return False
    explicit = re.search(r'任务|目标|操作单|整个计划|这个计划|该计划|这项计划|未执行的步骤', quote)
    if explicit:
        return True
    if re.fullmatch(r'\s*(?:确认|同意)\s*(?:取消|停止|退订)[。！!\s]*', request):
        return False
    if re.fullmatch(r'\s*(?:算了|不办了|不转了|取消|停止|终止)[。！!\s]*', request):
        return True
    business = r'订阅|续费|续订|会员|代扣|合同'
    # Never turn the business verb in “取消订阅” into cancellation of the goal
    # whose purpose is precisely to cancel that subscription.
    if re.search(business, request) or any(re.search(business, text) for text in goal_texts):
        return False
    return True
