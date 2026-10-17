# AquaGuard Dashboard

Next.js 16 (App Router, React 19) + TypeScript + Tailwind + Recharts. Talks to the AquaGuard backend (`/api/v1`, JWT) and its live WebSocket feed.

## Run

```bash
cp .env.example .env.local   # AQUAGUARD_API_URL, AQUAGUARD_WS_URL, AQUAGUARD_BACKEND_TZ
npm install
npm run dev                  # http://localhost:3000
```

Runtime env (read per request, also feeds the CSP): `AQUAGUARD_API_URL`, `AQUAGUARD_WS_URL`, `AQUAGUARD_BACKEND_TZ`, `DASHBOARD_ALLOW_SELF_REGISTRATION`, `SHUTOFF_WARNING_SECONDS`.

Scripts: `npm run lint`, `npx tsc --noEmit`, `npm test` (Vitest), `npm run build` (standalone output).

Docker: `docker build -t aquaguard-dashboard .` then `docker run -e AQUAGUARD_API_URL=https://api.example.com -e AQUAGUARD_BACKEND_TZ=Asia/Kolkata -p 3000:3000 aquaguard-dashboard`. URLs and timezone are read at runtime (injected by the root layout, also used for the CSP `connect-src`), so one image works behind any domain; runs as non-root on port 3000.

Backend timezone: baseline slots are hour-of-week with Monday=0 in the backend's `TIMEZONE` setting, which the API does not report. Set `AQUAGUARD_BACKEND_TZ` (or `NEXT_PUBLIC_BACKEND_TZ`) to the same IANA name (default `UTC`), otherwise the baseline band on the flow chart is shifted.

## Layout

- `src/lib` - typed API client (timeouts, `ApiError`, silent refresh), WS type guards, types, permissions, validation (mirrors backend constraints), shut-off countdown helpers
- `src/context` - `AuthProvider` (tokens in memory + sessionStorage, never localStorage) and `LiveProvider` (WebSocket merge)
- `src/hooks` - `useWebSocket` (ticket per connect, close-code handling, 1s-30s backoff with jitter), `useAlerts`, `useFlatData`, `usePolling`
- `src/views` + `src/app` - Overview, Flat detail, Alerts, Account (change password), Login/Register, and `views/admin/*` (Societies, Flats, Users)

## Notes

- Role gating in the UI (admin: valves/override; maintenance: ack/snooze/resolve; resident: read-only) is a convenience; the backend enforces authorisation.
- Colours are CSS variables in `globals.css` with dark-mode overrides (`prefers-color-scheme` and `[data-theme]`); Tailwind references them.
- Security headers: X-Frame-Options, nosniff, Referrer-Policy in `next.config.js`; the Content-Security-Policy is built per request in `src/middleware.ts` with a script nonce (no `script-src 'unsafe-inline'`). `style-src` keeps `'unsafe-inline'` because React/Recharts emit inline `style` attributes that cannot carry a nonce. Next 16 deprecates the `middleware` filename in favour of `proxy`; it still works and the build prints a notice.
- Alerts: states are the backend's `nudge|alert|ticket|auto_shutoff|acknowledged|snoozed|overridden|resolved`. `GET /alerts` has no "open" filter and rejects unknown `state` values with 422, so "open" is computed client-side (everything except `overridden`/`resolved`). Action buttons mirror the backend transition table.
### Authentication and sessions

- Login returns a 15-minute `access_token` (JWT with a `tv` claim) plus a rotating `refresh_token`. `src/lib/api.ts` refreshes silently: shortly before expiry (30 s skew) and once on a 401, then retries the original request exactly once. Refresh is **single-flight** (concurrent requests share one `POST /auth/refresh`; a parallel second call would present a spent token and revoke the whole family). The **new** refresh token is stored after every refresh (rotation). A definitive 4xx from `/auth/refresh` (expired, revoked, reuse detected) is treated as logout; network errors, 5xx and 429 are transient and keep the session.
- Logout calls `POST /auth/logout` (best effort) and clears local state. Change password (`/account`) posts to `/auth/change-password`, enforces the policy hints (at least 8 characters, a letter and a digit, at most 72 bytes) and stores the fresh token pair the server returns (all other sessions are signed out).
- Storage tradeoff (`src/lib/session.ts`): tokens live in module memory and are mirrored to `sessionStorage` so a reload keeps the session. Not `localStorage` (survives restarts, shared across tabs). `sessionStorage` is readable by script, so an XSS bug could steal tokens; mitigations are the strict nonce-based CSP, no raw HTML rendering and refresh-token rotation with reuse detection. An httpOnly cookie for the refresh token would be stronger but needs backend support. Tokens are never logged or put in URLs.

### Live feed (WebSocket)

- No JWT in the URL. Before **every** connect and reconnect `buildWsUrl()` (`src/lib/wsAuth.ts`, async) calls `POST /auth/ws-ticket` and connects to `/api/v1/ws/live?ticket=...` (single use, 30 s).
- Close codes: `4401` refreshes the token and reconnects once, otherwise logs out; `4403` (no data scope) shows a banner and never retries; `4429` (connection cap) and `4408` (idle) show a banner and retry with backoff. Server `{"type":"ping"}` frames are answered with a `ping` text frame. Frames are scope-filtered server-side; client type guards still validate everything, and `sensor` frames update device status.
- Unbound accounts do not connect at all.

### Access, scoping and admin

- Residents only receive their own flat; maintenance a society; admins everything. A resident without a flat (or maintenance without a society) sees "Your account is not linked to a home yet - contact your administrator". Out-of-scope ids answer 404 and render "Not found"; empty lists are normal.
- `/register` is 404 on the backend unless `ALLOW_SELF_REGISTRATION=true`. The login screen only links to it when the runtime flag `DASHBOARD_ALLOW_SELF_REGISTRATION` (or `ALLOW_SELF_REGISTRATION`) is truthy and no 404 has been seen; direct visits to `/register` handle a 404 with a friendly "Registration is closed" page.
- Admin screens (`/admin/societies`, `/admin/flats`, `/admin/users`; admin only, the backend enforces it too): forms validate against the backend constraints with labelled fields and `aria-describedby` errors, show the server `detail` on failure, never update optimistically, and confirm deletes/deactivation/password reset/logout-all. Reset password generates a random temporary password in the browser, sends it, and shows it once with a copy button. The "last active admin" 409 gets a friendly message.
- Flat page: when `GET /flats/{id}` includes `device` (fw, vbat, tot, valve_reported, err[]), it renders with human labels; `close_unconf` is a prominent warning. The panel is hidden when the backend sends no `device`.
- Alert wording: "water shut off" is only ever shown for the confirmed `auto_shutoff` state (`safeAlertMessage` withholds such claims otherwise). While `shutoff_warned_at` is set (state still `ticket`) a countdown banner (`SHUTOFF_WARNING_SECONDS`, default 300, runtime env) highlights Acknowledge/Override; `shutoff_gave_up_at` shows a "not confirmed, check manually" warning. `message_source` is shown subtly; "AI-written" only for `anthropic`.

- Contract tests: `src/lib/openapi.snapshot.json` is the backend's OpenAPI document; `npm run snapshot:openapi` regenerates it (needs the backend's Python deps). `src/lib/contract.test.ts` checks every path/method/query/body the API client uses and validates `src/test/fixtures.ts` against the response schemas.
