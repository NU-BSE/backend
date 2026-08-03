import pytest

from app.core.config import Settings
from app.core.errors import ApiError
from app.kv.store import MemoryTTLStore
from app.services.email_codes import EmailCodeService


def make_service(**overrides) -> tuple[EmailCodeService, MemoryTTLStore]:
    kwargs = dict(
        _env_file=None,
        jwt_secret="unit-test-secret-" + "y" * 30,
        database_url="sqlite+aiosqlite:///:memory:",
        redis_url="",
    )
    kwargs.update(overrides)
    settings = Settings(**kwargs)
    store = MemoryTTLStore()
    return EmailCodeService(store, settings), store


async def test_request_and_verify_roundtrip():
    service, _ = make_service()
    result = await service.request_code(
        email="a@b.c", name="A", purpose="login", client_ip="1.2.3.4"
    )
    assert len(result.code) == 6
    challenge = await service.verify_code(
        challenge_id=result.challenge_id, email="a@b.c", code=result.code
    )
    assert challenge.email == "a@b.c"
    assert challenge.name == "A"
    assert challenge.purpose == "login"


async def test_cooldown_blocks_immediate_resend():
    service, _ = make_service()
    await service.request_code(email="a@b.c", name=None, purpose="login", client_ip="1.1.1.1")
    with pytest.raises(ApiError) as excinfo:
        await service.request_code(
            email="a@b.c", name=None, purpose="login", client_ip="1.1.1.1"
        )
    assert excinfo.value.status_code == 429
    assert excinfo.value.extra["retryAfterSeconds"] > 0


async def test_per_email_hourly_limit():
    service, store = make_service(email_code_max_per_email_per_hour=2)
    for _ in range(2):
        await store.delete("auth:cooldown:a@b.c")
        await service.request_code(
            email="a@b.c", name=None, purpose="login", client_ip="9.9.9.9"
        )
    await store.delete("auth:cooldown:a@b.c")
    with pytest.raises(ApiError) as excinfo:
        await service.request_code(
            email="a@b.c", name=None, purpose="login", client_ip="9.9.9.9"
        )
    assert excinfo.value.status_code == 429


async def test_per_ip_hourly_limit():
    service, store = make_service(email_code_max_per_ip_per_hour=1)
    await service.request_code(email="a@b.c", name=None, purpose="login", client_ip="8.8.8.8")
    await store.delete("auth:cooldown:x@y.z")
    with pytest.raises(ApiError) as excinfo:
        await service.request_code(
            email="x@y.z", name=None, purpose="login", client_ip="8.8.8.8"
        )
    assert excinfo.value.status_code == 429


async def test_wrong_code_increments_attempts_and_rejects():
    service, _ = make_service()
    result = await service.request_code(
        email="a@b.c", name=None, purpose="login", client_ip="2.2.2.2"
    )
    wrong = "000000" if result.code != "000000" else "000001"
    with pytest.raises(ApiError) as excinfo:
        await service.verify_code(
            challenge_id=result.challenge_id, email="a@b.c", code=wrong
        )
    assert excinfo.value.code == "INVALID_CODE"

    # Correct code still works after a miss.
    challenge = await service.verify_code(
        challenge_id=result.challenge_id, email="a@b.c", code=result.code
    )
    assert challenge.email == "a@b.c"


async def test_email_mismatch_rejected():
    service, _ = make_service()
    result = await service.request_code(
        email="a@b.c", name=None, purpose="login", client_ip="3.3.3.3"
    )
    with pytest.raises(ApiError):
        await service.verify_code(
            challenge_id=result.challenge_id, email="other@b.c", code=result.code
        )
