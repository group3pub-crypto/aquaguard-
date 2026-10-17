# AquaGuard backend

FastAPI + async SQLAlchemy service: MQTT ingest, baselines, anomaly detection (ML with rule
fallback), alert escalation, valve control, REST + WebSocket API.

## Run

```bash
pip install -e '.[dev]'            # add '.[shap]' for SHAP explanations
export ENVIRONMENT=development      # production (the default) refuses to start without a strong SECRET_KEY
export DATABASE_URL=postgresql+asyncpg://aquaguard:aquaguard@localhost:5432/aquaguard
alembic upgrade head
uvicorn app.main:app_factory --factory --reload
ruff check . && pytest -q           # tests use in-memory SQLite, no services needed
```

The Docker image runs `alembic upgrade head` and then uvicorn (`docker-entrypoint.sh`).

## Integration tests (real PostgreSQL + Mosquitto)

`pytest` alone runs the SQLite/mock suite; `tests/integration/` (marker `integration`) is
**skipped** unless the environment points at real services. Use a dedicated, empty database: the
tests run Alembic against it and `TRUNCATE` every table before each test.

| Variable | Needed for |
|---|---|
| `TEST_DATABASE_URL` | all integration tests, e.g. `postgresql+asyncpg://user:pw@127.0.0.1:5432/aquaguard_test` |
| `TEST_MQTT_HOST`, `TEST_MQTT_PORT`, `TEST_MQTT_USERNAME`, `TEST_MQTT_PASSWORD` | tests marked `mqtt` (broker round trip, reconnect/resubscribe, redelivery, anonymous refusal); the user needs `readwrite aquaguard/#` |
| `TEST_MQTT_DEVICE_SECRET` | optional: per-device ACL test (broker started with `MQTT_DEVICES` containing `green-valley/101` and the same `MQTT_DEVICE_SECRET`) |

```bash
# broker with auth + per-device ACL, rendered from env (see infra/mosquitto/README.md)
MQTT_RUNTIME_DIR=/tmp/aq-mq MQTT_DATA_DIR=/tmp/aq-mq/data MQTT_LISTEN_ADDR=127.0.0.1 \
MQTT_LISTEN_PORT=1883 MQTT_USERNAME=aquaguard-backend MQTT_PASSWORD=$(openssl rand -hex 12) \
MQTT_DEVICE_SECRET=$(openssl rand -hex 24) MQTT_DEVICES=green-valley/101 \
  sh ../infra/mosquitto/entrypoint.sh &
export TEST_DATABASE_URL=... TEST_MQTT_HOST=127.0.0.1 TEST_MQTT_PORT=1883 \
       TEST_MQTT_USERNAME=aquaguard-backend TEST_MQTT_PASSWORD=...
pytest tests/integration -m integration -v
```

Covered: Alembic up/down/up and `alembic check` (no model drift), CHECK constraints / partial
index predicates / ON DELETE actions vs the models, `updated_at` triggers, cascades, JSONB,
unique constraints, pagination, concurrent duplicate and concurrent distinct ingest, the alert
lifecycle, MQTT reconnect + resubscribe, at-least-once redelivery, device ACL, advisory-lock
leader election, `FOR UPDATE SKIP LOCKED` escalation concurrency, `ctid` retention batches, the
`EXTRACT(epoch)` sensor-health SQL, single-use WS tickets / refresh rotation under concurrency and
the username-lowercase migration on existing data. CI runs them in the
`integration` job (`postgres:16` service + Mosquitto started with the same entrypoint).

Ingest tuning (env): `MQTT_WORKER_CONCURRENCY` (16), `DETECT_MAX_CONCURRENCY` (2, ML runs are
shed when saturated), `DETECT_DEBOUNCE_SECONDS` (30), `MQTT_MAX_FUTURE_SKEW_S` (300; raise for the
accelerated edge simulator). Measured throughput: `docs/performance.md`.

`DETECTION_POLICY` = `rules` | `ml` | `hybrid` (default). `hybrid` uses the deterministic rules as a
gate and the RandomForest for class/confidence (and a veto when P(normal) > 0.5); without loaded,
manifest-verified models every policy runs rules only, and `GET /health/details` -> `detection`
shows `policy`, `effective` and the reason. The default may change after `ml/reports/benchmark.md`
is reviewed on real data (the benchmark is synthetic).

LLM alert text (optional, off by default): `LLM_ENABLED`, `ANTHROPIC_API_KEY`, `LLM_MODEL`, ... see
`app/services/llm/settings.py`. An alert is always stored first with the template text
(`message_source=template`); after the transaction commits a background task may replace it
(`anthropic` or `template_fallback`). The LLM can never delay or prevent an alert.

## First start (no data is ever seeded)

A fresh database is empty and the API has no unauthenticated way to create anything. Bootstrap
with the CLI, then onboard through the admin API:

```bash
alembic upgrade head
python -m app.cli create-admin --username admin          # prompts twice on a TTY ...
printf '%s\n' "$PW" | python -m app.cli create-admin --username admin --password-stdin   # ... or stdin
# passwords are NEVER read from argv; policy: >= 12 chars, a letter and a digit, <= 72 BYTES
```

Then, as that admin (`POST /api/v1/auth/login`): `POST /societies`, `POST /flats` (the sensor id is an inventory label,
default `<slug>.<unit>`; the broker username is `<slug>/<unit>`), `POST /users` (+ `PUT /users/{id}/binding`).
Per sensor, print the broker credentials ONCE (nothing is written into `infra/`):

```bash
python -m app.cli provision-sensor --society green-valley --unit 101
# -> device username (green-valley/101), secret, the `mosquitto_passwd -b ...` command and the
#    ACL lines (identical to infra/mosquitto/acl.device.template); sensor_id is not modified
python -m app.cli retention-run [--dry-run]               # also runs daily on the leader
```

`scripts/dev_bootstrap.sh` strings these together for local development (NOT for production).

## Roles, scoping and who may do what

| Role | Sees | May act |
|---|---|---|
| `admin` | everything | everything, incl. valves, overrides, onboarding, users |
| `maintenance` | flats, readings, alerts, anomalies, stats and reports of THEIR society (`users.society_id`) | acknowledge / snooze / resolve alerts in that society |
| `resident` | ONLY their bound flat (`users.flat_id`) + the read-only name/address of its society | nothing (read-only) |
| unbound (no flat / society) | nothing: empty lists, 404 for ids | nothing |

Scope is applied **in SQL** on every route (`app/scope.py`; list endpoints are filtered, not
post-filtered) and an out-of-scope id answers **404**, never 403, so existence does not leak.
`/tools/*` obey the same scope; society-wide aggregates (`/societies/{id}/stats`,
`/tools/top-losses`, `/tools/weekly-report`) are staff-only. `tests/test_scope_matrix.py`
enumerates `app.routes`, FAILS for a route without a declared policy (`Depends(get_scope)`,
`AdminUser`, `SelfRoute`, `PublicRoute`, ...) and attacks every data route as a resident of
another flat, an unbound user and staff of another society.

Self-registration is OFF (`ALLOW_SELF_REGISTRATION=false` => 404). When enabled it creates an
UNBOUND resident that sees nothing until an admin binds it.

## Authentication lifecycle

* `POST /auth/login` -> `{access_token, refresh_token, expires_in, refresh_expires_in, role}`.
  Access tokens live `ACCESS_TOKEN_MINUTES` (15) and carry `tv` (the user's `token_version`),
  checked against the DB on every request: role change, deactivation, password change/reset and
  "logout everywhere" take effect immediately.
* `POST /auth/refresh {refresh_token}` rotates (the old token is spent). Presenting a spent token
  revokes its whole family (theft detection). Refresh tokens are opaque, stored as SHA-256.
* `POST /auth/logout {refresh_token}` (this device), `POST /auth/logout-all`,
  `POST /auth/change-password {current_password,new_password}` (returns a fresh pair),
  admin: `POST /users/{id}/reset-password`, `POST /users/{id}/logout-all`.
* Usernames are case-insensitive (stored lowercase). Login failures are identical for unknown
  user / wrong password / inactive / locked. After `LOGIN_MAX_FAILURES` (10) failures within 15
  min the account is locked for 15 min. Rate limit key = IP + username (`RATE_LIMIT_STORAGE_URI`).
* The last active admin can never be demoted/deactivated (409).
* **Service accounts** (`users.is_service`, e.g. the Telegram bot) may send `on_behalf_of` (<= 100
  chars) on alert acknowledge/snooze/override/resolve, valve open/close and `/tools/valve-control`.
  It is stored in `audit_log.details` and echoed as `on_behalf_of` + `on_behalf_of_verified:false`:
  asserted, NOT verified. Everyone else sending it gets 422. The audit ACTOR is always the
  authenticated username; `acknowledged_by` / `snoozed_by` / `issued_by` / ... in bodies are ignored.
* `GET /users/by-username/{username}` (admin or service account) lets the bot verify `/link`.

## WebSocket

1. `POST /api/v1/auth/ws-ticket` (Bearer) -> `{ticket, expires_in: 30}`: single use, bound to the
   user, their `token_version` and data scope, stored hashed in `ws_tickets`.
2. Connect to `/api/v1/ws/live?ticket=<ticket>[&flat_id=..&society_id=..]`. `?token=<JWT>` is no
   longer accepted. The server re-reads the user, filters every event by the connection's scope
   (client filters can only narrow), drops the socket when the user is deactivated / logged out
   (checked every `WS_REVALIDATE_S`), pings `{"type":"ping"}` every `WS_PING_INTERVAL_S` and closes
   idle clients after `WS_IDLE_TIMEOUT_S` (any client frame, e.g. text `ping` -> `pong`, counts).
3. Close codes: `4401` auth, `4403` empty scope (unbound), `4429` connection cap
   (`WS_MAX_PER_USER`, `WS_MAX_CONNECTIONS`), `4408` idle.

## Alert / valve safety

* New alerts start at tier <= 3. Dedupe: same flat + anomaly type with the same ongoing anomaly or
  activity in the last 30 min => one alert; a severity increase re-evaluates the tier (an
  acknowledged / snoozed alert re-opens only on an increase that reaches a higher tier).
* While a flat's baseline is `learning` nothing escalates above tier 2 ("alert") and the alert text
  and audit say so. `ready` needs >= 14 distinct days AND every one of the 168 hour-of-week slots
  with >= `MIN_SLOT_SAMPLES` (30) samples; `ready` -> `stale` after 30 days without retraining.
* **Automatic shut-off** needs the per-flat commissioning gate (`flats.auto_shutoff_validated`,
  default FALSE for every flat including existing ones; an admin sets it with
  `PUT /flats/{id}/auto-shutoff-validation {validated, note}`, audited, revocable). Without it the
  alert stays at `ticket` and the audit says `shutoff_not_commissioned` ("auto shut-off not
  commissioned for this flat"); manual closes are unaffected. It also needs: leak/burst, confidence >= 0.85, severity >= 0.9, 6 h at "ticket",
  baseline not learning, recent data and no active water use (any 15-min slice of the last
  `ACTIVE_USE_WINDOW_MIN` = 60 min with regularly stopping flow). Then:
  1. `shutoff_warned_at` set, audit + WS `shutoff_warning` (staff / resident can cancel by
     acknowledging, snoozing, overriding or resolving);
  2. after `SHUTOFF_WARNING_SECONDS` (300) the guards are re-evaluated; if they still hold the
     command row is **committed (pending) and only then published**;
  3. the alert becomes tier 4 / `auto_shutoff` (and the resident is told) **only after the device
     ack confirms** (`valve_commands.status=confirmed`). A late ack of a timed-out command counts.
  4. publish failure / no ack within `VALVE_ACK_TIMEOUT_S` (60): the alert stays at "ticket", the
     close is retried exactly once, then we give up, audit `shutoff_failed` and send a WS `alert`
     event `shutoff_failed` so staff close the valve by hand.
* Escalation: one query selects due alerts (keyset pages, no cap), one transaction + `FOR UPDATE
  SKIP LOCKED` per alert. The maintenance loop runs only on the leader (PostgreSQL advisory lock on
  a dedicated connection; SQLite always leads). SIGTERM: the in-flight cycle finishes (bounded by
  `SHUTDOWN_GRACE_S`), then MQTT and the DB close.
* Retention: `READINGS_RETENTION_DAYS` (90, 0 = off) and `AUDIT_RETENTION_DAYS` (0 = off), deleted
  in batches of `RETENTION_BATCH_SIZE` daily on the leader or via `retention-run`.

## Operations endpoints

| Endpoint | Auth | Notes |
|---|---|---|
| `GET /health` | none | `{"status":"ok"|"degraded"}` only |
| `GET /health/ready` | none | 503 unless DB reachable AND alembic revision == head AND (MQTT connected OR `MQTT_REQUIRED=false`) |
| `GET /health/details` | admin | counters, dead letters, MQTT state, model status, migration, uptime, leader |
| `GET /metrics` | `Bearer $METRICS_TOKEN` | `METRICS_ENABLED=true` only (404 otherwise): `ingest_messages_total{result}`, `detection_seconds`, `alerts_total{tier}`, `ws_connections`, `mqtt_connected`, `maintenance_leader` |

`/docs`, `/redoc`, `/openapi.json` are off in production unless `ENABLE_DOCS=true`. `X-Request-ID`
is kept only if it matches `[A-Za-z0-9._-]{1,64}`. The image is built from the REPO ROOT:
`docker build -f backend/Dockerfile .` (ignore rules: `backend/Dockerfile.dockerignore`).
`docker-entrypoint.sh` honours `PORT`, `WEB_CONCURRENCY` and `FORWARDED_ALLOW_IPS` (default
`127.0.0.1`, a wildcard is refused). With `WEB_CONCURRENCY > 1` set `RATE_LIMIT_STORAGE_URI` to a
shared store (`pip install -e '.[redis]'`), otherwise limits are per process (logged loudly).

## Configuration (env)

| Variable | Default | Notes |
|---|---|---|
| `ENVIRONMENT` | `production` | `development` / `test` / `production`. |
| `SECRET_KEY` | - | Production: >= 32 chars, >= 16 distinct, not a placeholder. Dev/test: random per process. |
| `DATABASE_URL` | - | REQUIRED in production; default/placeholder DB passwords are rejected. |
| `ALLOWED_ORIGINS` | - | Production: explicit `https://` origins, no `*`, not empty. |
| `ACCESS_TOKEN_MINUTES` (legacy `ACCESS_TOKEN_EXPIRE_MINUTES`) | 15 | |
| `REFRESH_TOKEN_DAYS` | 14 | |
| `LOG_LEVEL` (legacy `BACKEND_LOG_LEVEL`) | INFO | Legacy names still work and log a deprecation warning. |
| `ALLOW_SELF_REGISTRATION` | false | |
| `LOGIN_MAX_FAILURES` / `LOGIN_LOCK_MINUTES` | 10 / 15 | |
| `RATE_LIMIT_ENABLED`, `RATE_LIMIT_STORAGE_URI`, `WEB_CONCURRENCY`, `FORWARDED_ALLOW_IPS` | true, `memory://`, 1, `127.0.0.1` | |
| `WS_MAX_CONNECTIONS`, `WS_MAX_PER_USER`, `WS_TICKET_TTL_S`, `WS_IDLE_TIMEOUT_S`, `WS_PING_INTERVAL_S`, `WS_REVALIDATE_S` | 200, 5, 30, 90, 30, 60 | |
| `MQTT_HOST/PORT/USERNAME/PASSWORD`, `MQTT_ENABLED`, `MQTT_REQUIRED` | | `MQTT_REQUIRED=false` lets `/health/ready` pass without a broker. |
| `MODEL_DIR`, `MODEL_MANIFEST_REQUIRED` | `<repo>/ml/models`, true in production | Missing models => rule-based detection. |
| `VALVE_ACK_TIMEOUT_S`, `SHUTOFF_WARNING_SECONDS`, `ACTIVE_USE_WINDOW_MIN`, `MIN_SLOT_SAMPLES` | 60, 300, 60, 30 | |
| `READINGS_RETENTION_DAYS`, `AUDIT_RETENTION_DAYS`, `RETENTION_BATCH_SIZE` | 90, 0, 5000 | |
| `MAINTENANCE_INTERVAL_S`, `SHUTDOWN_GRACE_S` | 60, 20 | |
| `METRICS_ENABLED`, `METRICS_TOKEN` | false, - | Enabling needs a token of >= 24 chars. |
| `ENABLE_DOCS` | false in production | |
| `TIMEZONE`, `DETECT_DEBOUNCE_SECONDS` | UTC, 30 | See `app/config.py` for the rest. |

## Notes

- All DB access goes through `app/repositories`; services and routers never build queries.
- Hour-of-week slots, "night" and forecast features use `TIMEZONE` (default UTC).
- Forecast target is the mean flow rate in L/min per hour (not litres per hour); litres = L/min x 60.
- The flow payload accepts the optional firmware fields `tot`, `vbat`, `valve`, `fw`, `err`
  (validated in `app/schemas/mqtt.py`); ingest will persist them in a later change.

## LLM-written alert text (optional, off by default)

`app/services/llm/` adds `AnthropicExplainer` behind the `Explainer` interface. Install with
`pip install -e "backend[llm]"` (the `anthropic` package is imported lazily; without it, or without a key,
the template explainer is used). Build it with `app.services.llm.get_explainer()` (reads the env itself).

| Variable | Default | Notes |
|---|---|---|
| `LLM_ENABLED` | `false` | Master switch. Setting it to `false` is all that is needed to stop sending data. |
| `ANTHROPIC_API_KEY` | unset | From the environment only; never commit it. Missing key => template text. |
| `LLM_MODEL` | `claude-haiku-5-5` | `claude-sonnet-5-5` is the optional upgrade. |
| `LLM_TIMEOUT_S` | `4` | Hard cap (max 4) for the whole call including the single 429/overloaded retry. |
| `LLM_MAX_TOKENS` | `600` | Response cap (includes any model thinking tokens). |
| `LLM_DAILY_BUDGET` | `500` | Provider calls per UTC day; `0` disables calls. Exhausted => template fallback. |
| `LLM_MAX_CONCURRENCY` | `4` | Global in-flight limit. |
| `LLM_LANGUAGE` | `en` | `en` or `hi`. |
| `LLM_BREAKER_THRESHOLD` / `LLM_BREAKER_COOLDOWN_S` | `5` / `60` | Circuit breaker on consecutive provider failures. |
| `LLM_CACHE_SIZE` | `1024` | Per-alert result cache entries. |

**Data sent to the provider.** Per alert one request containing a fixed FACTS object: flat unit label (sanitized,
max 16 chars), anomaly type, tier, severity band, detector confidence, estimated loss (L/min), duration, night/day,
up to 3 top factor names (+ impact, and whether they came from SHAP or rules), and a recommended action chosen from
a fixed table. **Not sent:** usernames, emails, phone numbers, UUIDs, raw sensor series, addresses or resident
names. The unit label can still identify a household to someone who knows the building, so treat it as personal data:
check your data-processing agreement / privacy notice with the provider and your residents' consent before enabling.
Prompt contents are not logged at info level (only model, latency, token counts, outcome).

**Behaviour.** Alert creation never waits for the LLM: the engine stores the template text immediately
(`message_source='template'`); after commit the message is regenerated and `message_source` becomes `anthropic`, or
`template_fallback` when anything fails (no key/package, timeout, 429/5xx, budget or breaker open, or the output
fails validation). Validation rejects numbers not present in the facts (beyond rounding and x60 / x100 derivations),
URLs, markdown, phone numbers, guarantees, over-length text and any claim that water was shut off unless
`valve_state_confirmed` is true.

**Costs.** Cost scales with alert volume x tokens per call; `LLM_DAILY_BUDGET` is a hard ceiling on calls per day.
Check the provider's current pricing; no figures are given here.

**Engine hook** (to be wired in `alert_engine`): build the ctx for `rt.explainer.message(...)` with
`facts_from_models(flat, anomaly, alert, night=..., valve_state_confirmed=...)`, and after the creating
transaction commits call `schedule_regeneration(rt.db, alert.id, ctx, rt.explainer)`.
