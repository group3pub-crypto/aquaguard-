import pytest
from pydantic import ValidationError

from app.config import Settings


def S(**kw):
    return Settings(_env_file=None, **kw)


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
        S(environment="production", secret_key=key)


def test_production_accepts_strong_key():
    s = S(environment="production", secret_key="k8Zp" * 10)
    assert s.secret_key == "k8Zp" * 10


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


def test_production_demo_requires_explicit_passwords():
    with pytest.raises(ValidationError, match="DEMO_ADMIN_PASSWORD"):
        S(environment="production", secret_key="k8Zp" * 10, demo_mode=True)


def test_dev_demo_passwords_are_random_not_hardcoded():
    a, b = (
        S(environment="development", demo_mode=True),
        S(environment="development", demo_mode=True),
    )
    assert a.demo_admin_password and a.demo_admin_password != b.demo_admin_password
    assert a.demo_admin_password != a.demo_resident_password


def test_demo_passwords_untouched_when_demo_off():
    assert S(environment="test").demo_admin_password == ""
