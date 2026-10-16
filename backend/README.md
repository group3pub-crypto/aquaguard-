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
