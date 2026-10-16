"""Remove authentication material from ordinary dialogue/model payloads."""
import re

SECRET_KEYS = {'confirmation_token', 'code', 'code_hash', 'password', 'secret', 'operator_token', 'approver_token', 'csrf_token', 'api_key', 'model_api_key', 'deepseek_api_key', 'authorization'}
API_KEY = re.compile(r'\bsk-[A-Za-z0-9_-]{16,}\b')
AUTH_TEXT = re.compile(r'((?:验证码|短信码|OTP|密码|confirmation_token|operator_token)\s*[:：=]?\s*)[A-Za-z0-9_./+\-=]{4,}', re.I)


def scrub_auth(value):
    if isinstance(value, dict):
        return {key: '[已隐藏认证信息]' if key.lower() in SECRET_KEYS else scrub_auth(item) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_auth(item) for item in value]
    if isinstance(value, str):
        return API_KEY.sub('[已隐藏API密钥]', AUTH_TEXT.sub(r'\1[已隐藏认证信息]', value))
    return value


PRIVATE_MODEL_KEYS = {'account_number', 'target_account', 'birthday_address', 'birthday_recipient',
                      'address', 'recipient', 'source_reference_private'}
FULL_ACCOUNT = re.compile(r'(?<![A-Za-z0-9])[0-9]{12,30}(?![A-Za-z0-9])')
CONTACT_TEXT = re.compile(r'((?:收货地址|配送地址|收件地址|地址|收货人|收件人)(?:是|为|[:：])?\s*)([^，,。；;\n]{1,200})')
ACCOUNT_TEXT = re.compile(r'((?:收款账号|银行账号|对方账号|账号|账户号码)(?:是|为|[:：])?\s*)[A-Za-z0-9][A-Za-z0-9 -]{5,49}')


def model_payload(value, private_values=()):
    """Minimum necessary external fields; raw local forms remain authoritative.

    Known contact values are removed even in inherited messages. Free-form text
    cannot provide complete DLP; the limitation is disclosed in the privacy doc.
    """
    private = {str(v) for v in private_values if isinstance(v, str) and len(v) >= 2}
    def find(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if key.lower() in PRIVATE_MODEL_KEYS and isinstance(child, str) and len(child) >= 2:
                    private.add(child)
                find(child)
        elif isinstance(item, list):
            for child in item: find(child)
    find(value)
    def clean(item):
        if isinstance(item, dict):
            return {key: None if key.lower() in PRIVATE_MODEL_KEYS else clean(child) for key, child in item.items()}
        if isinstance(item, list): return [clean(child) for child in item]
        if isinstance(item, str):
            item = scrub_auth(item)
            for secret in sorted(private, key=len, reverse=True): item = item.replace(secret, '[本地敏感资料]')
            item = ACCOUNT_TEXT.sub(r'\1[完整账号留在本地]', item)
            return CONTACT_TEXT.sub(r'\1[本地敏感资料]', FULL_ACCOUNT.sub('[完整账号留在本地]', item))
        return item
    return clean(value)
