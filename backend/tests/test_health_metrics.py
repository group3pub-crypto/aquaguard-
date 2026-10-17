"""Health / readiness / metrics, production wiring and static deployment checks."""

import re
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import text

from app.database import Database
from app.main import create_app
from app.migrations import head_revision
from tests.conftest import make_settings

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
TOKEN = "m3trics-t0ken-" + "x" * 20


# ------------------------------------------------------------------ /health
async def test_public_health_discloses_nothing_but_the_status(client, rt):
    rt.counters["flow_failed"] = 5
    rt.dead_letter("aquaguard/secret-society/12/flow", "bad")
    r = await client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert "secret-society" not in r.text and "dead" not in r.text


async def test_health_is_degraded_when_the_database_is_down(client, db):
    async def down():
        return False

    db.ping = down  # type: ignore[method-assign]
    assert (await client.get("/health")).json() == {"status": "degraded"}


async def test_health_is_degraded_when_a_required_broker_is_disconnected(tmp_path, db):
    app = create_app(make_settings(tmp_path, mqtt_required=True), db)
    app.state.rt.publisher.connected = False
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/health")).json() == {"status": "degraded"}
        assert (await c.get("/health/ready")).status_code == 503


# ------------------------------------------------------------------ /health/ready
async def test_ready_ok_at_alembic_head(client):
    r = await client.get("/health/ready")
    assert r.status_code == 200 and r.json() == {"status": "ready"}


async def test_ready_503_when_schema_is_behind_head(client, db):
    async with db.engine.begin() as conn:
        await conn.execute(text("UPDATE alembic_version SET version_num = '0001'"))
    r = await client.get("/health/ready")
    assert r.status_code == 503 and r.json() == {"status": "unavailable"}


async def test_ready_503_when_migrations_never_ran(tmp_path):
    db = Database("sqlite+aiosqlite://")
    await db.create_all()  # tables but no alembic_version
    app = create_app(make_settings(tmp_path), db)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/health/ready")).status_code == 503
    await db.dispose()


async def test_ready_requires_mqtt_only_when_configured(tmp_path, db):
    for required, connected, code in [
        (True, False, 503),
        (True, True, 200),
        (False, False, 200),
    ]:
        app = create_app(make_settings(tmp_path, mqtt_required=required), db)
        app.state.rt.publisher.connected = connected
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            assert (await c.get("/health/ready")).status_code == code, (required, connected)


async def test_ready_503_when_db_down(client, db):
    async def down():
        return False

    db.ping = down  # type: ignore[method-assign]
    assert (await client.get("/health/ready")).status_code == 503


# ------------------------------------------------------------------ /health/details
async def test_details_has_the_operational_picture_for_admins(client, rt, world):
    rt.counters["flow_ok"] = 3
    rt.dead_letter("aquaguard/x/1/flow", "invalid_payload:flow")
    r = await client.get("/health/details", headers=world.admin)
    body = r.json()
    assert r.status_code == 200
    assert body["db"] == "ok" and body["mqtt"] == "disabled"
    assert body["migration"] == {"current": head_revision(), "head": head_revision()}
    assert body["ingest"]["flow_ok"] == 3 and body["dead_letters"]["count"] == 1
    assert body["dead_letters"]["recent"][0]["reason"] == "invalid_payload:flow"
    assert isinstance(body["uptime_s"], int) and body["uptime_s"] >= 0
    assert "models" in body and "maintenance_leader" in body and "ws_connections" in body


# ------------------------------------------------------------------ /metrics
@pytest.fixture
def metrics_app(tmp_path, db):
    return create_app(make_settings(tmp_path, metrics_enabled=True, metrics_token=TOKEN), db)


async def test_metrics_disabled_by_default_is_404(client):
    assert (await client.get("/metrics")).status_code == 404
    assert (
        await client.get("/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
    ).status_code == 404


async def test_metrics_requires_the_static_bearer_token(metrics_app):
    async with httpx.AsyncClient(
        transport=ASGITransport(app=metrics_app), base_url="http://t"
    ) as c:
        assert (await c.get("/metrics")).status_code == 401
        for bad in (
            "Bearer wrong",
            f"Bearer {TOKEN}x",
            f"Bearer {TOKEN[:-1]}",
            "Basic abc",
            TOKEN,
            "Bearer ",
        ):
            r = await c.get("/metrics", headers={"Authorization": bad})
            assert r.status_code == 401, bad
        ok = await c.get("/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
        assert ok.status_code == 200 and ok.headers["content-type"].startswith("text/plain")


async def test_metrics_token_is_compared_in_constant_time(monkeypatch):
    import hmac

    from app.routers import health

    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(health.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    app = create_app(
        make_settings(
            __import__("pathlib").Path("/tmp/aq-m"), metrics_enabled=True, metrics_token=TOKEN
        ),
        Database("sqlite+aiosqlite://"),
    )
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.get("/metrics", headers={"Authorization": "Bearer nope"})
    assert calls


async def test_metrics_exposes_the_required_series(metrics_app):
    rt = metrics_app.state.rt
    rt.counters["flow_ok"] = 7
    rt.counters["flow_failed"] = 2
    rt.is_leader = True
    from app import metrics

    metrics.DETECTION_SECONDS.observe(0.02)
    metrics.ALERTS_TOTAL.labels(tier="3").inc()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=metrics_app), base_url="http://t"
    ) as c:
        text_ = (await c.get("/metrics", headers={"Authorization": f"Bearer {TOKEN}"})).text
    assert 'ingest_messages_total{result="flow_ok"} 7.0' in text_
    assert 'ingest_messages_total{result="flow_failed"} 2.0' in text_
    assert "detection_seconds_bucket" in text_ and "detection_seconds_count" in text_
    assert re.search(r'alerts_total\{tier="3"\} [1-9]', text_)
    for tier in "1234":
        assert f'alerts_total{{tier="{tier}"}}' in text_
    assert "ws_connections 0.0" in text_
    assert "mqtt_connected 0.0" in text_
    assert "maintenance_leader 1.0" in text_


async def test_metrics_follow_live_state(metrics_app):
    rt = metrics_app.state.rt
    rt.publisher.connected = True
    async with httpx.AsyncClient(
        transport=ASGITransport(app=metrics_app), base_url="http://t"
    ) as c:
        h = {"Authorization": f"Bearer {TOKEN}"}
        assert "mqtt_connected 1.0" in (await c.get("/metrics", headers=h)).text
        rt.publisher.connected = False
        assert "mqtt_connected 0.0" in (await c.get("/metrics", headers=h)).text


async def test_detection_is_timed_by_the_histogram(db, rt, world):
    from app import metrics
    from app.models import Flat
    from app.services import detector

    before = metrics.DETECTION_SECONDS._sum.get()  # type: ignore[attr-defined]
    count_before = sum(b.get() for b in metrics.DETECTION_SECONDS._buckets)  # type: ignore[attr-defined]
    async with db.session() as s:
        flat = await s.get(Flat, world.flats[0].id)
        await detector.run_detection(s, rt.models, flat)
    assert sum(b.get() for b in metrics.DETECTION_SECONDS._buckets) == count_before + 1  # type: ignore[attr-defined]
    assert metrics.DETECTION_SECONDS._sum.get() >= before  # type: ignore[attr-defined]


async def test_creating_many_apps_does_not_duplicate_the_instrumentation(tmp_path):
    from app.services import detector

    for _ in range(3):
        create_app(make_settings(tmp_path), Database("sqlite+aiosqlite://"))
    inner = detector.run_detection
    depth = 0
    while hasattr(inner, "__wrapped__"):
        inner, depth = inner.__wrapped__, depth + 1
    assert depth == 1


# ------------------------------------------------------------------ static deployment checks
def test_dockerfile_copy_paths_exist_relative_to_the_repo_root():
    text_ = (BACKEND / "Dockerfile").read_text()
    assert "docker build -f backend/Dockerfile ." in text_
    for src in re.findall(r"^COPY\s+(\S+)\s+\S+", text_, re.M):
        assert (REPO / src).exists(), f"COPY source {src!r} missing relative to the repo root"
    assert (REPO / "libs/aquaguard_features/pyproject.toml").exists()
    assert "COPY backend/pyproject.toml" in text_ and "COPY backend/ ." in text_


def test_dockerfile_healthcheck_uses_PORT_and_the_ready_endpoint():
    text_ = (BACKEND / "Dockerfile").read_text()
    hc = text_[text_.index("HEALTHCHECK") :].split("ENTRYPOINT")[0]
    assert "os.environ.get('PORT'" in hc and "/health/ready" in hc and "8000'" in hc
    assert "ENV" in text_ and "PORT=8000" in text_ and "USER aquaguard" in text_


def test_build_context_ignore_file_keeps_secrets_and_tests_out():
    ignore = (BACKEND / "Dockerfile.dockerignore").read_text().split()
    assert "*" in ignore and "!backend" in ignore and "!libs/aquaguard_features" in ignore
    assert "**/.env" in ignore and "backend/tests" in ignore


def test_entrypoint_uses_PORT_workers_and_never_trusts_all_proxies():
    sh = (BACKEND / "docker-entrypoint.sh").read_text()
    assert '--port "$PORT"' in sh and '--workers "$WEB_CONCURRENCY"' in sh
    assert '--forwarded-allow-ips "$FORWARDED_ALLOW_IPS"' in sh
    assert "FORWARDED_ALLOW_IPS:-127.0.0.1" in sh and "forwarded-allow-ips '*'" not in sh
    assert "exec uvicorn" in sh and "alembic upgrade head" in sh
    assert "--proxy-headers" in sh and "--timeout-graceful-shutdown" in sh


def test_entrypoint_refuses_wildcard_proxies(tmp_path):
    import subprocess

    r = subprocess.run(
        ["sh", str(BACKEND / "docker-entrypoint.sh")],
        env={"PATH": "/usr/bin:/bin", "FORWARDED_ALLOW_IPS": "*"},
        capture_output=True,
        text=True,
    )
    assert r.returncode == 1 and "not '*'" in r.stderr


def test_no_demo_functionality_remains_in_the_backend():
    banned = re.compile(r"\bdemo\b|demo_|_demo|inject-fault|control/fault|control/speed", re.I)
    offenders = []
    files = [
        BACKEND / n for n in ("Dockerfile", "docker-entrypoint.sh", "README.md", "pyproject.toml")
    ]
    # app/mqtt/ and ingest.py belong to the ingest owner (leftover wording is reported to them)
    for path in [
        *(
            p
            for p in BACKEND.glob("app/**/*.py")
            if "mqtt" not in p.parts and p.name != "ingest.py"
        ),
        *files,
    ]:
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if banned.search(line):
                offenders.append(f"{path.relative_to(BACKEND)}:{n}: {line.strip()[:80]}")
    assert not offenders, offenders
    assert not (BACKEND / "app/seed.py").exists() and not (BACKEND / "app/routers/demo.py").exists()
    from app.runtime import Runtime

    assert not hasattr(Runtime, "demo") and "demo" not in Runtime.__dataclass_fields__


def test_dev_bootstrap_script_uses_the_cli_and_never_puts_a_password_in_argv():
    sh = (BACKEND / "scripts/dev_bootstrap.sh").read_text()
    assert "app.cli create-admin" in sh and "--password-stdin" in sh
    assert "--password " not in sh and "provision-sensor" in sh
    assert (BACKEND / "scripts/dev_bootstrap.sh").stat().st_mode & 0o111


def test_migration_0003_is_head_and_chains_from_0001():
    assert head_revision() == "0003"
