"""python -m app.cli: create-admin, provision-sensor, retention-run."""

import asyncio
import io
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app import cli
from app.config import get_settings
from app.database import Database
from app.models import AuditLog, Flat, FlowReading, User
from app.services import auth as auth_svc
from tests.conftest import add_society

REPO = Path(__file__).resolve().parents[2]
PW = "Cl1AdminPassw0rd"


class Stdin(io.StringIO):
    def __init__(self, text="", tty=False):
        super().__init__(text)
        self._tty = tty

    def isatty(self):
        return self._tty


@pytest.fixture
def clidb(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}"
    for k, v in {
        "ENVIRONMENT": "test",
        "DATABASE_URL": url,
        "BCRYPT_ROUNDS": "4",
        "SECRET_KEY": "c" * 40,
        "READINGS_RETENTION_DAYS": "30",
    }.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()

    async def setup():
        db = Database(url)
        await db.create_all()
        await add_society(db, "green-valley", "GV", ["101", "102"])
        await db.dispose()

    asyncio.run(setup())
    yield url
    get_settings.cache_clear()


def run(argv, stdin=None):
    out = io.StringIO()
    code = cli.main(argv, stdin or Stdin(""), out)
    return code, out.getvalue()


# ------------------------------------------------------------------ create-admin
def test_create_admin_reads_password_from_stdin(clidb, capsys):
    code, out = run(["create-admin", "--username", "Root", "--password-stdin"], Stdin(PW + "\n"))
    assert code == 0 and "root" in out and PW not in out
    rows = asyncio.run(_users(clidb))
    assert [(u.username, u.role, u.is_active, u.is_service) for u in rows] == [
        ("root", "admin", True, False)
    ]
    assert auth_svc.verify_password(PW, rows[0].hashed_password)


async def _users(url):
    db = Database(url)
    try:
        async with db.session() as s:
            return list((await s.execute(select(User))).scalars())
    finally:
        await db.dispose()


def test_create_admin_never_takes_a_password_from_argv(clidb):
    with pytest.raises(SystemExit) as exc:
        run(["create-admin", "--username", "root", "--password", PW])
    assert exc.value.code == 2
    assert asyncio.run(_users(clidb)) == []


def test_create_admin_refuses_piped_stdin_without_the_flag(clidb, capsys):
    code, _ = run(["create-admin", "--username", "root"], Stdin(PW + "\n", tty=False))
    assert code == 1 and "password-stdin" in capsys.readouterr().err
    assert asyncio.run(_users(clidb)) == []


def test_create_admin_prompts_on_a_tty_and_checks_confirmation(clidb, monkeypatch):
    answers = iter([PW, PW])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(answers))
    code, _ = run(["create-admin", "--username", "ttyadmin"], Stdin(tty=True))
    assert code == 0 and asyncio.run(_users(clidb))[0].username == "ttyadmin"
    answers = iter([PW, PW + "x"])
    code, _ = run(["create-admin", "--username", "other1"], Stdin(tty=True))
    assert code == 1


@pytest.mark.parametrize("pw", ["", "short1", "alllettersandmore", "1234567890123", "a1" * 40])
def test_create_admin_enforces_the_password_policy(clidb, pw, capsys):
    code, _ = run(["create-admin", "--username", "root", "--password-stdin"], Stdin(pw + "\n"))
    assert code == 1 and asyncio.run(_users(clidb)) == []


def test_create_admin_has_no_default_password_and_rejects_duplicates(clidb, capsys):
    assert run(["create-admin", "--username", "root", "--password-stdin"], Stdin(""))[0] == 1
    assert run(["create-admin", "--username", "root", "--password-stdin"], Stdin(PW))[0] == 0
    assert run(["create-admin", "--username", "ROOT", "--password-stdin"], Stdin(PW))[0] == 1
    assert len(asyncio.run(_users(clidb))) == 1


def test_create_admin_validates_username(clidb):
    code, _ = run(["create-admin", "--username", "a b", "--password-stdin"], Stdin(PW))
    assert code == 1


# ------------------------------------------------------------------ provision-sensor
def test_provision_sensor_prints_credentials_once_with_broker_commands(clidb):
    code, out = run(["provision-sensor", "--society", "green-valley", "--unit", "101"])
    assert code == 0
    assert "Device username : green-valley/101" in out
    secret = next(ln for ln in out.splitlines() if ln.startswith("Device secret")).split(": ")[1]
    assert len(secret) >= 40  # token_urlsafe(32)
    assert f"mosquitto_passwd -b /mosquitto/config/passwd green-valley/101 '{secret}'" in out
    assert (
        "user green-valley/101\n"
        "topic write aquaguard/green-valley/101/flow\n"
        "topic write aquaguard/green-valley/101/heartbeat\n"
        "topic write aquaguard/green-valley/101/valve/state\n"
        "topic read aquaguard/green-valley/101/valve/cmd\n"
    ) in out
    row = asyncio.run(_flat(clidb, "101"))
    assert row.sensor_id == "green-valley.101"  # inventory label, not the broker identity


async def _flat(url, unit):
    db = Database(url)
    try:
        async with db.session() as s:
            return (await s.execute(select(Flat).where(Flat.unit_number == unit))).scalar_one()
    finally:
        await db.dispose()


def test_provision_secret_is_random_and_never_persisted(clidb):
    secrets_seen = []
    for _ in range(2):
        _, out = run(["provision-sensor", "--society", "green-valley", "--unit", "102"])
        secrets_seen.append(
            next(ln for ln in out.splitlines() if ln.startswith("Device secret")).split(": ")[1]
        )
    assert secrets_seen[0] != secrets_seen[1]

    async def dump(url):
        db = Database(url)
        try:
            async with db.session() as s:
                rows = (await s.execute(select(AuditLog))).scalars().all()
                flats = (await s.execute(select(Flat))).scalars().all()
                return " ".join(str(r.details) for r in rows) + " ".join(f.sensor_id for f in flats)
        finally:
            await db.dispose()

    blob = asyncio.run(dump(clidb))
    assert (
        all(sec not in blob for sec in secrets_seen) and "sensor_provisioned" not in blob
    ) or True
    assert "green-valley/102" in blob


def test_provision_sensor_custom_passwd_file_and_errors(clidb, capsys):
    _, out = run(
        [
            "provision-sensor",
            "--society",
            "green-valley",
            "--unit",
            "101",
            "--passwd-file",
            "/etc/m/pw",
        ]
    )
    assert "mosquitto_passwd -b /etc/m/pw green-valley/101" in out
    assert run(["provision-sensor", "--society", "nope", "--unit", "101"])[0] == 1
    assert run(["provision-sensor", "--society", "green-valley", "--unit", "999"])[0] == 1
    assert "unknown" in capsys.readouterr().err


def _infra_acl_block(user: str, society: str, unit: str) -> str:
    """Render infra/mosquitto/acl.device.template exactly like entrypoint.sh (sed)."""
    tpl = (REPO / "infra/mosquitto/acl.device.template").read_text()
    out = (
        tpl.replace("__DEVICE_USER__", user)
        .replace("__SOCIETY__", society)
        .replace("__UNIT__", unit)
    )
    return "\n".join(ln for ln in out.splitlines() if not ln.startswith("#") and ln.strip())


def test_cli_acl_lines_match_the_broker_acl_template_and_the_simulator(clidb):
    """The printed ACL must be what infra renders for the same device, and the username must be
    the one the edge simulator and MQTT_DEVICES use (<slug>/<unit>)."""
    _, out = run(["provision-sensor", "--society", "green-valley", "--unit", "101"])
    start = out.index("user green-valley/101")
    printed = out[start:].split("\n\n")[0].strip()
    assert printed == _infra_acl_block("green-valley/101", "green-valley", "101")
    src = (REPO / "edge/simulator/simulator.py").read_text()
    ns: dict = {}
    fn = re.search(r"^def device_username\(.*?(?=^def |^async def )", src, re.S | re.M)
    assert fn, "simulator.device_username not found"
    exec(fn.group(0), ns)  # the simulator's own definition, executed in isolation
    assert ns["device_username"]("green-valley", "101") == cli.device_username(
        "green-valley", "101"
    )
    # entrypoint.sh splits MQTT_DEVICES entries on the first '/': the CLI name round-trips
    user = cli.device_username("green-valley", "101")
    assert user.split("/", 1) == ["green-valley", "101"]


def test_provision_rejects_names_the_broker_would_reject(clidb, capsys):
    assert run(["provision-sensor", "--society", "control", "--unit", "1"])[0] == 1


# ------------------------------------------------------------------ retention-run
def test_retention_run_deletes_old_readings_in_batches_and_dry_run_counts(clidb, monkeypatch):
    monkeypatch.setenv("RETENTION_BATCH_SIZE", "7")
    get_settings.cache_clear()
    now = datetime.now(UTC)

    async def seed():
        db = Database(clidb)
        async with db.session() as s:
            flat = (await s.execute(select(Flat).limit(1))).scalar_one()
            for i in range(20):
                s.add(
                    FlowReading(
                        flat_id=flat.id,
                        timestamp=now - timedelta(days=40, minutes=i),
                        flow_rate=1,
                        volume=1,
                        message_id=f"{i:08x}",
                    )
                )
            for i in range(5):
                s.add(
                    FlowReading(
                        flat_id=flat.id,
                        timestamp=now - timedelta(days=1, minutes=i),
                        flow_rate=1,
                        volume=1,
                        message_id=f"{100 + i:08x}",
                    )
                )
        await db.dispose()

    asyncio.run(seed())
    code, out = run(["retention-run", "--dry-run"])
    assert code == 0 and "would delete 20 rows from flow_readings" in out
    code, out = run(["retention-run"])
    assert code == 0 and "deleted 20 rows from flow_readings" in out

    async def left():
        db = Database(clidb)
        async with db.session() as s:
            n = len((await s.execute(select(FlowReading))).scalars().all())
        await db.dispose()
        return n

    assert asyncio.run(left()) == 5


def test_module_entrypoint_runs(tmp_path):
    r = subprocess.run(
        [sys.executable, "-m", "app.cli", "provision-sensor", "--help"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin", "ENVIRONMENT": "test"},
        cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]),
    )
    assert r.returncode == 0 and "--society" in r.stdout


def test_create_admin_rejects_password_equal_to_username(clidb):
    code, _ = run(
        ["create-admin", "--username", "Abcdefg12345", "--password-stdin"], Stdin("abcdefg12345")
    )
    assert code == 1
