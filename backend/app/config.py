"""Application settings. Fails closed: production refuses to start with weak/missing secrets."""

from __future__ import annotations

import os
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import structlog
from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

log = structlog.get_logger(__name__)

PLACEHOLDER_KEYS = {
    "changeme",
    "change-me",
    "change_me",
    "secret",
    "secret_key",
    "secretkey",
    "password",
    "your-secret-key",
    "your_secret_key",
    "change-me-in-production",
    "changeme-in-production",
    "dev-secret",
    "dev-secret-key",
    "supersecret",
    "super-secret",
    "default",
    "example",
    "test",
}
WEAK_DB_PASSWORDS = {
    "",
    "aquaguard",
    "postgres",
    "password",
    "passwd",
    "admin",
    "root",
    "default",
    "secret",
    "changeme",
    "change_me",
    "change-me",
    "example",
    "test",
    "12345678",
}
MIN_SECRET_LEN = 32
MIN_SECRET_DISTINCT = 16
MIN_METRICS_TOKEN_LEN = 24
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DEV_ORIGIN = "http://localhost:3000"
DEFAULT_DEV_DATABASE_URL = "postgresql+asyncpg://aquaguard:aquaguard@localhost:5432/aquaguard"

# legacy env names -> canonical field (a deprecation warning is logged when only the legacy one
# is set)
LEGACY_ENV = {
    "ACCESS_TOKEN_EXPIRE_MINUTES": "ACCESS_TOKEN_MINUTES",
    "BACKEND_LOG_LEVEL": "LOG_LEVEL",
}


def _is_placeholder(key: str) -> bool:
    low = key.strip().lower()
    return low in PLACEHOLDER_KEYS or any(
        t in low for t in ("changeme", "change-me", "change_me", "placeholder", "your-secret")
    )


def _weak_db_password(user: str | None, password: str | None) -> bool:
    pw = (password or "").strip()
    low = pw.lower()
    return (
        low in WEAK_DB_PASSWORDS
        or low == (user or "").lower()
        or _is_placeholder(pw)
        or "change_me" in low
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    environment: Literal["development", "production", "test"] = "production"
    app_version: str = "0.1.0"
    log_level: str = Field("INFO", validation_alias=AliasChoices("LOG_LEVEL", "BACKEND_LOG_LEVEL"))
    log_json: bool = True
    enable_docs: bool | None = None  # default: off in production, on elsewhere

    # security
    secret_key: str = ""
    access_token_minutes: int = Field(
        15,
        ge=1,
        le=24 * 60,
        validation_alias=AliasChoices("ACCESS_TOKEN_MINUTES", "ACCESS_TOKEN_EXPIRE_MINUTES"),
    )
    refresh_token_days: int = Field(14, ge=1, le=365)
    bcrypt_rounds: int = Field(12, ge=4, le=16)
    allowed_origins: str = ""
    allow_self_registration: bool = False
    login_max_failures: int = Field(10, ge=1)
    login_failure_window_minutes: int = Field(15, ge=1)
    login_lock_minutes: int = Field(15, ge=1)
    rate_limit_enabled: bool = True
    rate_limit_storage_uri: str = "memory://"
    web_concurrency: int = Field(1, ge=1)
    forwarded_allow_ips: str = "127.0.0.1"  # consumed by docker-entrypoint.sh; validated here

    # websocket
    ws_max_connections: int = Field(200, ge=1)
    ws_max_per_user: int = Field(5, ge=1)
    ws_ticket_ttl_s: int = Field(30, ge=5, le=300)
    ws_idle_timeout_s: float = Field(90.0, gt=0)
    ws_ping_interval_s: float = Field(30.0, gt=0)
    ws_revalidate_s: float = Field(60.0, gt=0)

    # storage
    database_url: str = ""

    # mqtt
    mqtt_enabled: bool = True
    mqtt_required: bool = True  # /health/ready needs a broker connection when True
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    background_tasks_enabled: bool = True
    # readings stamped further in the future than this are rejected (clock-skew guard)
    mqtt_max_future_skew_s: float = Field(300.0, ge=0)
    mqtt_client_id: str = "aquaguard-backend"
    # live readings older than this are rejected (stale/replayed); history goes through the
    # admin-only ingest.backfill_readings() path, never through MQTT
    mqtt_max_reading_age_s: float = Field(600.0, ge=1)
    # per-device message budget (token bucket, per minute); 0 disables. Raise it for the
    # accelerated edge simulator (--speed > 1), which emits one reading per simulated minute.
    mqtt_device_rate_per_min: int = Field(120, ge=0)
    mqtt_ca_file: Path | None = None  # set => TLS to the broker, verified against this CA
    mqtt_worker_concurrency: int = Field(16, ge=1, le=256)

    # ml
    model_dir: Path = REPO_ROOT / "ml" / "models"
    model_manifest_required: bool | None = None  # default: required in production
    timezone: str = "UTC"
    # rules = deterministic rules only; ml = RandomForest/IsolationForest (rules if models are
    # missing); hybrid = rules gate + ML class/confidence/veto (rules only if models are missing
    # or fail their manifest check). The default may change after ml/reports/benchmark.md is
    # reviewed on real data.
    detection_policy: Literal["rules", "ml", "hybrid"] = "hybrid"
    detect_debounce_seconds: float = 30.0
    # at most this many detections (CPU-bound ML) run at once; further triggers are shed and
    # retried on the flat's next reading so ingest never queues behind inference
    detect_max_concurrency: int = Field(2, ge=1, le=64)
    dead_letter_size: int = 100

    # alerting / valve safety
    valve_ack_timeout_s: float = Field(60.0, gt=0)
    shutoff_warning_seconds: float = Field(300.0, ge=0)
    active_use_window_min: int = Field(60, ge=15, le=24 * 60)
    min_slot_samples: int = Field(30, ge=1)
    maintenance_interval_s: float = Field(60.0, gt=0)
    shutdown_grace_s: float = Field(20.0, ge=0)

    # retention (0 = keep forever)
    readings_retention_days: int = Field(90, ge=0)
    audit_retention_days: int = Field(0, ge=0)
    retention_batch_size: int = Field(5000, ge=1)

    # observability
    metrics_enabled: bool = False
    metrics_token: str = ""

    @property
    def cors_origins(self) -> list[str]:
        origins = [o.strip() for o in self.allowed_origins.split(",") if o.strip()]
        if not origins and not self.is_production:
            return [DEFAULT_DEV_ORIGIN]
        return origins

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def docs_enabled(self) -> bool:
        return self.enable_docs if self.enable_docs is not None else not self.is_production

    @property
    def manifest_required(self) -> bool:
        if self.model_manifest_required is not None:
            return self.model_manifest_required
        return self.is_production

    @model_validator(mode="after")
    def _check_security(self) -> Settings:
        self._warn_legacy_env()
        origins = self.cors_origins
        if "*" in origins:
            raise ValueError("ALLOWED_ORIGINS must not contain '*' (credentials are enabled)")
        if self.is_production:
            self._check_production(origins)
        if not self.secret_key:
            if self.is_production:
                raise ValueError("SECRET_KEY is required in production")
            self.secret_key = secrets.token_urlsafe(48)
            log.warning(
                "secret_key_generated", note="random per-process key; tokens reset on restart"
            )
        if not self.database_url:
            self.database_url = DEFAULT_DEV_DATABASE_URL  # non-production only (checked above)
        if self.forwarded_allow_ips.strip() == "*":
            raise ValueError("FORWARDED_ALLOW_IPS must list trusted proxy IPs, never '*'")
        if self.metrics_enabled and len(self.metrics_token) < MIN_METRICS_TOKEN_LEN:
            raise ValueError(
                f"METRICS_ENABLED requires METRICS_TOKEN of >= {MIN_METRICS_TOKEN_LEN} characters"
            )
        if (
            self.is_production
            and self.rate_limit_enabled
            and self.rate_limit_storage_uri.startswith("memory:")
            and self.web_concurrency > 1
        ):
            log.warning(
                "rate_limit_memory_storage_with_multiple_workers",
                web_concurrency=self.web_concurrency,
                note="limits are per process; set RATE_LIMIT_STORAGE_URI=redis://... to share",
            )
        return self

    def _check_production(self, origins: list[str]) -> None:
        key = self.secret_key
        if not key:
            raise ValueError("SECRET_KEY is required in production")
        if key:
            if _is_placeholder(key) or len(key) < MIN_SECRET_LEN:
                raise ValueError(
                    f"SECRET_KEY must be non-placeholder and >= {MIN_SECRET_LEN} characters"
                )
            if len(set(key)) < MIN_SECRET_DISTINCT:
                raise ValueError(
                    f"SECRET_KEY must contain >= {MIN_SECRET_DISTINCT} distinct characters"
                )
        if not self.database_url:
            raise ValueError("DATABASE_URL is required in production")
        url = make_url(self.database_url)
        if url.get_backend_name() == "postgresql" and _weak_db_password(url.username, url.password):
            raise ValueError("DATABASE_URL uses a default/placeholder/weak database password")
        if not origins:
            raise ValueError("ALLOWED_ORIGINS is required in production (explicit https origins)")
        for origin in origins:
            p = urlparse(origin)
            if p.scheme != "https" or not p.netloc or p.path not in ("", "/") or p.query:
                raise ValueError(f"ALLOWED_ORIGINS entry must be an https origin: {origin!r}")

    @staticmethod
    def _warn_legacy_env() -> None:
        for legacy, canonical in LEGACY_ENV.items():
            if os.environ.get(legacy) and not os.environ.get(canonical):
                log.warning("deprecated_env_var", legacy=legacy, use=canonical)


@lru_cache
def get_settings() -> Settings:
    return Settings()
