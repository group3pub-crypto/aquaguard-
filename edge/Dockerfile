# syntax=docker/dockerfile:1
# DEVELOPMENT / TEST TOOL - the sensor SIMULATOR. It publishes SYNTHETIC readings and must never
# run in production (real devices are ESP32 sensors, see edge/firmware).
#
# Build context is the REPO ROOT:  docker build -f edge/Dockerfile -t aquaguard-edge .
# (allow-list in edge/Dockerfile.dockerignore)
ARG PYTHON_IMAGE=python:3.12.8-slim-bookworm
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN pip install "aiomqtt>=2,<3" "pydantic>=2.5,<3" "numpy>=1.26"

RUN useradd --system --create-home --uid 10001 sim
WORKDIR /app
COPY --chown=sim:sim edge/__init__.py /app/edge/__init__.py
COPY --chown=sim:sim edge/simulator /app/edge/simulator
USER sim

# Override: docker run ... --society dev-society --flats 10 --broker mosquitto:1883
ENTRYPOINT ["python", "-m", "edge.simulator"]
CMD ["--society", "dev-society", "--flats", "6", "--speed", "1", "--broker", "mosquitto:1883"]
