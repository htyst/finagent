"""One process-wide clock. Manual deployments must use one API worker."""
from datetime import date, datetime, time, timedelta, timezone
from threading import RLock
from zoneinfo import ZoneInfo
import os

SHANGHAI = ZoneInfo('Asia/Shanghai')
class Clock:
    def now(self) -> datetime:
        raise NotImplementedError
class RealClock(Clock):
    def now(self):
        return datetime.now(timezone.utc)
class ManualClock(Clock):
    def __init__(self, value='2026-09-24'):
        self._lock = RLock()
        self.set(value)
    def set(self, value):
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if isinstance(value, date) and not isinstance(value, datetime):
            value = datetime.combine(value, time(12), SHANGHAI)
        if value.tzinfo is None:
            value = value.replace(tzinfo=SHANGHAI)
        with self._lock:
            self._value = value.astimezone(timezone.utc)
        return self.now()
    def now(self):
        with self._lock:
            return self._value
    def advance(self, days=0, seconds=0):
        with self._lock:
            self._value += timedelta(days=days, seconds=seconds)
            return self._value

_clock = None
_lock = RLock()
def configure(mode=None, initial='2026-09-24'):
    global _clock
    mode = mode or os.getenv('FINAGENT_CLOCK', 'manual')
    if mode not in {'real', 'manual'}:
        raise ValueError('clock must be real or manual')
    with _lock:
        _clock = ManualClock(initial) if mode == 'manual' else RealClock()
    return _clock
def get_clock():
    global _clock
    with _lock:
        if _clock is None:
            configure(initial=os.getenv('FINAGENT_CLOCK_INITIAL', '2026-09-24'))
        return _clock
def now():
    return get_clock().now()
def business_day(setting=None):
    return now().astimezone(SHANGHAI).date()
