FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 ENVIRONMENT=production

WORKDIR /srv/backend
COPY pyproject.toml ./
RUN mkdir app && touch app/__init__.py && pip install . && rm -rf app
COPY . .
RUN pip install --no-deps . && chmod +x docker-entrypoint.sh \
    && useradd --system --uid 10001 --home /srv/backend aquaguard \
    && chown -R aquaguard /srv/backend
USER aquaguard

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=4).status == 200 else 1)"
ENTRYPOINT ["./docker-entrypoint.sh"]
