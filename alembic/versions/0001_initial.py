"""Initial attestation tables.

Revision ID: 0001_initial
Revises:
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "device",
        sa.Column("device_id", sa.String(length=128), primary_key=True),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("public_key_pem", sa.Text(), nullable=True),
        sa.Column("last_counter", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("strong_integrity_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("model_family", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
    )
    op.create_index("ix_device_user_id", "device", ["user_id"])

    op.create_table(
        "attestation_result",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column(
            "device_id",
            sa.String(length=128),
            sa.ForeignKey("device.device_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("trust_tier", sa.String(length=16), nullable=False),
        sa.Column("action_hash", sa.String(length=64), nullable=False),
        sa.Column("verdict", sa.JSON(), nullable=False),
        sa.Column("classifier_reasons", sa.JSON(), nullable=False),
        sa.Column("risk_flags", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_attestation_result_user_id", "attestation_result", ["user_id"])
    op.create_index("ix_attestation_result_device_id", "attestation_result", ["device_id"])
    op.create_index(
        "ix_attestation_result_device_created",
        "attestation_result",
        ["device_id", "created_at"],
    )

    op.create_table(
        "security_event",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.String(length=128), nullable=True),
        sa.Column("device_id", sa.String(length=128), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("asn", sa.String(length=64), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_security_event_user_id", "security_event", ["user_id"])
    op.create_index("ix_security_event_device_id", "security_event", ["device_id"])
    op.create_index(
        "ix_security_event_type_created",
        "security_event",
        ["event_type", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("security_event")
    op.drop_table("attestation_result")
    op.drop_table("device")
