"""Deployment configuration checks (stdlib only).

    python infra/scripts/check_env.py [--env .env] [--dev]   validate a real .env (make check-env)
    python infra/scripts/check_env.py --lint           keep .env.example, compose files and the
                                                       services' config modules in sync (CI)

``--env`` fails closed on placeholders / weak secrets, mirroring (and extending) what the
services themselves enforce. ``--lint`` fails when

* a ``${VAR}`` used by a compose file is not documented in ``.env.example``,
* a variable in ``.env.example`` is used by nothing in the repo's deployment files,
* an environment key passed to backend/bot/dashboard is not read by that service's config.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILES = sorted(ROOT.glob("docker-compose*.yml"))
USAGE_GLOBS = [
    "docker-compose*.yml",
    "Makefile",
    "infra/**/*.sh",
    "infra/**/*.py",
    "infra/**/*.sql",
    "infra/**/Caddyfile",
    "infra/**/Dockerfile*",
    ".github/workflows/*.yml",
]
# variables read by tools that are not services in compose (documented in .env.example anyway)
EXTRA_USED = {"TELEGRAM_ADMIN_IDS"}  # read by the bot through compose; kept for explicitness

SECRETS_ALWAYS = {
    "SECRET_KEY": 32,
    "POSTGRES_PASSWORD": 16,
    "DB_OWNER_PASSWORD": 16,
    "DB_APP_PASSWORD": 16,
    "DB_BACKUP_PASSWORD": 16,
    "MQTT_PASSWORD": 12,
}
URL_SAFE = re.compile(r"^[A-Za-z0-9_.~-]+$")


def parse_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip("'\"")
    return out


def is_placeholder(v: str) -> bool:
    low = v.lower()
    return not v or "change_me" in low or "changeme" in low or low in {"password", "secret"}


def validate(env: dict[str, str], dev: bool = False) -> list[str]:
    errs: list[str] = []
    for name, min_len in SECRETS_ALWAYS.items():
        v = env.get(name, "")
        if is_placeholder(v):
            errs.append(f"{name} is unset or a placeholder")
        elif len(v) < min_len:
            errs.append(f"{name} must be at least {min_len} characters")
    for name in ("POSTGRES_PASSWORD", "DB_OWNER_PASSWORD", "DB_APP_PASSWORD", "DB_BACKUP_PASSWORD"):
        v = env.get(name, "")
        if v and not URL_SAFE.match(v):
            errs.append(f"{name} may only contain [A-Za-z0-9_.~-] (it is embedded in a URL)")
    key = env.get("SECRET_KEY", "")
    if key and len(set(key)) < 16:
        errs.append("SECRET_KEY needs at least 16 distinct characters")
    domain = env.get("PUBLIC_DOMAIN", "")
    if not domain or "/" in domain or ":" in domain or domain.startswith("http"):
        errs.append("PUBLIC_DOMAIN must be a bare host name (no scheme, port or path)")
    tls = env.get("CADDY_TLS", "internal")
    if tls != "internal" and "@" not in tls:
        errs.append("CADDY_TLS must be 'internal' or an e-mail address for ACME")
    if tls != "internal" and domain in {"localhost", "aquaguard.example.com"}:
        errs.append("ACME needs a real, publicly resolvable PUBLIC_DOMAIN")
    if env.get("FORWARDED_ALLOW_IPS", "").strip() == "*":
        errs.append("FORWARDED_ALLOW_IPS must list the proxy address, never '*'")
    if (
        env.get("METRICS_ENABLED", "false").lower() == "true"
        and len(env.get("METRICS_TOKEN", "")) < 24
    ):
        errs.append("METRICS_ENABLED=true needs METRICS_TOKEN of at least 24 characters")
    if env.get("MQTT_DEVICES", "").strip() and len(env.get("MQTT_DEVICE_SECRET", "")) < 32:
        errs.append("MQTT_DEVICES needs MQTT_DEVICE_SECRET of at least 32 characters")
    if env.get("MQTT_DEVICES", "").strip() and is_placeholder(env.get("MQTT_DEVICE_SECRET", "")):
        errs.append("MQTT_DEVICE_SECRET is a placeholder")
    try:
        if float(env.get("LLM_TIMEOUT_S", "4")) > 4:
            errs.append("LLM_TIMEOUT_S must be <= 4 (backend limit); the bot has BOT_LLM_TIMEOUT_S")
    except ValueError:
        errs.append("LLM_TIMEOUT_S must be a number")
    if env.get("LLM_ENABLED", "false").lower() == "true" and is_placeholder(
        env.get("ANTHROPIC_API_KEY", "")
    ):
        errs.append("LLM_ENABLED=true needs ANTHROPIC_API_KEY")
    if not dev and not (env.get("MQTT_TLS_CERT_FILE") and env.get("MQTT_TLS_KEY_FILE")):
        errs.append(
            "MQTT_TLS_CERT_FILE/MQTT_TLS_KEY_FILE unset: devices would have no TLS listener "
            "(see .env.example)"
        )
    profiles = {p.strip() for p in env.get("COMPOSE_PROFILES", "").split(",")}
    if "bot" in profiles:
        for name in ("TELEGRAM_BOT_TOKEN", "BOT_API_PASSWORD"):
            if is_placeholder(env.get(name, "")):
                errs.append(f"{name} is required by profile 'bot'")
    if "backup" in profiles and not env.get("BACKUP_AGE_RECIPIENT", "").startswith("age1"):
        errs.append("profile 'backup' needs BACKUP_AGE_RECIPIENT (an age1... public key)")
    return errs


# ----------------------------------------------------------------------------- lint
def compose_vars() -> set[str]:
    found: set[str] = set()
    for f in COMPOSE_FILES:
        found |= set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)[}:?-]", f.read_text()))
    return found


def example_vars() -> set[str]:
    names: set[str] = set()
    for line in (ROOT / ".env.example").read_text().splitlines():
        m = re.match(r"^#?\s*([A-Z][A-Z0-9_]*)=", line)
        if m:
            names.add(m.group(1))
    return names


def used_anywhere(name: str) -> bool:
    for pattern in USAGE_GLOBS:
        for p in ROOT.glob(pattern):
            if p.is_file() and re.search(rf"\b{name}\b", p.read_text(errors="ignore")):
                return True
    return False


def service_env_keys(service: str) -> set[str]:
    keys: set[str] = set()
    for f in COMPOSE_FILES:
        text = f.read_text()
        m = re.search(rf"^  {service}:\n(.*?)(?=^  [a-z-]+:\n|^[a-z]+:\n|\Z)", text, re.S | re.M)
        if not m:
            continue
        env = re.search(r"^    environment:\n((?:      .*\n|\s*\n)+)", m.group(1), re.M)
        if env:
            keys |= set(re.findall(r"^      ([A-Z][A-Z0-9_]*):", env.group(1), re.M))
    return keys


def config_names(paths: list[str]) -> set[str]:
    names: set[str] = set()
    for rel in paths:
        text = (ROOT / rel).read_text()
        names |= {n.upper() for n in re.findall(r"^    ([a-z][a-z0-9_]*)\s*:", text, re.M)}
        names |= set(re.findall(r'"([A-Z][A-Z0-9_]{2,})"', text))
        names |= {t for t in re.findall(r"'([A-Z][A-Z0-9_]{2,})'", text)}
    return names


def lint() -> list[str]:
    errs: list[str] = []
    documented, used = example_vars(), compose_vars()
    for n in sorted(used - documented):
        errs.append(f"{n}: used by a compose file but missing from .env.example")
    for n in sorted(documented):
        if n not in used and n not in EXTRA_USED and not used_anywhere(n):
            errs.append(f"{n}: documented in .env.example but not used anywhere")
    checks = {
        "backend": ["backend/app/config.py", "backend/app/services/llm/settings.py"],
        "bot": ["bot/bot/config.py"],
        "dashboard": ["dashboard/src/lib/serverConfig.ts"],
        "db-migrate": ["backend/alembic/env.py"],
    }
    # variables that are consumed by the image/entrypoint rather than a settings class
    infra_known = {"PORT", "WEB_CONCURRENCY", "FORWARDED_ALLOW_IPS", "NODE_ENV", "HOSTNAME"}
    for svc, paths in checks.items():
        known = config_names(paths) | infra_known
        for key in sorted(service_env_keys(svc) - known):
            errs.append(f"{svc}: environment key {key} is not read by {', '.join(paths)}")
    return errs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--env", default=str(ROOT / ".env"))
    ap.add_argument("--lint", action="store_true")
    ap.add_argument("--dev", action="store_true", help="development stack: TLS settings optional")
    args = ap.parse_args()
    if args.lint:
        errs = lint()
        label = "env/compose lint"
    else:
        path = Path(args.env)
        if not path.is_file():
            print(f"{path} not found - run `make env` first", file=sys.stderr)
            return 1
        errs = validate(parse_env(path), dev=args.dev)
        label = f"{path.name} validation"
    for e in errs:
        print(f"  - {e}", file=sys.stderr)
    if errs:
        print(f"{label} FAILED ({len(errs)} problem(s))", file=sys.stderr)
        return 1
    print(f"{label} ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
