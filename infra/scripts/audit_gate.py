"""Gate a dependency audit report against an allow-list (stdlib only).

    npm audit --json > npm.json;           python infra/scripts/audit_gate.py npm npm.json
    pip-audit -f json -o pip.json ...;     python infra/scripts/audit_gate.py pip pip.json

* npm: fails on any `high` or `critical` advisory that is not allow-listed.
* pip: pip-audit reports no severity, so ANY unlisted vulnerability fails (stricter than "high").

Allow-list: .github/audit-allowlist.json, entries {"id", "reason", "expires": "YYYY-MM-DD"}.
An entry without reason, or past its expiry, is itself an error.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BLOCKING = {"high", "critical"}


def load_allowlist(kind: str, today: date) -> tuple[set[str], list[str]]:
    data = json.loads((ROOT / ".github" / "audit-allowlist.json").read_text())
    allowed: set[str] = set()
    errors: list[str] = []
    for entry in data.get(kind, []):
        ident = entry.get("id", "")
        if not entry.get("reason"):
            errors.append(f"allow-list entry {ident!r} has no reason")
            continue
        try:
            expired = date.fromisoformat(entry["expires"]) < today
        except (KeyError, ValueError):
            errors.append(f"allow-list entry {ident!r} needs expires=YYYY-MM-DD")
            continue
        if expired:
            errors.append(f"allow-list entry {ident!r} expired on {entry['expires']}")
            continue
        allowed.add(ident)
    return allowed, errors


def npm_findings(report: dict) -> list[tuple[str, str, set[str]]]:
    out = []
    for name, vuln in report.get("vulnerabilities", {}).items():
        sev = vuln.get("severity", "")
        if sev not in BLOCKING:
            continue
        ids: set[str] = set()
        for via in vuln.get("via", []):
            if isinstance(via, dict):
                url = via.get("url") or ""
                ids.add(url.rsplit("/", 1)[-1] if url else str(via.get("source", "")))
        ids.add(name)  # package-level allow-list entry (transitive findings carry no advisory id)
        out.append((name, sev, ids))
    return out


def pip_findings(report: dict) -> list[tuple[str, str, set[str]]]:
    out = []
    for dep in report.get("dependencies", []):
        for v in dep.get("vulns", []):
            ids = {v.get("id", "")} | set(v.get("aliases", []))
            out.append((f"{dep['name']}=={dep.get('version', '?')}", "unrated", ids))
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in {"npm", "pip"}:
        print(__doc__, file=sys.stderr)
        return 2
    kind, report_path = argv[1], Path(argv[2])
    report = json.loads(report_path.read_text())
    allowed, errors = load_allowlist(kind, date.today())
    findings = npm_findings(report) if kind == "npm" else pip_findings(report)
    for name, sev, ids in findings:
        if ids & allowed:
            print(f"allow-listed: {name} ({sev}) {sorted(ids)}")
            continue
        errors.append(f"{name}: {sev} advisory {sorted(i for i in ids if i)}")
    for e in errors:
        print(f"  - {e}", file=sys.stderr)
    if errors:
        print(f"{kind} audit gate FAILED ({len(errors)} problem(s))", file=sys.stderr)
        return 1
    print(f"{kind} audit gate ok ({len(findings)} blocking-severity finding(s), all allow-listed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
