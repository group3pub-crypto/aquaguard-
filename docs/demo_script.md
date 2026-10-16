# 3-minute demo

0:00 Run `make demo`. Explain: simulator publishes 6 flats at 60x speed through authenticated MQTT.

0:30 Open http://localhost:3000, log in as `admin` (password: `DEMO_ADMIN_PASSWORD` in `.env`). Show live flow for the society.

1:00 Trigger the leak scenario from the admin demo controls (demo endpoints are admin-only and exist only when `DEMO_MODE=true`). Point out the slow leak sits below any daily cap, yet continuous night flow stands out.

1:45 Watch the alert climb `watch -> alert -> ticket`. Open the alert, show detector evidence and acknowledge it (maintenance/admin only).

2:15 Log out, log in as a resident: show read-only access, controls hidden and rejected server-side.

2:40 As admin, close the leaking flat's valve (confirmation required). Optionally show the Telegram bot (`make up` with `--profile bot`): "which flats are leaking", Acknowledge button.

2:55 Close with limitations: models trained on synthetic data, not field-validated (`docs/limitations.md`).
