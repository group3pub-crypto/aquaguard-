"""LLM settings, read from the environment by this module itself (no coupling to app.config)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_MODEL = "claude-haiku-5-5"  # low latency / cost; claude-sonnet-5-5 is the optional upgrade


class LLMSettings(BaseSettings):
    """All fields come from env vars (no prefix): ANTHROPIC_API_KEY, LLM_MODEL, LLM_ENABLED, ..."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    anthropic_api_key: SecretStr | None = Field(None, validation_alias="ANTHROPIC_API_KEY")
    llm_enabled: bool = Field(False, validation_alias="LLM_ENABLED")  # opt-in: data leaves the host
    llm_model: str = Field(DEFAULT_MODEL, validation_alias="LLM_MODEL")
    llm_timeout_s: float = Field(4.0, gt=0, le=4.0, validation_alias="LLM_TIMEOUT_S")
    llm_max_tokens: int = Field(600, ge=64, le=2000, validation_alias="LLM_MAX_TOKENS")
    llm_daily_budget: int = Field(
        500, ge=0, validation_alias="LLM_DAILY_BUDGET"
    )  # calls/UTC day; 0 = none
    llm_max_concurrency: int = Field(4, ge=1, le=32, validation_alias="LLM_MAX_CONCURRENCY")
    llm_language: Literal["en", "hi"] = Field("en", validation_alias="LLM_LANGUAGE")
    llm_breaker_threshold: int = Field(5, ge=1, validation_alias="LLM_BREAKER_THRESHOLD")
    llm_breaker_cooldown_s: float = Field(60.0, ge=1, validation_alias="LLM_BREAKER_COOLDOWN_S")
    llm_cache_size: int = Field(1024, ge=0, validation_alias="LLM_CACHE_SIZE")

    @property
    def api_key(self) -> str | None:
        raw = self.anthropic_api_key.get_secret_value().strip() if self.anthropic_api_key else ""
        return raw if raw and not raw.startswith("CHANGE_ME") else None
