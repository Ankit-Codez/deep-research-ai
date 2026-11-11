"""Load validated settings without reading credentials at import time."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigurationError(ValueError):
    """An actionable configuration error safe to display to the user."""


class Settings(BaseSettings):
    """Environment variables override the optional .env in the current directory.

    Keys are optional until a real model is requested. Mock adapters, schema
    tests, and future offline tools can run without provider credentials.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    mock_mode: bool = False
    database_url: SecretStr = SecretStr("postgresql+psycopg://deepresearch@127.0.0.1:55432/deepresearch")
    app_env: Literal["development", "test", "production"] = "development"
    auth_cookie_secure: bool = False
    auth_session_hours: int = Field(default=12, ge=1, le=168)
    auth_allowed_origins: list[str] = Field(default_factory=lambda: [
        "http://127.0.0.1:8000", "http://127.0.0.1:5173", "http://localhost:5173"])
    auth_allow_registration: bool = True
    auth_rate_limit_attempts: int = Field(default=10, ge=1, le=100)
    auth_rate_limit_window_seconds: int = Field(default=900, ge=30, le=3600)
    quota_daily_runs: int = Field(default=20, ge=1, le=10000)
    quota_daily_tokens: int = Field(default=1_000_000, ge=1)
    quota_daily_cost_usd: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    quota_max_active_runs: int = Field(default=3, ge=1, le=100)
    research_max_resumes: int = Field(default=2, ge=0, le=5)
    progress_stream_seconds: int = Field(default=25, ge=1, le=120)
    admin_daily_spend_alert_usd: float = Field(default=10, gt=0, allow_inf_nan=False)
    admin_queue_alert_seconds: int = Field(default=120, ge=1)
    account_export_max_bytes: int = Field(default=20_000_000, ge=1024, le=100_000_000)
    frontend_dist_dir: Path = Path("frontend/dist")
    worker_poll_seconds: float = Field(default=1, ge=0.1, le=30)
    worker_lease_seconds: int = Field(default=60, ge=10, le=600)
    llm_provider: Literal["openai", "anthropic", "google_genai"] = "openai"
    llm_model: str = Field(default="gpt-4.1-mini", min_length=1)
    llm_temperature: float | None = Field(default=0.0, ge=0, le=1)
    llm_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    llm_max_retries: int = Field(default=1, ge=0, le=5)
    llm_max_output_tokens: int = Field(default=8192, ge=128, le=16000)
    gemini_thinking_level: Literal["minimal", "low", "medium", "high"] | None = None
    research_max_retries: int = Field(default=2, ge=0, le=5)
    research_max_llm_calls: int = Field(default=16, ge=1, le=100)
    research_max_searches: int = Field(default=12, ge=1, le=100)
    research_max_elapsed_seconds: float = Field(default=180, gt=0, le=1800)
    research_max_tokens: int = Field(default=200000, ge=1)
    research_max_cost_usd: float | None = Field(default=None, gt=0)
    research_input_cost_per_million: float = Field(default=0, ge=0)
    research_output_cost_per_million: float = Field(default=0, ge=0)
    research_search_cost_usd: float = Field(default=0, ge=0)
    provider_backoff_seconds: float = Field(default=0.5, ge=0, le=10)
    provider_max_retries: int = Field(default=1, ge=0, le=5)
    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    google_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    search_provider: Literal["tavily"] = "tavily"
    tavily_api_key: SecretStr | None = None
    research_max_pages: int = Field(default=5, ge=1, le=20)
    research_max_page_attempts: int = Field(default=15, ge=1, le=100)
    research_max_source_retries: int = Field(default=2, ge=0, le=5)
    research_results_per_query: int = Field(default=5, ge=1, le=10)
    research_max_content_chars: int = Field(default=8000, ge=200, le=30000)
    research_max_response_bytes: int = Field(default=1_000_000, ge=1024, le=5_000_000)
    research_request_timeout: float = Field(default=10, gt=0, le=60)
    research_page_deadline: float = Field(default=30, gt=0, le=120)
    research_request_delay: float = Field(default=1, ge=0, le=10)
    research_max_redirects: int = Field(default=3, ge=0, le=5)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @model_validator(mode="after")
    def cost_rates_required(self):
        if self.app_env == "production" and not self.auth_cookie_secure:
            raise ValueError("Production login requires AUTH_COOKIE_SECURE=true and HTTPS.")
        if self.quota_daily_cost_usd is not None and self.research_max_cost_usd is None:
            raise ValueError("A daily cost quota requires RESEARCH_MAX_COST_USD and configured cost rates.")
        if self.research_max_cost_usd is not None and (
            self.research_input_cost_per_million <= 0
            or self.research_output_cost_per_million <= 0
            or self.research_search_cost_usd <= 0
        ):
            raise ValueError("A cost budget requires positive input, output, and search price estimates.")
        return self

    @field_validator("llm_model", mode="before")
    @classmethod
    def strip_model_name(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("llm_temperature", mode="before")
    @classmethod
    def blank_temperature_uses_provider_default(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("gemini_thinking_level", mode="before")
    @classmethod
    def blank_thinking_level_uses_provider_default(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator(
        "openai_api_key", "anthropic_api_key", "google_api_key",
        "gemini_api_key", "tavily_api_key", mode="before",
    )
    @classmethod
    def normalize_key(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    def require_api_key(self) -> SecretStr:
        """Require a real provider key; mock factories bypass this method."""
        if self.llm_provider == "google_genai":
            key = self.google_api_key or self.gemini_api_key
            variable = "GOOGLE_API_KEY (or GEMINI_API_KEY)"
        else:
            key = getattr(self, f"{self.llm_provider}_api_key")
            variable = f"{self.llm_provider.upper()}_API_KEY"
        if key is None or not key.get_secret_value().strip():
            raise ConfigurationError(
                f"Missing {variable}. Set it in the environment or .env "
                f"before using provider '{self.llm_provider}'."
            )
        return key
