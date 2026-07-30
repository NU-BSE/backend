from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = "development"
    app_debug: bool = False
    app_name: str = "Attestation API"
    cors_origins: str = ""
    environment: str = "development"
    play_integrity_enabled: bool = False

    database_url: str = "postgresql+asyncpg://attestation:attestation@localhost:5432/attestation"
    redis_url: str = "redis://localhost:6379/0"

    auth_jwt_secret: str = Field(min_length=32)
    auth_jwt_issuer: str = "attestation-local"
    auth_jwt_audience: str = "attestation-api"
    allow_dev_token_endpoint: bool = False

    nonce_ttl_ms: int = Field(default=90_000, ge=5_000, le=300_000)
    max_attested_latency_ms: int = Field(default=2_500, ge=100, le=60_000)

    android_package_name: str = "com.attestation.security"
    android_cert_sha256: str = ""
    play_integrity_project_number: str | None = None
    google_service_account_file: Path | None = None
    android_attestation_root_sha256: str = ""

    jit_private_key_file: Path = Path("./secrets/jit-ed25519-private.pem")
    jit_kid: str = "jit-dev-2026-01"
    jit_api_audience: str = "sensitive-api"
    jit_ttl_restricted_seconds: int = 60
    jit_ttl_standard_seconds: int = 120
    jit_ttl_elevated_seconds: int = 120
    jit_ttl_highest_seconds: int = 180

    velocity_burst_requests_per_minute: int = 30
    velocity_ip_hopping_distinct_ips_1h: int = 5
    velocity_asn_hopping_distinct_asns_1h: int = 2
    velocity_low_entropy_bits_per_symbol: float = 2.0

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def android_certificate_allowlist(self) -> set[str]:
        return {
            item.replace(":", "").strip().lower()
            for item in self.android_cert_sha256.split(",")
            if item.strip()
        }

    @property
    def android_root_allowlist(self) -> set[str]:
        return {
            item.replace(":", "").strip().lower()
            for item in self.android_attestation_root_sha256.split(",")
            if item.strip()
        }

    @model_validator(mode="after")
    def validate_production(self) -> "Settings":
        if not self.is_production:
            return self

        missing: list[str] = []
        if not self.android_certificate_allowlist:
            missing.append("ANDROID_CERT_SHA256")
        if not self.play_integrity_project_number:
            missing.append("PLAY_INTEGRITY_PROJECT_NUMBER")
        if not self.google_service_account_file:
            missing.append("GOOGLE_SERVICE_ACCOUNT_FILE")
        if self.allow_dev_token_endpoint:
            raise ValueError("ALLOW_DEV_TOKEN_ENDPOINT must be false in production")
        if missing:
            raise ValueError(f"Missing production settings: {', '.join(missing)}")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
