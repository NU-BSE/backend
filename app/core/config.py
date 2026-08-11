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

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def admin_email_set(self) -> set[str]:
        return {item.strip().lower() for item in self.admin_emails.split(",") if item.strip()}

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
