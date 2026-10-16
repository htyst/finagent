"""One request budget shared by understanding, repairs and legacy model adapters."""
from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic
from app.domain.errors import DomainError

ACTIVE = ContextVar('dialogue_budget', default=None)
SEMANTIC_DISPATCH = ContextVar('semantic_dispatch', default=False)


@contextmanager
def budget(calls, seconds):
    value = {'remaining': calls, 'deadline': monotonic() + seconds, 'calls': 0}
    token = ACTIVE.set(value)
    try:
        yield value
    finally:
        ACTIVE.reset(token)


def remaining_timeout(timeout):
    value = ACTIVE.get()
    if value is None:
        return timeout
    remaining = value['deadline'] - monotonic()
    if value['remaining'] <= 0 or remaining <= 0:
        raise DomainError('MODEL_BUDGET_EXCEEDED', '本轮模型调用或总耗时达到上限；没有自动执行操作，请重试或查看已有状态', 503)
    value['remaining'] -= 1
    value['calls'] += 1
    return min(timeout, remaining)
