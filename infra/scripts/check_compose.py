"""Static checks of the deployment files (no Docker daemon needed, only the `docker compose` CLI).

    python infra/scripts/check_compose.py

* every overlay combination renders with `docker compose config` against a throw-away .env made
  from .env.example (skipped with a notice if the docker CLI is missing),
* production policy: no stray published ports, no floating tags, hardening flags, memory limits,
  no simulator, no DEMO_* settings, no auto-applied override file,
* every build context / Dockerfile / COPY source / relative bind mount exists,
* every FROM in a Dockerfile is pinned to a non-`latest` tag (ARG defaults are resolved).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLACEHOLDER = "CHANGE_ME"
SETS = {
    "prod": ["docker-compose.yml", "docker-compose.prod.yml"],
    "dev": ["docker-compose.yml", "docker-compose.dev.yml"],
    "prod+timescale": [
        "docker-compose.yml",
        "docker-compose.prod.yml",
        "docker-compose.timescale.yml",
    ],
    "prod+monitoring": [
        "docker-compose.yml",
        "docker-compose.prod.yml",
        "docker-compose.monitoring.yml",
    ],
}
PROD_PUBLISHED = {("proxy", 80), ("proxy", 443), ("mosquitto", 8883)}
HARDENING_EXEMPT_READ_ONLY = {"prometheus", "alertmanager"}  # monitoring images write to volumes


def render(files: list[str], env_file: Path) -> dict:
    cmd = ["docker", "compose", "--env-file", str(env_file)]
    for f in files:
        cmd += ["-f", str(ROOT / f)]
    cmd += ["--profile", "*", "config", "--format", "json"]
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, check=False)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip())
    return json.loads(res.stdout)


def make_env(dest: Path) -> None:
    lines = []
    for i, line in enumerate((ROOT / ".env.example").read_text().splitlines()):
        if re.match(r"^[A-Z][A-Z0-9_]*=" + PLACEHOLDER + "$", line):
            line = line.replace(PLACEHOLDER, f"ci-only-placeholder-value-{i:04d}-0123456789abcdef")
        lines.append(line)
    dest.write_text("\n".join(lines) + "\n")


def check_policy(model: dict, errs: list[str]) -> None:
    services = model["services"]
    for forbidden in ("simulator", "demo"):
        if forbidden in services:
            errs.append(f"prod: service '{forbidden}' must not exist in the production file set")
    for name, svc in services.items():
        image = svc.get("image", "")
        base = image.rsplit("/", 1)[-1]
        if "build" not in svc and (":" not in base.split("@")[0] or base.endswith(":latest")):
            errs.append(f"prod: {name}: image {image!r} is untagged or :latest")
        for p in svc.get("ports", []):
            key = (name, int(p["target"]))
            if key not in PROD_PUBLISHED:
                errs.append(f"prod: {name} publishes {p.get('published')}->{p['target']}")
        if name == "models-init" or svc.get("network_mode") == "none":
            pass
        elif "ALL" not in svc.get("cap_drop", []):
            errs.append(f"prod: {name}: cap_drop must contain ALL")
        if "no-new-privileges:true" not in svc.get("security_opt", []):
            errs.append(f"prod: {name}: no-new-privileges missing")
        if not svc.get("read_only") and name not in HARDENING_EXEMPT_READ_ONLY:
            errs.append(f"prod: {name}: read_only must be true")
        if not svc.get("deploy", {}).get("resources", {}).get("limits", {}).get("memory"):
            errs.append(f"prod: {name}: memory limit missing")
        env = svc.get("environment", {})
        for k in env:
            if k.startswith("DEMO"):
                errs.append(f"prod: {name}: DEMO setting {k}")
    be = services["backend"]["environment"]
    if be.get("ENVIRONMENT") != "production":
        errs.append("prod: backend ENVIRONMENT must be production")
    if be.get("MODEL_MANIFEST_REQUIRED") != "true":
        errs.append("prod: backend MODEL_MANIFEST_REQUIRED must be true")
    if not be["ALLOWED_ORIGINS"].startswith("https://"):
        errs.append("prod: ALLOWED_ORIGINS must be https")
    if "*" in be["FORWARDED_ALLOW_IPS"]:
        errs.append("prod: FORWARDED_ALLOW_IPS must not be a wildcard")
    if "aquaguard_app" not in be["DATABASE_URL"]:
        errs.append("prod: backend must connect as the aquaguard_app role")
    for vol in services["backend"].get("volumes", []):
        if vol.get("target") == "/models" and not vol.get("read_only"):
            errs.append("prod: /models must be mounted read-only into the backend")


def check_paths(model: dict, errs: list[str]) -> None:
    for name, svc in model["services"].items():
        build = svc.get("build")
        if build:
            ctx = Path(build["context"])
            df = ctx / build.get("dockerfile", "Dockerfile")
            if not df.is_file():
                errs.append(f"{name}: Dockerfile {df} missing")
            else:
                check_dockerfile(name, df, ctx, errs)
        for vol in svc.get("volumes", []):
            if vol.get("type") == "bind":
                src = Path(vol["source"])
                if not src.exists() and ".dev-certs" not in str(src):
                    errs.append(f"{name}: bind mount source {src} does not exist")


def check_dockerfile(service: str, df: Path, ctx: Path, errs: list[str]) -> None:
    text = df.read_text()
    args = dict(re.findall(r"^ARG\s+(\w+)=(\S+)", text, re.M))
    stages: set[str] = set()
    for m in re.finditer(r"^FROM\s+(?:--\S+\s+)?(\S+)(?:\s+AS\s+(\w+))?$", text, re.M):
        ref, alias = m.group(1), m.group(2)
        for k, v in args.items():
            ref = ref.replace("${" + k + "}", v).replace("$" + k, v)
        if alias:
            stages.add(alias.lower())
        if ref.lower() in stages or ref in {"scratch", "backend"}:
            continue
        tag = ref.split("@")[0].rsplit(":", 1)
        if len(tag) < 2 or "/" in tag[-1] or tag[-1] == "latest":
            errs.append(f"{service}: {df.name}: FROM {ref} is untagged or :latest")
    for m in re.finditer(r"^COPY\s+((?:--\S+\s+)*)(.+)$", text, re.M):
        flags, rest = m.group(1), m.group(2).split()
        if "--from" in flags or rest[0].startswith("<<") or len(rest) < 2:
            continue
        for src in rest[:-1]:
            if "$" in src or "*" in src:
                continue
            if not (ctx / src).exists():
                errs.append(f"{service}: {df.name}: COPY source {src!r} not found in context {ctx}")


def main() -> int:
    errs: list[str] = []
    if list(ROOT.glob("docker-compose.override.y*ml")):
        errs.append("docker-compose.override.yml exists: Compose would apply it silently")
    for needle in ("demo", "DEMO_"):
        for f in ROOT.glob("docker-compose*.yml"):
            if re.search(rf"^(?!\s*#).*{needle}", f.read_text(), re.M):
                errs.append(f"{f.name}: contains {needle!r}")
    if shutil.which("docker") is None:
        print("docker CLI not found: compose rendering checks skipped", file=sys.stderr)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            make_env(env_file)
            for label, files in SETS.items():
                try:
                    model = render(files, env_file)
                except RuntimeError as exc:
                    errs.append(f"{label}: docker compose config failed: {exc}")
                    continue
                if label == "prod":
                    check_policy(model, errs)
                    check_paths(model, errs)
                if label == "dev":
                    check_paths(model, errs)
                    if "simulator" not in model["services"]:
                        errs.append("dev: simulator service missing")
    for e in errs:
        print(f"  - {e}", file=sys.stderr)
    if errs:
        print(f"compose checks FAILED ({len(errs)} problem(s))", file=sys.stderr)
        return 1
    print("compose checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
