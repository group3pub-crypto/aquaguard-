import pytest
from pydantic import ValidationError

from app.config import Settings


def S(**kw):
    return Settings(_env_file=None, **kw)


STRONG = "Q7vLx2NmRt9WcKd4Hs6Bj3YfGp8ZaE5u"
PROD = dict(
    database_url="postgresql+asyncpg://aq:Xk29fj3Lq8vZ1mQp@db.internal:5432/aq",
    allowed_origins="https://dash.example.org",
)


def test_default_environment_is_production_and_fails_closed(monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(ValidationError, match="SECRET_KEY is required"):
        S()


@pytest.mark.parametrize(
    "key",
    [
        "changeme",
        "change-me-in-production",
        "your-secret-key",
        "SECRET",
        "x" * 31,
        "please-changeme-" + "a" * 30,
    ],
)
def test_production_rejects_placeholder_or_short_keys(key):
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        S(environment="production", secret_key=key, **PROD)


def test_production_accepts_strong_key():
    s = S(environment="production", secret_key=STRONG, **PROD)
    assert s.secret_key == STRONG


@pytest.mark.parametrize("env", ["development", "test"])
def test_non_production_generates_random_key_per_instance(env):
    a, b = S(environment=env), S(environment=env)
    assert len(a.secret_key) >= 32 and a.secret_key != b.secret_key


def test_cors_wildcard_rejected():
    with pytest.raises(ValidationError, match="ALLOWED_ORIGINS"):
        S(environment="test", allowed_origins="*")


def test_cors_origins_parsed_from_env_string():
    s = S(environment="test", allowed_origins="http://a.test, https://b.test")
    assert s.cors_origins == ["http://a.test", "https://b.test"]


# ------------------------------------------------------------------ production validation matrix
def prod(**over):
    return S(environment="production", **{"secret_key": STRONG, **PROD, **over})


def test_secret_needs_16_distinct_characters():
    with pytest.raises(ValidationError, match="distinct"):
        prod(secret_key="ab12" * 10)
    assert prod(secret_key="abcdefghijklmnop" * 2)  # exactly 16 distinct, 32 long


def test_database_url_required_in_production_no_localhost_default():
    kw = {k: v for k, v in PROD.items() if k != "database_url"}
    with pytest.raises(ValidationError, match="DATABASE_URL is required"):
        S(environment="production", secret_key=STRONG, **kw)


def test_non_production_keeps_a_dev_database_default():
    assert "localhost" in S(environment="development").database_url


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://aquaguard:aquaguard@db:5432/aq",
        "postgresql+asyncpg://postgres:postgres@db:5432/aq",
        "postgresql+asyncpg://aq:CHANGE_ME@db:5432/aq",
        "postgresql+asyncpg://aq:password@db:5432/aq",
        "postgresql+asyncpg://aq@db:5432/aq",
        "postgresql+asyncpg://alice:alice@db:5432/aq",
    ],
)
def test_production_rejects_default_db_credentials(url):
    with pytest.raises(ValidationError, match="database password"):
        prod(database_url=url)


@pytest.mark.parametrize(
    "origins",
    [
        "",
        "*",
        "https://a.test,*",
        "http://dash.example.org",
        "https://dash.example.org/app",
        "dash.test",
    ],
)
def test_production_requires_explicit_https_origins(origins):
    with pytest.raises(ValidationError, match="ALLOWED_ORIGINS"):
        prod(allowed_origins=origins)


def test_production_accepts_several_https_origins():
    s = prod(allowed_origins="https://a.example.org, https://b.example.org:8443")
    assert s.cors_origins == ["https://a.example.org", "https://b.example.org:8443"]


def test_docs_default_off_in_production_on_elsewhere_and_overridable():
    assert prod().docs_enabled is False
    assert prod(enable_docs=True).docs_enabled is True
    assert S(environment="development").docs_enabled is True
    assert S(environment="development", enable_docs=False).docs_enabled is False


def test_model_manifest_required_defaults_to_production_only():
    assert prod().manifest_required is True
    assert S(environment="development").manifest_required is False
    assert prod(model_manifest_required=False).manifest_required is False


def test_metrics_enabled_demands_a_long_token():
    with pytest.raises(ValidationError, match="METRICS_TOKEN"):
        S(environment="test", metrics_enabled=True, metrics_token="short")
    assert S(environment="test", metrics_enabled=True, metrics_token="m" * 32).metrics_enabled
    assert S(environment="test").metrics_enabled is False


def test_forwarded_allow_ips_wildcard_is_refused():
    with pytest.raises(ValidationError, match="FORWARDED_ALLOW_IPS"):
        S(environment="test", forwarded_allow_ips="*")
    assert S(environment="test").forwarded_allow_ips == "127.0.0.1"


def test_safe_defaults():
    s = S(environment="test")
    assert s.access_token_minutes == 15
    assert s.allow_self_registration is False
    assert s.valve_ack_timeout_s == 60 and s.shutoff_warning_seconds == 300
    assert s.readings_retention_days == 90 and s.audit_retention_days == 0
    assert s.active_use_window_min == 60 and s.min_slot_samples == 30
    assert s.login_max_failures == 10 and s.rate_limit_storage_uri == "memory://"


def test_legacy_env_names_are_accepted_as_aliases(monkeypatch):
    monkeypatch.setenv("ACCESS_TOKEN_EXPIRE_MINUTES", "7")
    monkeypatch.setenv("BACKEND_LOG_LEVEL", "debug")
    s = S(environment="test")
    assert s.access_token_minutes == 7 and s.log_level == "debug"
    monkeypatch.setenv("ACCESS_TOKEN_MINUTES", "9")
    assert S(environment="test").access_token_minutes == 9  # canonical name wins


def test_legacy_env_names_log_a_deprecation_warning(monkeypatch):
    from structlog.testing import capture_logs

    monkeypatch.setenv("ACCESS_TOKEN_EXPIRE_MINUTES", "7")
    monkeypatch.delenv("ACCESS_TOKEN_MINUTES", raising=False)
    with capture_logs() as logs:
        S(environment="test")
    assert any(e["event"] == "deprecated_env_var" for e in logs)


def test_memory_rate_limit_storage_with_many_workers_warns_loudly():
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        prod(web_concurrency=4)
    assert any(e["event"] == "rate_limit_memory_storage_with_multiple_workers" for e in logs)
    with capture_logs() as logs:
        prod(web_concurrency=4, rate_limit_storage_uri="redis://r:6379/0")
    assert not logs


def test_demo_settings_are_gone():
    s = S(environment="test", demo_mode=True)  # unknown settings are ignored, not honoured
    assert not hasattr(s, "demo_mode") and not hasattr(s, "seed_demo_data")
