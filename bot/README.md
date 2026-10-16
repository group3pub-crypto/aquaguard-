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
| `BOT_API_BASE_URL` / `BOT_API_USERNAME` / `BOT_API_PASSWORD` | backend service account (needs admin or maintenance rights for ack/snooze/valve) |
| `BOT_STATE_FILE` | JSON file persisting approved links (optional) |
| `BOT_POLL_INTERVAL` | alert poll period in seconds (default 30) |

## Commands

`/start`, `/link <username>`, `/status`, `/alerts`, `/report`, `/valve <unit> open|close`
(admin; always needs an inline "Confirm" press). Free text such as "which flats are leaking"
or "top losses" is matched by simple regex rules and routed through the tool registry.

## Authorization model

- Admins = ids in `TELEGRAM_ADMIN_IDS`. Anyone else must `/link`; an admin approves via an
  inline button. Linked users are residents only; the bot cannot grant higher roles.
- Roles: resident < maintenance < admin. Every tool call, including button callbacks, is
  role-checked at the registry (`ToolRegistry.call`), never trusting callback data.
- Note: the bot acts on the backend as one service account. Per-user roles are enforced by
  the bot, not by the backend, so keep the bot host trusted.
- Outbound sends are limited to 25 msg/s, text is MarkdownV2-escaped, httpx logging is
  silenced so the token (part of Telegram API URLs) is not logged.

## Tool registry / watsonx Orchestrate

`bot.tools.build_default_registry().get_tool_schemas(role=None)` returns function-calling
definitions (`{"type":"function","function":{name,description,parameters}}` plus an
`x-required-role` hint). Feed these to IBM watsonx Orchestrate / BOB as tool specs and
dispatch the model's tool calls with `await registry.call(name, args, role, api)`, which
validates arguments against the schema and enforces the role. Tools: `get_usage_summary`,
`list_active_alerts`, `acknowledge_alert`, `snooze_alert`, `top_losses`, `set_valve`,
`generate_report`. A state-changing tool such as `set_valve` should always be gated by a
human confirmation step in the orchestrator.

## Assumed backend endpoints

All in `bot/api_client.py`: `POST /api/v1/auth/login`, `GET /api/v1/alerts?state=`,
`GET /api/v1/usage/summary`, `GET /api/v1/losses/top`, `GET /api/v1/reports/summary`,
`POST /api/v1/alerts/{id}/ack|snooze`, `POST /api/v1/units/{unit}/valve`. Adjust that one
file if the backend paths differ.
