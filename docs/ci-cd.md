# CI/CD

| Workflow | Trigger | What |
|---|---|---|
| `ci.yml` | PR, push to main | lint (ruff pinned to `RUFF_VERSION`, shellcheck, env/compose consistency, Caddyfile, actionlint), backend (py3.11/3.12), integration (real Postgres 16 + Mosquitto), ml, edge, bot, dashboard (node 22: lint, tsc, vitest, build), docker builds (backend, dashboard, bot, edge, backup, models), security (gitleaks, pip-audit, npm audit), aggregate `ci-success` |
| `nightly.yml` | 02:17 UTC, manual | integration suite, load smoke (100 meters, >= 95 % landed), backup/restore drill |
| `release.yml` | tag `vX.Y.Z` | build + push to GHCR with provenance (max) and SBOM, build attestation, Trivy scan (blocks on fixable HIGH/CRITICAL), SPDX SBOM artifacts. **Not executed yet** |

All workflows default to `permissions: contents: read` and use `concurrency`. gitleaks runs as a checksum-verified
pinned CLI (no licence needed). `pip-audit` has no severity, so any unlisted finding fails; `npm audit` gates on
**production** dependencies at high/critical (dev-tool findings are reported only). Accepted risks go in
`.github/audit-allowlist.json` with reason and expiry.

Actions are pinned to major tags. **TODO:** run `sh infra/scripts/pin-actions.sh` (needs `gh auth`) to pin full SHAs;
Dependabot (`.github/dependabot.yml`: actions, pip, npm, docker, compose) keeps them current.

## Recommended branch protection (`main`)

* Require pull requests, >= 1 approval, Code Owners review (fill `.github/CODEOWNERS`), dismiss stale approvals.
* Require status check `ci-success` (and nothing else: it aggregates), up-to-date branch, linear history.
* Block force pushes and deletions; require signed commits if your policy demands it.
* Restrict tag creation `v*` to maintainers (tags publish images); protect the `ghcr.io` packages.
* Enable secret scanning + push protection and Dependabot alerts in repository settings.
