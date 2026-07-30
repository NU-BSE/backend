from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db.models import AttestationResult, Device


class DeviceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, device_id: str) -> Device | None:
        return await self.session.get(Device, device_id)

    async def bind_or_update(
        self,
        *,
        device_id: str,
        user_id: str,
        platform: str,
        model_family: str | None,
        strong_integrity: bool,
    ) -> tuple[Device, bool]:
        device = await self.get(device_id)
        now = datetime.now(UTC)
        first_seen = device is None

        if device is not None and device.user_id != user_id:
            raise ApiError(403, "DEVICE_ALREADY_BOUND", "Device is bound to another user")

        if device is None:
            device = Device(
                device_id=device_id,
                user_id=user_id,
                platform=platform,
                model_family=model_family,
                first_seen_at=now,
                last_seen_at=now,
                strong_integrity_since=now if strong_integrity else None,
            )
            self.session.add(device)
        else:
            device.last_seen_at = now
            device.model_family = model_family or device.model_family
            if strong_integrity:
                device.strong_integrity_since = device.strong_integrity_since or now
            else:
                device.strong_integrity_since = None

        await self.session.flush()
        return device, first_seen

    async def update_counter_atomically(self, device_id: str, new_counter: int) -> None:
        result = await self.session.execute(
            update(Device)
            .where(Device.device_id == device_id, Device.last_counter < new_counter)
            .values(last_counter=new_counter, last_seen_at=datetime.now(UTC))
        )
        if result.rowcount != 1:
            raise ApiError(403, "ASSERTION_FAILED", "Counter replay detected")


class AttestationResultRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(
        self,
        *,
        user_id: str,
        device_id: str,
        platform: str,
        trust_tier: str,
        action_hash: str,
        verdict: dict[str, Any],
        classifier_reasons: list[str],
        risk_flags: list[str],
    ) -> AttestationResult:
        result = AttestationResult(
            user_id=user_id,
            device_id=device_id,
            platform=platform,
            trust_tier=trust_tier,
            action_hash=action_hash,
            verdict=verdict,
            classifier_reasons=classifier_reasons,
            risk_flags=risk_flags,
        )
        self.session.add(result)
        await self.session.flush()
        return result
