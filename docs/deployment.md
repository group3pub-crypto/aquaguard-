# Deployment (single node, Docker Compose)

Everything below is how the repository is meant to be run in production. Items marked
**UNTESTED** were written without a Docker daemon (or the relevant binary) available; what was
actually exercised is listed in "Verification status".

## File sets (nothing is applied implicitly)

| Purpose | Command | Files |
|---|---|---|
| Production | `make up` | `docker-compose.yml` + `docker-compose.prod.yml` |
| Development | `make dev-up` | `docker-compose.yml` + `docker-compose.dev.yml` |
| Simulator (dev/test tool, **never production**) | `make sim-up` | dev file, profile `sim` |
| Optional TimescaleDB (**UNTESTED, see caveat**) | add `-f docker-compose.timescale.yml` | |
| Optional Prometheus/Alertmanager (**UNTESTED**) | add `-f docker-compose.monitoring.yml` | |

There is no `docker-compose.override.yml`: Compose would apply it silently. `make STACK=dev <target>` runs any
target against the dev set.

## First deployment

1. DNS: point `PUBLIC_DOMAIN` at the host; open 80, 443 and 8883.
2. `make env`, then edit `.env`: `PUBLIC_DOMAIN`, `CADDY_TLS` (an e-mail for Let's Encrypt, or `internal`),
   `TIMEZONE`, the MQTT TLS paths (below). `make check-env` must pass; `make up` runs it.
3. `make up` builds the images, trains the models (see "Models"), migrates the database as the owner role,
   starts the backend only after `db-migrate` and `models-init` succeeded.
4. `make bootstrap-admin` (password prompt). Create societies/flats through the API, then
   `make provision-sensor SOCIETY=<slug> UNIT=<n>` per device.

## Variables

`.env.example` is authoritative and checked in CI (`infra/scripts/check_env.py --lint`): every variable
used by a compose file is documented there, every documented variable is used, and every variable
passed to backend/bot/dashboard is read by that service's config module. Which component rejects
which `CHANGE_ME` placeholder is listed at the top of `.env.example`; `make check-env` additionally
enforces them all. Known name conflict: the backend caps `LLM_TIMEOUT_S` at 4 s, the bot allows 60 s,
so compose maps the bot's value from `BOT_LLM_TIMEOUT_S`.

Secrets are plain environment variables from a `0600` `.env`. The backend and bot do not read `*_FILE`
variables or Docker secrets (only Postgres' own image does, which we do not use for consistency); in
Swarm/Kubernetes inject them as environment from the platform's secret store.

## TLS and reverse proxy

Caddy (`infra/caddy/Caddyfile`) terminates HTTPS for `PUBLIC_DOMAIN`: `/api/*` (including the WebSocket
`/api/v1/ws/live`) goes to the backend, everything else to the dashboard. HSTS, `nosniff`, default
referrer/frame/permissions headers (the dashboard keeps its own nonce-based CSP), 1 MB request limit, gzip/zstd,
JSON access log with the one-time WebSocket `ticket` redacted. `/health`, `/metrics` and `/docs` are not routed.

* `CADDY_TLS=you@example.org`: ACME (Let's Encrypt), automatic renewal; needs 80/443 reachable.
* `CADDY_TLS=internal`: Caddy's private CA for LANs. Trust its root on clients:
  `docker compose -f docker-compose.yml -f docker-compose.prod.yml cp proxy:/data/caddy/pki/authorities/local/root.crt .`
* CORS (`ALLOWED_ORIGINS`) and the dashboard's API/WebSocket URLs derive from `PUBLIC_DOMAIN`.
* The backend trusts `X-Forwarded-*` only from `FORWARDED_ALLOW_IPS`, default `172.31.250.2`: the fixed
  address Caddy has on the `app` network (subnet `172.31.250.0/24` in `docker-compose.yml`). If that
  subnet collides with yours, change both consistently. The entrypoint refuses `*`.

### MQTT (devices)

Port 8883 (TLS >= 1.2, username/password + per-device ACL) is published; the plain 1883 listener never is
(production) and exists only on the private network for the backend.

* **ACME certificate shared from Caddy (UNTESTED):** the proxy's data volume is mounted read-only into
  Mosquitto at `/caddy-data`; set `MQTT_TLS_CERT_FILE`/`MQTT_TLS_KEY_FILE` as shown in `.env.example`.
  Mosquitto reads the files at start, so restart it after renewals (about every 60 days):
  `docker compose ... restart mosquitto` from cron/systemd timer. It will crash-loop until Caddy has issued the
  first certificate. The device must trust the Let's Encrypt chain.
* **Private CA (LAN):** put `server.crt`, `server.key`, `ca.crt` in `MQTT_TLS_DIR` and flash `ca.crt` into the
  device. `sh infra/scripts/gen-dev-certs.sh` creates a 30-day throw-away CA: **development only**.
* No TLS files configured means no device listener: `make check-env` fails closed.

## Database roles

Created once by `infra/postgres/10-roles.sh` + `init.sql` (data directory must be empty):

| Role | Use | Rights |
|---|---|---|
| `aquaguard_admin` | bootstrap superuser | break-glass and restores; no service uses it |
| `aquaguard_owner` | `db-migrate` one-shot | owns schema/tables, runs alembic |
| `aquaguard_app` | backend runtime | SELECT/INSERT/UPDATE/DELETE, sequence usage; no DDL/TRUNCATE |
| `aquaguard_backup` | backup container | `pg_read_all_data` |

The backend entrypoint still runs `alembic upgrade head`; with the app role that is a verified no-op when the
schema is already at head (the restore test proves it), so a new migration must be applied by `db-migrate`
first (`make up` / `make migrate` do this). Changing a password later: `ALTER ROLE ... PASSWORD`, edit `.env`,
recreate the services. Passwords must be URL-safe (`make env` generates hex).
Postgres is tuned for the 1 GiB limit (`shared_buffers=256MB`, `max_connections=100`, ...); scale the
settings together with `deploy.resources`.

### TimescaleDB (optional, UNTESTED)

`docker-compose.timescale.yml` swaps the image; `infra/postgres/timescale.sql` converts `flow_readings`
to a hypertable with compression and retention. **It changes the unique constraint to include the timestamp,
which breaks the backend's `ON CONFLICT (flat_id, message_id)` de-duplication**; do not use it until the backend
and migrations are adapted.

## Models

`ml/Dockerfile.models` trains the detector and forecaster at image-build time **on top of the backend image**
(identical Python/scikit-learn, required because the artefacts are pickles), writes `manifest.json`, and verifies
every file with the backend's own `verify_manifest` before the image is accepted. `models-init` copies them
into the `models` volume at every `up`; the backend mounts it read-only with `MODEL_MANIFEST_REQUIRED=true`.
**These models are trained on SYNTHETIC data.** They are a starting point, not a calibrated production model:
recalibrate with real readings (`ml/scripts/train_from_export.py`) and read `ml/reports/` (and `benchmark.md`
when present) before trusting any number. Build time depends on the machine: the default forecaster training
(12 flats) took 20+ minutes on a shared 4-core VM, so the image trains it on `ML_FORECAST_FLATS=4` (detector: default config, ~2 min). The image build itself was NOT run; times are from running the scripts directly.

## Backups and restore

* `make backup-up` (profile `backup`, needs `BACKUP_AGE_RECIPIENT`): daily `pg_dump --format=custom`, checked with
  `pg_restore --list`, encrypted with **age** to your public key, `.sha256` next to it, pruned after
  `BACKUP_RETENTION_DAYS`. Only the public key is on the server; keep the private identity offline.
  `make backup` takes one immediately. Files are in the `backups` volume: **copy them off the host** (rclone/restic/S3).
  A gpg variant is `pg_dump -Fc | gpg --encrypt -r KEY` - not scripted here.
* Restore (stop the backend first):
  `infra/backup/restore.sh --file F.dump.age --identity key.txt --target-db aquaguard_restored --create`
  (superuser connection variables `PGHOST/PGUSER/PGPASSWORD`). It verifies the checksum, decrypts, restores with
  `SET ROLE aquaguard_owner` and re-applies grants. Restoring into the live database needs `--allow-live`.
* **Drill:** `make restore-test` creates a throw-away native PostgreSQL cluster, runs the real role script and
  alembic migrations, loads data as the app role, backs up, restores, and compares row counts and content hashes;
  it also checks wrong key / tampered file / live-DB refusal. Run nightly by `.github/workflows/nightly.yml`.
* Not backed up on purpose: Mosquitto persistence (broker config is rendered from `.env`; devices republish valve
  state), the `models` volume (rebuilt from the image), Caddy's data volume (certificates are re-issued;
  back it up if you use `tls internal` and distributed its root).
* **Point-in-time recovery (WAL archiving) is not enabled.** Nightly dumps give an RPO of up to 24 h. For lower RPO
  add to the postgres `command`: `-c archive_mode=on -c archive_command='...'` with `pg_basebackup` (or pgBackRest/
  WAL-G) shipping to object storage, and test recovery with `recovery_target_time`. Needs a writable archive path
  and a base-backup schedule; **UNTESTED**, not configured.

## Monitoring (UNTESTED examples)

Set `METRICS_ENABLED=true` and `METRICS_TOKEN` (>= 24 chars); `docker-compose.monitoring.yml` adds Prometheus and
Alertmanager (loopback ports, no auth: tunnel in). Config and rules are in `infra/monitoring/` (metric names come
from `backend/app/metrics.py`). Loki/Promtail: `infra/monitoring/promtail.yml` is an optional sketch (needs the
Docker socket). Health: `/health/ready` is the container healthcheck.

## Upgrades

`git pull && make up` rebuilds, re-trains models, runs `db-migrate`. From registry images (release workflow):
`AQUAGUARD_REGISTRY=ghcr.io/<org>/aquaguard AQUAGUARD_TAG=v1.2.3` in `.env`, then
`docker compose -f docker-compose.yml -f docker-compose.prod.yml pull && ... up -d --no-build`. Take a backup
first. Pin third-party base images by digest: `make pin-digests` prints them (tags in the repo are best-effort and
were **not verified against a registry**).

## Verification status

Executed here: compose rendering for all file sets (`docker compose config`), `check_compose.py`/`check_env.py`,
Caddyfile validation (`caddy validate`, v2.10.0), shellcheck, actionlint, ruff, the pytest/npm suites listed in
the task report, and `make restore-test` against PostgreSQL 16.15. Not executed (no Docker daemon): any image
build, container start, health check, read-only/cap-drop behaviour of the stock images, Caddy ACME/WebSocket
proxying, Mosquitto cert sharing, the backup container, models image build, Timescale, monitoring, release workflow.
