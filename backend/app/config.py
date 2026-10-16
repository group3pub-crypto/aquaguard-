"""Application settings. Fails closed: production refuses to start without a strong secret."""
from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

import structlog
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = structlog.get_logger(__name__)

PLACEHOLDER_KEYS = {
    "changeme", "change-me", "change_me", "secret", "secret_key", "secretkey", "password",
    "your-secret-key", "your_secret_key", "change-me-in-production", "changeme-in-production",
    "dev-secret", "dev-secret-key", "supersecret", "super-secret", "default", "example", "test",
}
MIN_SECRET_LEN = 32
REPO_ROOT = Path(__file__).resolve().parents[2]


def _is_placeholder(key: str) -> bool:
    low = key.strip().lower()
    return low in PLACEHOLDER_KEYS or any(
        t in low for t in ("changeme", "change-me", "change_me", "placeholder", "your-secret")
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: Literal["development", "production", "test"] = "production"
    app_version: str = "0.1.0"
    log_level: str = "INFO"
    log_json: bool = True

    # security
    secret_key: str = ""
    access_token_minutes: int = Field(60, ge=1)
    bcrypt_rounds: int = Field(12, ge=4, le=16)
    allowed_origins: str = "http://localhost:3000"
    rate_limit_enabled: bool = True
    ws_max_connections: int = Field(200, ge=1)

    # storage
    database_url: str = "postgresql+asyncpg://aquaguard:aquaguard@localhost:5432/aquaguard"

    # mqtt
    mqtt_enabled: bool = True
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    background_tasks_enabled: bool = True

    # ml
    model_dir: Path = REPO_ROOT / "ml" / "models"
    timezone: str = "UTC"
    detect_debounce_seconds: float = 30.0
    dead_letter_size: int = 100
    explainer_backend: Literal["template"] = "template"

    # demo
    demo_mode: bool = False
    seed_demo_data: bool = False
    demo_admin_password: str = ""
    demo_resident_password: str = ""

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @model_validator(mode="after")
    def _check_security(self) -> Settings:
        if "*" in self.cors_origins:
            raise ValueError("ALLOWED_ORIGINS must not contain '*' (credentials are enabled)")
        if not self.secret_key:
            if self.is_production:
                raise ValueError("SECRET_KEY is required in production")
            self.secret_key = secrets.token_urlsafe(48)
            log.warning("secret_key_generated", note="random per-process key; tokens reset on restart")
        elif self.is_production and (_is_placeholder(self.secret_key) or len(self.secret_key) < MIN_SECRET_LEN):
            raise ValueError(
                f"SECRET_KEY must be a non-placeholder value of at least {MIN_SECRET_LEN} characters"
            )
        if self.demo_mode or self.seed_demo_data:
            for attr in ("demo_admin_password", "demo_resident_password"):
                if getattr(self, attr):
                    continue
                if self.is_production:
                    raise ValueError(f"{attr.upper()} must be set when demo mode is on in production")
                generated = secrets.token_urlsafe(12)
                setattr(self, attr, generated)
                log.warning("demo_password_generated", setting=attr, value=generated)
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
