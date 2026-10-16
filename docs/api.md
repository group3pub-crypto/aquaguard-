# API

Base path `/api/v1`, JSON, `Authorization: Bearer <JWT>` obtained from `POST /api/v1/auth/login` with `{"username","password"}`. Health: `GET /health` (no auth). Interactive docs at `/docs`.

Roles: resident < maintenance < admin. Reads are open to any authenticated user.

| Method & path | Purpose | Required role |
|---|---|---|
| POST `/auth/login` | Obtain JWT | none |
| GET `/health` | Liveness | none |
| GET `/alerts`, `/alerts/{id}` (`?state=`) | List / read alerts | any authenticated |
| GET `/units`, `/usage/*`, `/losses/*`, `/reports/*`, `/forecast/*` | Read data | any authenticated |
| POST `/alerts/{id}/ack` | Acknowledge | admin or maintenance |
| POST `/alerts/{id}/snooze` | Snooze | admin or maintenance |
| POST `/alerts/{id}/resolve` | Resolve | admin or maintenance |
| POST `/units/{unit}/valve` | Open/close valve | admin |
| POST `/alerts/{id}/override` | Manual state override | admin |
| POST `/demo/*` | Demo controls (only when `DEMO_MODE=true`) | admin |
| PUT `/users/{id}/role` | Change role | admin |
| POST `/ml/retrain` | Retrain models | admin |
| POST `/forecast/generate` | Generate forecast | admin |

This table states the intended access contract. Paths other than login and health are indicative; `/docs` on the running backend (OpenAPI) is the source of truth for exact routes and schemas.
