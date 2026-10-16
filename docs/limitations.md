# Limitations

- **Synthetic data**: ML models are trained on generated data. Reported metrics (in `ml/reports/`) do not predict field accuracy.
- **No hardware validation**: firmware and valve control have not been validated on real plumbing; the simulator stands in for devices.
- **Single node**: one broker, one database, one backend instance; no HA, no horizontal scaling.
- **Token revocation not implemented**: JWTs stay valid until they expire; there is no logout blacklist or refresh rotation.
- **MQTT**: no TLS and no per-device topic ACLs by default; all devices share one credential.
- **Bot**: acts as one backend service account; per-user roles are enforced inside the bot only. Link state is a local JSON file. Intent matching is keyword-based, not NLU.
- **Load test**: `infra/loadtest` measures publisher-side throughput and PUBACK latency, not backend ingestion or end-to-end latency.
- **Safety**: remote valve closure can affect fire-fighting or supply; keep a manual override in real deployments.
