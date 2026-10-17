# SPEC

See `README.md` (problem, architecture, security model), `docs/api.md` (access contract) and `docs/mqtt.md` (topics/payloads). Acceptance: `make up` brings up the production stack and `make bootstrap-admin` creates the first admin; `make test` passes; no secrets in the repo; production fails closed without strong secrets.
