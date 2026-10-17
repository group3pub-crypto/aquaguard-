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
| `BOT_API_BASE_URL` / `BOT_API_USERNAME` / `BOT_API_PASSWORD` | backend service account (must be admin: ack/snooze/resolve need staff, valve-control needs admin) |
| `BOT_STATE_FILE` | JSON file with approved links (atomic writes, mode 0600) |
| `BOT_SEEN_FILE` | delivered-alert ids (default `seen_alerts.json` beside `BOT_STATE_FILE`); without a state dir, dedupe is in-memory only |
| `BOT_AUDIT_FILE` | bot-side audit log, JSON lines (default `audit.jsonl` beside `BOT_STATE_FILE`) |
| `BOT_POLL_INTERVAL` | alert poll period in seconds (default 30; backs off up to 300 s on failure) |

## Commands

`/start`, `/link <backend_username>`, `/status`, `/alerts`, `/usage [unit] [hours]`; staff: `/report`, `/losses [days]`;
admin: `/valve <unit> open|close` (inline Confirm, valid 60 s, single use, only for the originating user, chat and
message), `/approve <request_id> <flat_unit|maintenance>`, `/reject <request_id>`, `/revoke <telegram_id>`.
Free text such as "which flats are leaking" or "top losses" is matched by regex rules and routed through the registry.

## Authorization model

- Admins = ids in `TELEGRAM_ADMIN_IDS`. Anyone else runs `/link <backend_username>` (rate limited to 3 per hour,
  at most 50 pending requests, 24 h TTL). An admin approves with `/approve <request_id> <flat_unit>` (resident, bound
  to that flat) or `/approve <request_id> maintenance` (staff). The claimed username is NOT verified against the
  backend (no lookup endpoint); the admin vouches for it. Nobody can become admin through the bot.
- Residents see only their bound flat (usage, alerts, alert pushes); `top_losses`, reports and status are staff only.
  Maintenance can ack/snooze/resolve and see all flats, but not use the valve. Old link files without a role/flat
  binding are ignored (re-link required).
- The bot talks to the backend as one admin service account, so this scoping is enforced by the bot only. Backend-side
  scoping of resident tokens is separate work; keep the bot host trusted.
- Roles are re-checked in `ToolRegistry.call` for every command and button press.

## Alerts and the poller

Backend states: `nudge, alert, ticket, auto_shutoff, acknowledged, snoozed, overridden, resolved`.
`alert`, `ticket` and `auto_shutoff` are pushed individually; `nudge` only as one digest message per poll. The dedupe
key is `<alert_id>:<state>` (so escalations are pushed again), marked only after at least one successful delivery,
persisted in `BOT_SEEN_FILE`. Per-chat failures (blocked bot, network) are logged and do not stop other chats. Poll
failures are logged with a traceback and retried with exponential backoff. On the very first start (no seen file) the
current backlog is recorded, not pushed. PTB has a global error handler that logs with traceback.

## Actor attribution (important)

The backend ignores `acknowledged_by` / `snoozed_by` in request bodies and records the **service account** for
acknowledge, snooze and resolve. The human behind the Telegram action is therefore recorded only in the bot-side audit
log (`bot.audit` logger and `BOT_AUDIT_FILE`), not in the backend audit. The exception is valve commands: the bot sends
`issued_by=telegram:<id>/<username>`, which the backend stores in `ValveCommand.issued_by`.

## Tool registry / watsonx Orchestrate

`bot.tools.build_default_registry().get_tool_schemas(role=None)` returns function-calling definitions
(`{"type":"function","function":{name,description,parameters}}` plus an `x-required-role` hint). Dispatch tool calls
with `await registry.call(name, args, principal, api)`, which validates arguments and enforces role and flat scoping.
Tools: `get_usage_summary`, `list_active_alerts`, `acknowledge_alert`, `snooze_alert`, `resolve_alert`, `top_losses`,
`generate_report`, `system_status`, `set_valve`. State-changing tools should be gated by a human confirmation step.

## Backend endpoints used

All in `bot/api_client.py` (Bearer JWT, `/api/v1`): `POST /auth/login`, `GET /alerts?state=&flat_id=&limit=`,
`POST /alerts/{id}/{acknowledge,snooze,resolve}` (snooze: `duration_minutes` 15/30/60/120), `GET /societies`,
`GET /societies/{id}/stats`, `GET /flats?society_id=&limit=&offset=` (cached 5 min; unit number <-> flat id), and
`POST /tools/{active-alerts, flat-usage (hours), top-losses (days, limit), valve-control, weekly-report}`.
The bot serves the first society returned by `/societies`. `tests/openapi.snapshot.json` is a snapshot of the backend
OpenAPI (regenerate: `cd backend && ENVIRONMENT=test python -c "from app.main import app_factory; import json;
json.dump(app_factory().openapi(), open('../bot/tests/openapi.snapshot.json','w'), indent=1, sort_keys=True)"`);
tests check request paths, params and bodies against it.

## Creating the bot service account

Register a user (e.g. `bot_service`) via the API/dashboard, then promote it with
`PUT /api/v1/auth/users/{id}/role` to `admin`. Put its credentials in `BOT_API_USERNAME` / `BOT_API_PASSWORD`.

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
