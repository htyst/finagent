"""Shared DeepSeek configuration and safe error handling; never log credentials."""
from dataclasses import dataclass, field
import os
import re
from urllib.parse import urlsplit

import httpx

from app.config import ROOT  # noqa: F401: load the local ignored .env
from app.agent.telemetry import measured_post
from app.domain.errors import DomainError


@dataclass(frozen=True)
class ModelConfig:
    api_key: str = field(repr=False)
    model: str = 'deepseek-flash'
    base_url: str = 'https://api.deepseek.com'

    @property
    def strict_tools(self):
        return urlsplit(self.base_url).path.rstrip('/') == '/beta'


def configuration(*, require_key=True):
    provider = (os.getenv('MODEL_PROVIDER') or 'deepseek').strip().lower()
    if provider != 'deepseek':
        raise DomainError('MODEL_CONFIG_INVALID', 'MODEL_PROVIDER 目前应设置为 deepseek', 503)
    key = (os.getenv('MODEL_API_KEY') or os.getenv('DEEPSEEK_API_KEY') or '').strip()
    model = (os.getenv('MODEL_NAME') or 'deepseek-flash').strip()
    base = (os.getenv('MODEL_BASE_URL') or 'https://api.deepseek.com').strip().rstrip('/')
    # Credentials may only be sent to the official service. No redirects/proxies supplied by chat.
    try:
        url = urlsplit(base)
    except ValueError as error:
        raise DomainError('MODEL_CONFIG_INVALID', 'MODEL_BASE_URL 格式无效，请检查本地配置', 503) from error
    if (url.scheme != 'https' or url.netloc != 'api.deepseek.com'
            or url.path not in ('', '/v1', '/beta') or url.query or url.fragment):
        raise DomainError('MODEL_CONFIG_INVALID', 'MODEL_BASE_URL 应为 https://api.deepseek.com（可带 /v1 或 /beta）', 503)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}', model):
        raise DomainError('MODEL_CONFIG_INVALID', 'MODEL_NAME 格式无效，请检查本地配置', 503)
    if require_key and not key:
        raise DomainError('MODEL_NOT_CONFIGURED', '请在项目本地 .env 配置 MODEL_API_KEY，保存后重新启动 Live', 503)
    return ModelConfig(api_key=key, model=model, base_url=base)


def public_status():
    try:
        config = configuration(require_key=False)
        return {'provider': 'deepseek', 'model': config.model, 'configured': bool(config.api_key),
                'configuration_error': None}
    except DomainError as error:
        return {'provider': 'deepseek', 'model': None, 'configured': False,
                'configuration_error': error.message}


def post_chat(http, body, *, timeout=30.0):
    from app.agent.model_budget import remaining_timeout
    timeout = remaining_timeout(timeout)
    config = configuration()
    body = dict(body, model=config.model, thinking={'type': 'disabled'}, stream=False)
    # DeepSeek strict function calling requires the beta endpoint. In standard mode
    # the application still validates every returned argument with strict Pydantic schemas.
    if 'tools' in body:
        body['tools'] = [dict(tool, function=dict(tool['function'], strict=config.strict_tools))
                         for tool in body['tools']]
    try:
        response = measured_post(http, config.base_url + '/chat/completions',
            headers={'Authorization': 'Bearer ' + config.api_key, 'Content-Type': 'application/json'},
            json=body, timeout=timeout, follow_redirects=False)
    except httpx.HTTPError as error:
        raise DomainError('MODEL_UNAVAILABLE', 'DeepSeek 连接失败或超时，请检查网络后重试', 503) from error
    errors = {
        400: ('MODEL_REQUEST_INVALID', 'DeepSeek 不接受当前请求，请检查模型名称和接口配置'),
        401: ('MODEL_AUTH_FAILED', 'DeepSeek API Key 无效，请更新本地 .env 后重新启动 Live'),
        402: ('MODEL_BALANCE_INSUFFICIENT', 'DeepSeek 账户余额不足，请充值后重试'),
        404: ('MODEL_REQUEST_INVALID', 'DeepSeek 模型或接口不存在，请检查 MODEL_NAME 和 MODEL_BASE_URL'),
        422: ('MODEL_REQUEST_INVALID', 'DeepSeek 请求参数不受支持，请检查模型配置'),
        429: ('MODEL_RATE_LIMITED', 'DeepSeek 请求过于频繁，请稍后重试'),
    }
    if response.status_code != 200:
        code, message = errors.get(response.status_code, ('MODEL_UNAVAILABLE', 'DeepSeek 服务暂时不可用，请稍后重试'))
        # Provider bodies can contain request fragments. Never reflect them to users or audit logs.
        raise DomainError(code, message, 503)
    return response


def probe():
    """One small real request without account data; does not prove business execution."""
    import json
    with httpx.Client(timeout=15.0) as http:
        response = post_chat(http, {'max_tokens': 120,
            'messages': [{'role': 'user', 'content': 'Call connection_check with ok=true.'}],
            'tools': [{'type': 'function', 'function': {'name': 'connection_check',
                'parameters': {'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
                               'required': ['ok'], 'additionalProperties': False}}}],
            'tool_choice': {'type': 'function', 'function': {'name': 'connection_check'}}}, timeout=15.0)
    try:
        calls = response.json()['choices'][0]['message']['tool_calls']
        if (len(calls) != 1 or calls[0]['function']['name'] != 'connection_check'
                or json.loads(calls[0]['function']['arguments']) != {'ok': True}):
            raise ValueError('unexpected probe response')
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise DomainError('MODEL_OUTPUT_MISMATCH', 'DeepSeek 已响应，但工具调用校验未通过', 503) from error
    return dict(public_status(), connected=True)
