"""Exact phone lookup within an already owner-filtered simulated directory."""
import re

PHONE = re.compile(r"(?<![0-9])(?:\+?86[ -]*)?(1[3-9](?:[ -]*[0-9]){9})(?![0-9])")
LABELLED = re.compile(r"(?:手机号|手机号码|电话(?:号码)?)[是为：: ]*([+0-9][0-9 +\-]*)")

def normalize_phone(value: str | None) -> str | None:
    if not value:
        return None
    compact = re.sub(r"[ -]", "", value.strip())
    compact = re.sub(r"^(?:手机号|手机号码|电话(?:号码)?)[是为：:]*", "", compact)
    if compact.startswith("+86"):
        compact = compact[3:]
    elif len(compact) == 13 and compact.startswith("86"):
        compact = compact[2:]
    return compact if re.fullmatch(r"1[3-9][0-9]{9}", compact) else None

def phone_reference(text: str) -> tuple[str | None, str | None]:
    if re.search(r"(?:我的|本人(?:的)?|付款人(?:的)?)(?:手机号|手机号码|电话)|(?:备注|附言)[^，。；;\n]*(?:手机号|手机号码|电话)", text):
        return None, "请明确收款人的手机号；本人联系方式或转账附言不能用来定位收款对象。"
    labelled = LABELLED.findall(text)
    if any(normalize_phone(item) is None for item in labelled):
        return None, "手机号格式不完整或不正确，请提供完整的11位手机号。"
    phones = list(dict.fromkeys(match.group(1).replace(" ", "").replace("-", "") for match in PHONE.finditer(text)))
    if not phones and not labelled and re.search(r"手机号|手机号码|电话(?:号码)?", text):
        return None, "手机号格式不完整或不正确，请提供完整的11位手机号。"
    if len(phones) > 1:
        return None, "本轮包含多个手机号，请明确本次收款人的手机号。"
    return (phones[0], None) if phones else (None, None)

def match_phone(reference: str, recipients):
    phone, error = phone_reference(reference)
    if error:
        return []
    if not phone:
        return None
    matches = [row for row in recipients if normalize_phone(getattr(row, "phone", None)) == phone]
    tail = re.search(r"尾号\s*([0-9]{4})(?![0-9])", reference)
    return [row for row in matches if row.account_last4 == tail.group(1)] if tail else matches

def mask_phone(phone: str | None) -> str | None:
    value = normalize_phone(phone)
    return value[:3] + "****" + value[-4:] if value else None


def note_reference(text: str) -> tuple[str | None, str | None]:
    """An explicit saved recipient remark, distinct from a transfer memo."""
    if re.search(r'转账(?:的)?备注|(?:转账)?附言', text):
        return None, None
    raw = re.findall(r'(?:收款人|收款对象)?备注(?:是|为|叫|[:：])\s*[“「"]?([^”」"\s，,。；;]+)', text)
    raw += re.findall(r'(?:转给|给|付给)(?:收款人|收款对象)?备注(?!是|为|叫|[:：])\s*[“「"]?([^”」"\s，,。；;]+)', text)
    notes = []
    for value in raw:
        value = re.split(r'的人|的收款人|的收款对象|(?:转|付)[0-9一二三四五六七八九十]', value, maxsplit=1)[0].strip()
        if value and value not in notes:
            notes.append(value)
    if len(notes) > 1:
        return None, '本轮提到多个收款人备注，请明确要匹配哪一个。'
    if notes and len(notes[0]) > 80:
        return None, '收款人备注最多80字，请核对保存的备注。'
    return (notes[0], None) if notes else (None, None)


def match_remark(reference: str | None, recipients):
    if not reference:
        return []
    ref = reference.strip()
    return [row for row in recipients if (getattr(row, 'remark', None) or '').strip() == ref]
