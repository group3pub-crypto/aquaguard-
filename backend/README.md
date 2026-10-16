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
