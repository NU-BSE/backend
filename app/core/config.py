from functools import lru_cache

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
    app_name: str = "Creepy.IM API"
    app_version: str = "0.1.0"
    cors_origins: str = ""

    database_url: str = "postgresql+asyncpg://creepy:creepy@localhost:5432/creepy"
    redis_url: str = "redis://localhost:6379/0"
    apply_schema_on_startup: bool = True
    schema_file: str = "sql/migrate_001.sql"

    jwt_secret: str = Field(default="")
    jwt_issuer: str = "creepy-im"
    jwt_audience: str = "creepy-im-api"
    access_token_ttl_minutes: int = Field(default=15, ge=1, le=1440)
    refresh_token_ttl_days: int = Field(default=30, ge=1, le=365)

    admin_emails: str = ""

    # Accounts that hold the product without paying for it, for store review.
    #
    # Google Play requires working credentials for anything behind a login, and
    # a reviewer cannot be asked to make a real purchase. These accounts resolve
    # to full entitlements with no subscription row at all.
    #
    # Comma-separated `email:Name` pairs, e.g. "admin@creepy.im:Admin349401".
    # The name is required and matched exactly, case included: the address
    # alone is guessable, so the pair is the credential. A bare email (no
    # colon) is accepted and matches on address only — weaker, and not what a
    # published demo account should use.
    #
    # Empty by default: a demo bypass that shipped on by default would be a way
    # to get the paid product for free. Keep this disjoint from ADMIN_EMAILS —
    # these credentials are handed to strangers, and /admin/grant must not be
    # among the things they can reach.
    demo_accounts: str = ""

    email_code_ttl_seconds: int = Field(default=300, ge=30, le=3600)
    email_code_resend_cooldown_seconds: int = Field(default=60, ge=5, le=3600)
    email_code_max_per_email_per_hour: int = Field(default=5, ge=1, le=100)
    email_code_max_per_ip_per_hour: int = Field(default=20, ge=1, le=1000)
    email_code_max_verify_attempts: int = Field(default=5, ge=1, le=20)

    brevo_api_key: str = ""
    email_from: str = ""

    llm_upstream_url: str = ""
    llm_upstream_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = Field(default=120.0, ge=5, le=600)
    llm_mock: bool = False

    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    llm_model_fast: str = ""
    llm_model_normal: str = ""
    llm_model_expert: str = ""

    llm_normal_score_threshold: int = Field(default=4, ge=0, le=100)
    llm_expert_score_threshold: int = Field(default=10, ge=0, le=100)
    llm_allow_expert: bool = True
    llm_expert_daily_user_limit: int = Field(default=20, ge=0)
    llm_expert_global_daily_budget_usd: float | None = None
    llm_expert_max_calls_per_run: int = Field(default=3, ge=1)
    llm_request_timeout_seconds: float = Field(default=60.0, ge=5, le=600)
    llm_max_retries: int = Field(default=1, ge=0, le=3)

    # Google Play Billing.
    #
    # The service account must have "View financial data" on the Play Console
    # and be linked to the app. Either point at a mounted key file or paste the
    # JSON; the file is preferred so the key never lands in `docker inspect`.
    play_package_name: str = "im.creepy.app"
    play_service_account_file: str = ""
    play_service_account_json: str = ""
    play_api_timeout_seconds: float = Field(default=20.0, ge=1, le=120)
    # Shared secret for the RTDN push endpoint. Pub/Sub cannot be trusted by
    # source IP, so an unauthenticated webhook lets anyone forge a renewal.
    play_rtdn_secret: str = ""
    # Refuse purchases whose token was already redeemed by a different user.
    # Only relevant if you ever disable it: a purchase token is bearer proof of
    # payment, so a leaked one would otherwise entitle whoever presents it.
    play_verify_enabled: bool = True

    # Device-bound model licensing.
    #
    # The signing key is asymmetric on purpose: the app must verify licences,
    # so whatever verifies them ships inside the app. An HMAC secret would
    # therefore be extractable from any build and would let its holder mint
    # licences; the app carries only the public half.
    license_signing_key: str = ""
    """RSA private key, PEM. Empty disables licence issuance entirely."""
    model_keys_json: str = ""
    """{"<modelVersion>": "<base64 AES key>"} — dropping a version revokes it."""
    require_strong_integrity: bool = False
    """Demand MEETS_STRONG_INTEGRITY rather than MEETS_DEVICE_INTEGRITY."""

    # Where translated MCP server bundles are written. Content-addressed, so
    # this is a cache: deleting it costs a re-translation, never correctness.
    mcp_bundle_root: str = "var/mcp-bundles"

    # On-device model weights served to paying clients.
    model_artifact_root: str = "app/models"
    # Entitlement required to download weights. Empty disables the gate, which
    # is only appropriate in development.
    model_download_requires_entitlement: bool = True

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def admin_email_set(self) -> set[str]:
        return {item.strip().lower() for item in self.admin_emails.split(",") if item.strip()}

    @property
    def demo_account_map(self) -> dict[str, str | None]:
        """Lower-cased email -> the exact name required with it, or None.

        None means "any name", which only happens for a bare address in the
        configuration. Names are kept verbatim because they are compared
        exactly; only the email is normalized.
        """
        accounts: dict[str, str | None] = {}
        for item in self.demo_accounts.split(","):
            entry = item.strip()
            if not entry:
                continue
            email, separator, name = entry.partition(":")
            accounts[email.strip().lower()] = name.strip() if separator else None
        return accounts

    @property
    def demo_account_set(self) -> set[str]:
        return set(self.demo_account_map)

    @model_validator(mode="after")
    def validate_environment(self) -> "Settings":
        if not self.jwt_secret or len(self.jwt_secret) < 32:
            raise ValueError("JWT_SECRET is required and must be at least 32 characters long")
        if self.is_production:
            missing: list[str] = []
            if not self.brevo_api_key:
                missing.append("BREVO_API_KEY")
            if not self.email_from:
                missing.append("EMAIL_FROM")
            if not self.llm_upstream_url and not self.llm_mock:
                missing.append("LLM_UPSTREAM_URL")
            if not self.cors_origins:
                missing.append("CORS_ORIGINS")
            if not self.openrouter_api_key:
                missing.append("OPENROUTER_API_KEY")
            if not self.llm_model_fast:
                missing.append("LLM_MODEL_FAST")
            if not self.llm_model_normal:
                missing.append("LLM_MODEL_NORMAL")
            if not self.llm_model_expert:
                missing.append("LLM_MODEL_EXPERT")
            if missing:
                raise ValueError(f"Missing production settings: {', '.join(missing)}")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
