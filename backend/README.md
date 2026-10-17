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
lifecycle, MQTT reconnect + resubscribe, at-least-once redelivery, device ACL. CI runs them in the
`integration` job (`postgres:16` service + Mosquitto started with the same entrypoint).

Ingest tuning (env): `MQTT_WORKER_CONCURRENCY` (16), `DETECT_MAX_CONCURRENCY` (2, ML runs are
shed when saturated), `DETECT_DEBOUNCE_SECONDS` (30), `MQTT_MAX_FUTURE_SKEW_S` (300; raise for the
accelerated demo simulator). Measured throughput: `docs/performance.md`.

## Configuration (env)

| Variable | Notes |
|---|---|
| `ENVIRONMENT` | `development` / `test` / `production` (default). |
| `SECRET_KEY` | Required in production: >= 32 chars, not a placeholder. Dev/test: random per process. |
| `ALLOWED_ORIGINS` | Comma separated CORS origins; `*` is rejected. |
| `DATABASE_URL` | `postgresql+asyncpg://...` |
| `MQTT_HOST/PORT/USERNAME/PASSWORD`, `MQTT_ENABLED` | Broker connection. |
| `MODEL_DIR` | Default `<repo>/ml/models`. Missing models => rule-based detection. |
| `DEMO_MODE` | Mounts `/api/v1/demo/*`. Needs `DEMO_ADMIN_PASSWORD` / `DEMO_RESIDENT_PASSWORD` in production. |
| `SEED_DEMO_DATA` | Seeds 1 society, 6 flats, `admin` and `resident` users on startup (idempotent). |
| `ACCESS_TOKEN_MINUTES`, `WS_MAX_CONNECTIONS`, `TIMEZONE`, `RATE_LIMIT_ENABLED` | See `app/config.py`. |

## Notes

- All DB access goes through `app/repositories`; services and routers never build queries.
- Auth: JWT (HS256). Roles are read from the DB on each request, so role changes apply at once.
  Self-registration always creates `resident`. WebSocket: `/api/v1/ws/live?token=<JWT>`.
- Hour-of-week slots, "night" and forecast features use `TIMEZONE` (default UTC).
- Forecast target is the mean flow rate in L/min per hour (not litres per hour); litres = L/min x 60.

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
