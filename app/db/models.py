import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class Device(Base):
    __tablename__ = "device"

    device_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    public_key_pem: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_counter: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    strong_integrity_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    model_family: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")

    results: Mapped[list["AttestationResult"]] = relationship(back_populates="device")


class AttestationResult(Base):
    __tablename__ = "attestation_result"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    device_id: Mapped[str] = mapped_column(
        ForeignKey("device.device_id", ondelete="CASCADE"), nullable=False, index=True
    )
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    trust_tier: Mapped[str] = mapped_column(String(16), nullable=False)
    action_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    verdict: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    classifier_reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    risk_flags: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    device: Mapped[Device] = relationship(back_populates="results")


class SecurityEvent(Base):
    __tablename__ = "security_event"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    device_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    asn: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


Index("ix_attestation_result_device_created", AttestationResult.device_id, AttestationResult.created_at)
Index("ix_security_event_type_created", SecurityEvent.event_type, SecurityEvent.created_at)
