"""Operations CLI: ``python -m app.cli <command>``.

    create-admin      create the first (or another) admin; password from a TTY prompt or stdin
    provision-sensor  generate a device secret for a flat and print the broker commands
    retention-run     apply READINGS_/AUDIT_RETENTION_DAYS once (cron-able)

Passwords are NEVER accepted on the command line (they would land in shell history and `ps`).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import re
import secrets
import sys
from typing import TextIO

from app.config import get_settings
from app.database import Database
from app.passwords import PasswordPolicyError, validate_password
from app.repositories import AuditRepository, FlatRepository, SocietyRepository
from app.services import auth as auth_svc
from app.services.retention import run_retention

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,50}$")
DEFAULT_PASSWD_FILE = "/mosquitto/config/passwd"


class CliError(Exception):
    pass


def read_password(stdin: TextIO, *, from_stdin: bool) -> str:
    """TTY: prompt twice. Piped: only with --password-stdin (first line)."""
    if from_stdin:
        pw = stdin.readline().rstrip("\r\n")
        if not pw:
            raise CliError("no password received on stdin")
        return pw
    if not stdin.isatty():
        raise CliError("not a terminal: pipe the password and pass --password-stdin")
    first = getpass.getpass("Password: ")
    if getpass.getpass("Repeat password: ") != first:
        raise CliError("passwords do not match")
    return first


async def create_admin(db: Database, settings, username: str, name: str, password: str) -> str:
    if not USERNAME_RE.match(username):
        raise CliError("username: 3-50 chars of letters, digits, '_', '.', '-'")
    try:
        validate_password(password, username)
    except PasswordPolicyError as exc:
        raise CliError(str(exc)) from None
    async with db.session() as session:
        user = await auth_svc.create_user(
            session, settings, username=username, password=password, name=name, role="admin"
        )
        if user is None:
            raise CliError(f"user {username.lower()!r} already exists")
        await AuditRepository(session).record(
            entity_type="user", entity_id=user.id, action="admin_created_by_cli", actor="cli"
        )
        return user.username


def broker_instructions(
    slug: str, unit: str, secret: str, passwd_file: str = DEFAULT_PASSWD_FILE
) -> str:
    user = f"{slug}.{unit}"
    return (
        f"Device username : {user}\n"
        f"Device secret   : {secret}\n"
        "(shown ONCE - it is not stored anywhere; re-run this command to rotate it)\n\n"
        "1) Add the credential on the broker host:\n"
        f"   mosquitto_passwd -b {passwd_file} {user} '{secret}'\n\n"
        "2) Add to the mosquitto ACL file:\n"
        f"user {user}\n"
        f"topic write aquaguard/{slug}/{unit}/flow\n"
        f"topic write aquaguard/{slug}/{unit}/heartbeat\n"
        f"topic write aquaguard/{slug}/{unit}/valve/state\n"
        f"topic read aquaguard/{slug}/{unit}/valve/cmd\n\n"
        "3) Reload mosquitto (SIGHUP) and flash the device with the username and secret.\n"
    )


async def provision_sensor(
    db: Database, slug: str, unit: str, passwd_file: str = DEFAULT_PASSWD_FILE
) -> str:
    async with db.session() as session:
        society = await SocietyRepository(session).get_by_slug(slug)
        if society is None:
            raise CliError(f"unknown society slug {slug!r} (create it via POST /api/v1/societies)")
        flat = await FlatRepository(session).get_by_unit(society.id, unit)
        if flat is None:
            raise CliError(f"unknown unit {unit!r} in {slug!r} (create it via POST /api/v1/flats)")
        username = f"{slug}.{unit}"
        other = await FlatRepository(session).get_by_sensor_id(username)
        if other is not None and other.id != flat.id:
            raise CliError(f"sensor id {username!r} is already used by another flat")
        flat.sensor_id = username
        secret = secrets.token_urlsafe(32)
        await AuditRepository(session).record(
            entity_type="flat",
            entity_id=flat.id,
            action="sensor_provisioned",
            actor="cli",
            details={"device_user": username},  # never the secret
            flat_id=flat.id,
            society_id=society.id,
        )
    return broker_instructions(slug, unit, secret, passwd_file)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("create-admin", help="create an admin user")
    a.add_argument("--username", required=True)
    a.add_argument("--name", default=None, help="display name (default: the username)")
    a.add_argument(
        "--password-stdin", action="store_true", help="read the password from stdin (first line)"
    )
    s = sub.add_parser("provision-sensor", help="issue device credentials for a flat")
    s.add_argument("--society", required=True, help="society slug")
    s.add_argument("--unit", required=True, help="flat unit number")
    s.add_argument("--passwd-file", default=DEFAULT_PASSWD_FILE)
    r = sub.add_parser("retention-run", help="apply the retention policy once")
    r.add_argument("--dry-run", action="store_true", help="only count what would be deleted")
    return p


async def _run(args: argparse.Namespace, stdin: TextIO, out: TextIO) -> int:
    settings = get_settings()
    db = Database(settings.database_url)
    try:
        if args.command == "create-admin":
            pw = read_password(stdin, from_stdin=args.password_stdin)
            name = await create_admin(db, settings, args.username, args.name or args.username, pw)
            print(f"created admin {name!r}", file=out)
        elif args.command == "provision-sensor":
            print(await provision_sensor(db, args.society, args.unit, args.passwd_file), file=out)
        else:
            result = await run_retention(db, settings, dry_run=args.dry_run)
            verb = "would delete" if args.dry_run else "deleted"
            for table, n in result.items():
                print(f"{verb} {n} rows from {table}", file=out)
    finally:
        await db.dispose()
    return 0


def main(
    argv: list[str] | None = None, stdin: TextIO | None = None, out: TextIO | None = None
) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args, stdin or sys.stdin, out or sys.stdout))
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
