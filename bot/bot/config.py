from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Bot configuration; everything comes from the environment."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: SecretStr
    telegram_admin_ids: str = ""
    bot_api_base_url: str = "http://backend:8000"
    bot_api_username: str = "bot_service"
    bot_api_password: SecretStr
    bot_state_file: str = ""
    bot_seen_file: str = ""  # default: seen_alerts.json next to BOT_STATE_FILE
    bot_audit_file: str = ""  # default: audit.jsonl next to BOT_STATE_FILE
    bot_poll_interval: float = Field(30.0, ge=5)
    bot_send_rate: float = Field(25.0, gt=0, le=30)
    bot_api_timeout: float = 10.0
    # must equal the backend SHUTOFF_WARNING_SECONDS: only used to display the shut-off countdown
    bot_shutoff_warning_seconds: float = Field(300.0, ge=0)
    # Optional LLM "smart mode" (natural-language intent routing). Off unless BOT_NLU_ENABLED=true.
    bot_nlu_enabled: bool = False
    anthropic_api_key: SecretStr | None = None
    llm_model: str = "claude-haiku-5-5"  # claude-sonnet-5-5 is the optional upgrade
    llm_timeout_s: float = Field(15.0, gt=0, le=60)
    bot_nlu_user_per_min: int = Field(10, ge=1)
    bot_nlu_user_per_day: int = Field(100, ge=1)
    bot_nlu_global_per_day: int = Field(1000, ge=1)
    bot_nlu_max_rounds: int = Field(3, ge=1, le=3)

    @field_validator("telegram_bot_token", "bot_api_password")
    @classmethod
    def _not_placeholder(cls, v: SecretStr) -> SecretStr:
        raw = v.get_secret_value()
        if not raw or raw.startswith("CHANGE_ME"):
            raise ValueError("must be set to a real value")
        return v

    @property
    def nlu_api_key(self) -> str | None:
        raw = self.anthropic_api_key.get_secret_value().strip() if self.anthropic_api_key else ""
        return raw if raw and not raw.startswith("CHANGE_ME") else None

    @property
    def admin_ids(self) -> set[int]:
        return {int(x) for x in self.telegram_admin_ids.replace(" ", "").split(",") if x}

    def _sibling(self, explicit: str, name: str) -> str | None:
        if explicit:
            return explicit
        return str(Path(self.bot_state_file).with_name(name)) if self.bot_state_file else None

    @property
    def seen_path(self) -> str | None:
        return self._sibling(self.bot_seen_file, "seen_alerts.json")

    @property
    def audit_path(self) -> str | None:
        return self._sibling(self.bot_audit_file, "audit.jsonl")
