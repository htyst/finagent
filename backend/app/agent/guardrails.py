"""Shared language safety boundary; unsupported conditions never disappear."""
import re
from decimal import Decimal
CONDITION_MARKERS = ('如果', '若', '才', '否则', '超过', '大于', '小于', '低于',
                     '以上', '以下', '不足', '不超过', '不到', '至少', '至多')
EXTREME_MARKERS = ('最大', '最小', '最多', '最少', '最贵')
def has_condition(text):
    return any(word in text for word in CONDITION_MARKERS)
_DIGITS = dict(zip('零〇一二两三四五六七八九', (0,0,1,2,2,3,4,5,6,7,8,9)))
def chinese_number(text):
    if re.fullmatch(r'[0-9]+(?:\.[0-9]{1,2})?', text):
        return Decimal(text)
    if '点' in text:
        whole, fraction = text.split('点', 1)
        if not 1 <= len(fraction) <= 2 or any(c not in _DIGITS for c in fraction):
            raise ValueError('unsupported fraction')
        return chinese_number(whole) + Decimal('0.' + ''.join(str(_DIGITS[c]) for c in fraction))
    if not any(c in text for c in '十百千万'):
        return Decimal(''.join(str(_DIGITS[c]) for c in text))
    if re.search(r'[百千万][一二两三四五六七八九]$', text):
        raise ValueError('ambiguous colloquial number; use Arabic digits')
    total = section = digit = 0
    for c in text:
        if c in _DIGITS:
            digit = _DIGITS[c]
        elif c in '十百千':
            section += (digit or 1) * {'十':10, '百':100, '千':1000}[c]
            digit = 0
        elif c == '万':
            total += (section + digit) * 10000
            section = digit = 0
        else:
            raise ValueError('unsupported number')
    return Decimal(total + section + digit)
MONEY_PATTERN = re.compile(r'(?<![0-9.])([0-9]+(?:\.[0-9]{1,2})?|[零〇一二两三四五六七八九十百千万点]+)\s*(块钱|块|圆|元)')
def normalize_money(text):
    def replace(m):
        try:
            return format(chinese_number(m[1]), 'f') + '元'
        except ValueError:
            return m[0]
    return MONEY_PATTERN.sub(replace, text)
def money_minor(value):
    number = chinese_number(str(value)) * 100
    if number != number.to_integral_value() or number < 0:
        raise ValueError('amount must be nonnegative cents')
    return int(number)
