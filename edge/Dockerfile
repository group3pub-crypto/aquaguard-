# Build context: this directory (docker build -t aquaguard-edge edge/)
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir "aiomqtt>=2,<3" "pydantic>=2.5,<3" "numpy>=1.26"

RUN useradd --create-home --uid 10001 sim
WORKDIR /app
COPY --chown=sim:sim __init__.py /app/edge/__init__.py
COPY --chown=sim:sim simulator /app/edge/simulator
USER sim

# Override via `docker run ... --broker mosquitto:1883 --flats 10`
ENTRYPOINT ["python", "-m", "edge.simulator"]
CMD ["--society", "demo", "--flats", "6", "--speed", "60", "--broker", "mosquitto:1883"]
