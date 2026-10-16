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

    @field_validator("telegram_bot_token", "bot_api_password")
    @classmethod
    def _not_placeholder(cls, v: SecretStr) -> SecretStr:
        raw = v.get_secret_value()
        if not raw or raw.startswith("CHANGE_ME"):
            raise ValueError("must be set to a real value")
        return v

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
