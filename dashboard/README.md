# AquaGuard Dashboard

Next.js 16 (App Router, React 19) + TypeScript + Tailwind + Recharts. Talks to the AquaGuard backend (`/api/v1`, JWT) and its live WebSocket feed.

## Run

```bash
cp .env.example .env.local   # AQUAGUARD_API_URL, AQUAGUARD_WS_URL, AQUAGUARD_BACKEND_TZ
npm install
npm run dev                  # http://localhost:3000
```

Scripts: `npm run lint`, `npx tsc --noEmit`, `npm test` (Vitest), `npm run build` (standalone output).

Docker: `docker build -t aquaguard-dashboard .` then `docker run -e AQUAGUARD_API_URL=https://api.example.com -e AQUAGUARD_BACKEND_TZ=Asia/Kolkata -p 3000:3000 aquaguard-dashboard`. URLs and timezone are read at runtime (injected by the root layout, also used for the CSP `connect-src`), so one image works behind any domain; runs as non-root on port 3000.

Backend timezone: baseline slots are hour-of-week with Monday=0 in the backend's `TIMEZONE` setting, which the API does not report. Set `AQUAGUARD_BACKEND_TZ` (or `NEXT_PUBLIC_BACKEND_TZ`) to the same IANA name (default `UTC`), otherwise the baseline band on the flow chart is shifted.

## Layout

- `src/lib` - typed API client (timeouts, `ApiError`, 401 handling), WS type guards, types, permissions, chart helpers
- `src/context` - `AuthProvider` (token in memory + sessionStorage, never localStorage) and `LiveProvider` (WebSocket merge)
- `src/hooks` - `useWebSocket` (1s-30s backoff with jitter, max retries), `useAlerts`, `useFlatData`, `usePolling`
- `src/views` + `src/app` - Overview, Flat detail, Alerts, Login/Register

## Notes

- Role gating in the UI (admin: valves/override; maintenance: ack/snooze/resolve; resident: read-only) is a convenience; the backend enforces authorisation.
- Colours are CSS variables in `globals.css` with dark-mode overrides (`prefers-color-scheme` and `[data-theme]`); Tailwind references them.
- Security headers: X-Frame-Options, nosniff, Referrer-Policy in `next.config.js`; the Content-Security-Policy is built per request in `src/middleware.ts` with a script nonce (no `script-src 'unsafe-inline'`). `style-src` keeps `'unsafe-inline'` because React/Recharts emit inline `style` attributes that cannot carry a nonce. Next 16 deprecates the `middleware` filename in favour of `proxy`; it still works and the build prints a notice.
- Alerts: states are the backend's `nudge|alert|ticket|auto_shutoff|acknowledged|snoozed|overridden|resolved`. `GET /alerts` has no "open" filter and rejects unknown `state` values with 422, so "open" is computed client-side (everything except `overridden`/`resolved`). Action buttons mirror the backend transition table.
- WebSocket auth: the JWT is currently sent as `?token=` on `/api/v1/ws/live`. This is isolated in `buildWsUrl()` in `src/lib/wsAuth.ts`; when the backend offers a short-lived ticket, only that function changes. A close with code 4401 clears the session and redirects to `/login` (no retry with the stale token).
- Contract tests: `src/lib/openapi.snapshot.json` is the backend's OpenAPI document; `npm run snapshot:openapi` regenerates it (needs the backend's Python deps). `src/lib/contract.test.ts` checks every path/method/query/body the API client uses and validates `src/test/fixtures.ts` against the response schemas.
