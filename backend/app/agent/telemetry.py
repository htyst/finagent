"""Opt-in evaluation metrics. No messages, credentials or response bodies retained."""
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter

_current = ContextVar("model_metrics", default=None)

@contextmanager
def collect():
    attempts = []
    token = _current.set(attempts)
    try:
        yield attempts
    finally:
        _current.reset(token)

def measured_post(http, url, **kwargs):
    started = perf_counter()
    item = {"http_status": None, "error": None, "usage": None}
    try:
        response = http.post(url, **kwargs)
        item["http_status"] = response.status_code
        if response.status_code == 200:
            try:
                usage = response.json().get("usage", {})
                item["usage"] = {key: value for key, value in usage.items()
                    if key in {"prompt_tokens", "completion_tokens", "total_tokens",
                               "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"}
                    and type(value) is int and value >= 0} or None
            except (ValueError, AttributeError, TypeError):
                pass
        return response
    except Exception as error:
        item["error"] = type(error).__name__
        raise
    finally:
        item["seconds"] = round(perf_counter() - started, 6)
        sink = _current.get()
        if sink is not None:
            sink.append(item)
