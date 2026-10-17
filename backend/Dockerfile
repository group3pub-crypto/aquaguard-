FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 ENVIRONMENT=production PORT=8000

# Build context is the REPO ROOT: docker build -f backend/Dockerfile .
# (the shared train/serve feature package in libs/ is installed first; the ignore rules live in
# backend/Dockerfile.dockerignore, which BuildKit prefers over a root .dockerignore)
COPY libs/aquaguard_features /srv/libs/aquaguard_features
RUN pip install /srv/libs/aquaguard_features

WORKDIR /srv/backend
COPY backend/pyproject.toml ./
RUN mkdir app && touch app/__init__.py && pip install . && rm -rf app
COPY backend/ .
RUN pip install --no-deps . && chmod +x docker-entrypoint.sh \
    && useradd --system --uid 10001 --home /srv/backend aquaguard \
    && chown -R aquaguard /srv/backend
USER aquaguard

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/health/ready' % os.environ.get('PORT','8000'), timeout=4).status == 200 else 1)"
ENTRYPOINT ["./docker-entrypoint.sh"]
