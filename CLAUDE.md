# CLAUDE.md

AquaGuard: ESP32 sensors -> MQTT -> FastAPI (ML + 4-tier alert state machine) -> Next.js dashboard + Telegram bot.

## Stack
- backend/: Python 3.11, FastAPI, SQLAlchemy async + Alembic, PostgreSQL 16, JWT bearer auth, REST under `/api/v1`, health `/health`
- dashboard/: Next.js (npm scripts: lint, build, test)
- edge/: Python simulator (`python -m edge.simulator`) and firmware
- ml/: `scripts/{generate_data,train_detector,train_forecaster}.py`, models in `ml/models/` (gitignored), reports in `ml/reports/`
- bot/: python-telegram-bot v21, tool registry (`bot/tools/registry.py`)
- infra/: mosquitto, postgres init, loadtest

## Commands
`make help` | `make up/down/logs` (production file set) | `make dev-up` / `sim-up` (dev only) | `make bootstrap-admin` | `make provision-sensor` | `make backup` / `restore-test` | `make test` (or `test-backend|ml|edge|dashboard|bot`) | `make lint` | `make format` | `make typecheck` | `make train` | `make env`

## Conventions
- Never commit secrets; `.env` is gitignored, `.env.example` holds placeholders (`CHANGE_ME`). Fail closed on missing/weak secrets.
- Ruff for Python (line length 100-120), type hints on public functions, async I/O.
- Role checks happen server-side (resident < maintenance < admin); the UI/bot only hide controls.
- Do not invent ML numbers in docs; point to `ml/reports/`.
- Docker ports bind to 127.0.0.1 in dev; production publishes only the TLS proxy (80/443) and MQTT TLS (8883). Keep docs in `docs/` in sync with `docs/api.md` and `docs/mqtt.md` when changing contracts.
