from datetime import datetime, timezone

from app.kv.store import MemoryTTLStore
from app.services.usage import UsageMeter, _day_key, _seconds_until_next_utc_day


def make_meter() -> UsageMeter:
    return UsageMeter(MemoryTTLStore())


async def test_no_limit_always_allowed():
    meter = make_meter()
    for _ in range(5):
        check = await meter.check_and_increment("u1", None)
        assert check.allowed is True


async def test_limit_enforced_and_not_consumed_on_reject():
    meter = make_meter()
    for i in range(1, 4):
        check = await meter.check_and_increment("u2", 3)
        assert check.allowed is True
        assert check.used == i

    rejected = await meter.check_and_increment("u2", 3)
    assert rejected.allowed is False
    assert rejected.limit == 3
    assert rejected.retry_after_seconds > 0

    # A rejected attempt must not consume quota: still exactly 3 used.
    assert await meter.current("u2") == 3


async def test_day_key_uses_utc_date():
    now = datetime(2026, 8, 3, 23, 59, tzinfo=timezone.utc)
    assert _day_key("u", now).endswith("2026-08-03")


async def test_seconds_until_next_day_positive():
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    seconds = _seconds_until_next_utc_day(now)
    assert 0 < seconds <= 86400
