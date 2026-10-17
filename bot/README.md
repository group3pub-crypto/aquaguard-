# AquaGuard Telegram bot

Async (`python-telegram-bot` v21, long-polling) bot that talks to the backend through a
dedicated service account.

## Run

```bash
cd bot && pip install -e ".[dev]" && pytest
TELEGRAM_BOT_TOKEN=... BOT_API_PASSWORD=... python -m bot.main
# or: docker compose --profile bot up -d   (from repo root)
```

| Variable | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | required, from @BotFather; never logged |
| `TELEGRAM_ADMIN_IDS` | comma-separated Telegram user ids with the admin role |
| `BOT_API_BASE_URL` / `BOT_API_USERNAME` / `BOT_API_PASSWORD` | backend SERVICE account (`is_service`, role `admin` recommended; see below). Password must be set and not `CHANGE_ME*` |
| `BOT_SHUTOFF_WARNING_SECONDS` | default 300; must equal the backend `SHUTOFF_WARNING_SECONDS`; only used to show the shut-off countdown |
| `BOT_STATE_FILE` | JSON file with approved links (atomic writes, mode 0600) |
| `BOT_SEEN_FILE` | delivered-alert ids (default `seen_alerts.json` beside `BOT_STATE_FILE`); without a state dir, dedupe is in-memory only |
| `BOT_AUDIT_FILE` | bot-side audit log, JSON lines (default `audit.jsonl` beside `BOT_STATE_FILE`) |
| `BOT_POLL_INTERVAL` | alert poll period in seconds (default 30; backs off up to 300 s on failure) |

## Commands

`/start`, `/link <backend_username>`, `/status`, `/alerts`, `/usage [unit] [hours]`; staff: `/report`, `/losses [days]`;
admin: `/valve <unit> open|close` (inline Confirm, valid 60 s, single use, only for the originating user, chat and
message), `/approve <request_id>`, `/reject <request_id>`, `/revoke <telegram_id>`.
Free text such as "which flats are leaking" or "top losses" is matched by regex rules and routed through the registry.

## Authorization model

- Admins = ids in `TELEGRAM_ADMIN_IDS`. Anyone else runs `/link <backend_username>` (rate limited to 3 per hour,
  at most 50 pending requests, 24 h TTL). The bot first checks the name against the backend
  (`GET /users/by-username/{u}`, service/admin only): unknown, inactive, service, admin, flat-less resident or
  already-claimed (`telegram_id` set to someone else) accounts are refused with one generic message and never reach
  an admin. Otherwise an admin approves with `/approve <request_id>`; the role (resident or maintenance) and flat
  come from the backend record, re-read at approval time. The mapping is stored in `BOT_STATE_FILE` and
  `telegram_id` is written to the backend user (`PATCH /users/{id}`, best effort: needs an admin service account).
  The backend cannot prove that the Telegram user owns the account, so the admin still vouches for the person.
  Nobody can become admin through the bot.
- Residents see only their bound flat (usage, alerts, alert pushes); `top_losses`, reports and status are staff only.
  Maintenance can ack/snooze/resolve and see all flats, but not use the valve. Old link files without a role/flat
  binding are ignored (re-link required).
- The bot talks to the backend as one service account, so resident scoping is enforced by the bot (the backend
  scopes per account, not per Telegram user); keep the bot host trusted.
- Roles are re-checked in `ToolRegistry.call` for every command and button press.

## Alerts and the poller

Backend states: `nudge, alert, ticket, auto_shutoff, acknowledged, snoozed, overridden, resolved`.
`alert`, `ticket` and `auto_shutoff` are pushed individually; `nudge` only as one digest message per poll. The dedupe
key is `<alert_id>:<state>` (so escalations are pushed again), marked only after at least one successful delivery,
persisted in `BOT_SEEN_FILE`. Per-chat failures (blocked bot, network) are logged and do not stop other chats. Poll
failures are logged with a traceback and retried with exponential backoff. On the very first start (no seen file) the
current backlog is recorded, not pushed. PTB has a global error handler that logs with traceback.

## Authentication and attribution

- **Tokens.** The bot logs in as the service user (`POST /auth/login`) and keeps the access token (15 min) and the
  rotating refresh token in memory only. It refreshes shortly before expiry and on a 401 (the request is retried
  once). Refresh is single-flight: concurrent callers share one `POST /auth/refresh`, and every response replaces
  BOTH tokens (a spent refresh token revokes the whole family). If a refresh fails for any reason, the bot logs in
  again with its credentials. Transport errors, 429 and 5xx are retried with jittered exponential backoff. Tokens
  and the password are never logged.
- **Attribution.** The backend records the authenticated service account as the actor and ignores body fields such
  as `acknowledged_by`, `issued_by`; the bot no longer sends them. It sends `on_behalf_of=telegram:<id>`
  (max 100 chars) on acknowledge, snooze, resolve, override and valve calls. The backend stores and echoes it as
  asserted, not verified (`on_behalf_of_verified: false`); the bot repeats the echoed value in its confirmations.
  The bot-side audit log (`bot.audit` logger, `BOT_AUDIT_FILE`) additionally keeps the claimed backend username.

## Safety wording and the shut-off protocol

- The bot only says water is shut off for an alert whose `state` is `auto_shutoff` (the backend sets it only after
  the valve device confirmed). A valve command that is not `confirmed` is reported as "NOT confirmed by the device".
  Backend alert messages that claim a shut-off in any other state are withheld.
- The poller (no websocket) derives the protocol events from the alert row: a `ticket` with `shutoff_warned_at` is a
  **shut-off warning** pushed with a countdown (residents: text; staff: Acknowledge button; admins also Override),
  and `shutoff_gave_up_at` is a **shut-off failed** push to staff ("close the valve manually"). Override needs an admin
  service account and an admin Telegram user.
- Device-status errors (e.g. unconfirmed close) are not exposed by `GET /flats/{id}` (only `valve_state`
  open/closed/unknown and `sensor_status`), so the bot does not push them.

## Tool registry / watsonx Orchestrate

`bot.tools.build_default_registry().get_tool_schemas(role=None)` returns function-calling definitions
(`{"type":"function","function":{name,description,parameters}}` plus an `x-required-role` hint). Dispatch tool calls
with `await registry.call(name, args, principal, api)`, which validates arguments and enforces role and flat scoping.
Tools: `get_usage_summary`, `list_active_alerts`, `acknowledge_alert`, `snooze_alert`, `resolve_alert`, `top_losses`,
`generate_report`, `system_status`, `set_valve`. State-changing tools should be gated by a human confirmation step.

## Backend endpoints used

All in `bot/api_client.py` (Bearer JWT, `/api/v1`): `POST /auth/login`, `POST /auth/refresh`,
`GET /alerts?state=&flat_id=&limit=`, `POST /alerts/{id}/{acknowledge,snooze,override,resolve}` (snooze:
`duration_minutes` 15/30/60/120), `GET /users/by-username/{u}`, `PATCH /users/{id}` (`telegram_id`), `GET /societies`,
`GET /societies/{id}/stats`, `GET /flats?society_id=&limit=&offset=` (cached 5 min; unit number <-> flat id), and
`POST /tools/{active-alerts, flat-usage (hours), top-losses (days, limit), valve-control, weekly-report}`.
The bot serves the first society returned by `/societies`. `tests/openapi.snapshot.json` is a snapshot of the backend
OpenAPI (regenerate: `cd backend && ENVIRONMENT=test python -c "from app.main import app_factory; import json;
print(json.dumps(app_factory().openapi()))" 2>/dev/null | grep '^{"openapi"' > /tmp/openapi.json`, then write it with
`json.dumps(..., indent=1, sort_keys=True)`); the test simulator checks every request against it (paths, params, bodies,
and that deprecated/ignored body fields are not sent).

## Creating the bot service account

1. Create the first admin with the backend CLI (password from a prompt or stdin, never on the command line):
   `python -m app.cli create-admin --username <admin> `.
2. Log in as that admin (`POST /api/v1/auth/login`) and create the service user with the admin API
   (`POST /api/v1/users`, body `{"username": "bot_service", "name": "Telegram bot", "password": "<12+ chars, letter
   and digit>", "role": "admin", "is_service": true}`). `is_service` is what allows `on_behalf_of` and
   `GET /users/by-username`.
3. Put the credentials in `.env` as `BOT_API_USERNAME` / `BOT_API_PASSWORD` (never commit them).

Role `admin` enables everything (valve control, override, writing `telegram_id`). Role `maintenance` also works for
reading, ack/snooze/resolve and the link lookup, but valve control, override and the `telegram_id` write-back are
refused by the backend.

## Smart mode: natural-language requests (optional, off by default)

With `BOT_NLU_ENABLED=true`, free text is routed by an Anthropic model using tool calling over the registry above.
Install `pip install -e "bot[llm]"`. Without the package, a key, or on any outage/limit, the bot tells the user
"Smart mode is unavailable" and uses the keyword intents.

| Variable | Default | Notes |
|---|---|---|
| `BOT_NLU_ENABLED` | `false` | Single flag that turns the feature (and all data sharing) off. |
| `ANTHROPIC_API_KEY` | unset | Environment only. |
| `LLM_MODEL` | `claude-haiku-5-5` | `claude-sonnet-5-5` optional. |
| `LLM_TIMEOUT_S` | `15` | Per model call. |
| `BOT_NLU_USER_PER_MIN` / `BOT_NLU_USER_PER_DAY` | `10` / `100` | Per Telegram user. |
| `BOT_NLU_GLOBAL_PER_DAY` | `1000` | Global budget of LLM requests per UTC day. |
| `BOT_NLU_MAX_ROUNDS` | `3` | Max tool-call rounds per message. |

**Safety model.** The model only sees tools allowed for the user's role (resident: read tools for their own flat,
no `unit` argument; maintenance adds ack/snooze/resolve; admin adds `set_valve`). Every `tool_use` is checked against
that list and the registry's argument validation; read tools run through `registry.call`, which re-checks role and
flat scope. Mutating tools are never executed by the model: they become inline-button confirmations (`valve:*` for
valves, `nlu:*` for alert actions), bound to user/chat/message, single-use, 60 s, and re-checked on press. Tool output
goes back as `<tool_result_data>` untrusted data; answers are trimmed (800 chars) and sent MarkdownV2-escaped.
Memory: last 6 turns per chat, in memory, 30 min TTL; bot/API tokens and key-like strings are redacted.
The audit log records `nlu_request`, `nlu_tool`, `nlu_proposal`, `nlu_tool_rejected`, `nlu_confirmed`, `nlu_fallback`
with a message hash and length only, never the message body.

**Data sent to the provider:** the user's message text (up to 500 chars), recent turns of that chat, the role (and
bound flat unit for residents), tool schemas, and tool results (alert texts, usage figures, loss tables, which can
contain flat unit labels). Not sent: Telegram ids/usernames, backend credentials or JWTs. Users may type personal data
into free text, so review your DPA/privacy notice before enabling. Costs depend on traffic and the model; limits above
cap calls, and no prices are stated here.
