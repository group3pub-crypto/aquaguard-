#!/bin/sh
# Apply migrations, then hand over to uvicorn (exec so signals reach the server).
set -e
alembic upgrade head
exec uvicorn app.main:app_factory --factory --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers
