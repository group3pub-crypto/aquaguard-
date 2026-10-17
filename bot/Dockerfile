# syntax=docker/dockerfile:1
# Build context: bot/   (docker build -t aquaguard-bot bot/)
# Pin the base by digest with `make pin-digests` (--build-arg PYTHON_IMAGE=...@sha256:<digest>).
ARG PYTHON_IMAGE=python:3.12.8-slim-bookworm
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN useradd --system --create-home --uid 10001 bot && mkdir /state && chown bot /state
WORKDIR /app
COPY pyproject.toml ./
COPY bot ./bot
RUN pip install .
USER bot
VOLUME ["/state"]
CMD ["python", "-m", "bot.main"]
