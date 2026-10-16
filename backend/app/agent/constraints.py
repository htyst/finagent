"""Guard unsupported write requirements before any model or preparation tool."""
import re

SCHEDULE_REPLY = ('暂不支持预约或定时转账、卡片操作；没有创建预约，也没有生成可立即执行的操作单。'
                  '如需现在准备普通操作，请重新说明付款账户、收款对象和金额，核对后再点击确认。')
COLLECTION_REPLY = ('暂不支持AA收款、收款请求或成员付款状态查询；没有创建收款请求，也没有扣款或入账。'
                    '请通过支持AA收款的支付渠道发起并核对到账。')


def fresh_write_request(text):
    if re.search(r'怎么|如何|是否|能不能|什么|多少|吗|[?？]|不要|别|取消', text):
        return False
    return bool(re.search(r'从[^。！？]*转|给[^。！？]*转|转给|转入|转到|冻结|解冻', text))


def unsupported_write(text, intent, context):
    from app.agent.requirements import banking_time_text
    text=banking_time_text(text)
    collecting = bool(re.search(r'AA\s*收款|发起[^。！？]*AA|(?:发起|向[^。！？]*|帮我[^。！？]*)收款', text, re.I))
    if collecting or (context.get('unsupported_write') == 'collection'
                      and re.search(r'收款|付款状态|支付状态|谁.*(?:付|交)|每人.*元', text)):
        return 'collection', COLLECTION_REPLY
    timed = bool(re.search(
        r'预约|定时|明天|后天|明早|今晚|下周|下个(?:月|星期)|下星期|生日(?:当天|时)|届时|到时候|那天|'
        r'[0-9一二三四五六七八九十]+(?:秒|分钟|小时|天)后|'
        r'\d{1,2}\s*[点时](?:\d{1,2}分?)?|\d{1,2}[:：]\d{2}', text) or re.search(
        r'^(?:请|在|安排在|改到|改成)?\s*(?:(?:\d{4}年)?\d{1,2}月\d{1,2}[日号]|'
        r'\d{4}-\d{2}-\d{2})(?!\d|\s*(?:到|至))', text))
    writing = bool(re.search(r'转账|转给|转入|转到|从[^。！？]*转|给[^。！？]*转|冻结|解冻', text))
    revising_write = intent in {'transfer', 'conditional_transfer', 'card', 'investigate_card'} and bool(
        re.search(r'改|换|安排|执行|扣款|预约|定时', text))
    if timed and (writing or revising_write):
        return 'schedule', SCHEDULE_REPLY
    previous = context.get('unsupported_write')
    if previous and not fresh_write_request(text) and re.search(r'改|金额|确认|继续|执行|照|按|现在.*状态', text):
        return (previous, SCHEDULE_REPLY if previous == 'schedule' else COLLECTION_REPLY)
    if previous == 'schedule' and re.search(r'预约状态|定时状态|到时.*执行', text):
        return 'schedule', SCHEDULE_REPLY
    return None
