# AquaGuard Dashboard

Next.js 14 (App Router) + TypeScript + Tailwind + Recharts. Talks to the AquaGuard backend (`/api/v1`, JWT) and its live WebSocket feed.

## Run

```bash
cp .env.example .env.local   # NEXT_PUBLIC_API_URL, NEXT_PUBLIC_WS_URL
npm install
npm run dev                  # http://localhost:3000
```

Scripts: `npm run lint`, `npx tsc --noEmit`, `npm test` (Vitest), `npm run build` (standalone output).

Docker: `docker build --build-arg NEXT_PUBLIC_API_URL=https://api.example.com --build-arg NEXT_PUBLIC_WS_URL=wss://api.example.com -t aquaguard-dashboard .` (NEXT_PUBLIC_* are inlined at build time; runs as non-root on port 3000).

## Layout

- `src/lib` - typed API client (timeouts, `ApiError`, 401 handling), WS type guards, types, permissions, chart helpers
- `src/context` - `AuthProvider` (token in memory + sessionStorage, never localStorage) and `LiveProvider` (WebSocket merge)
- `src/hooks` - `useWebSocket` (1s-30s backoff with jitter, max retries), `useAlerts`, `useFlatData`, `usePolling`
- `src/views` + `src/app` - Overview, Flat detail, Alerts, Demo (admin), Login/Register

## Notes

- Role gating in the UI (admin: valves/override/demo; maintenance: ack/snooze/resolve; resident: read-only) is a convenience; the backend enforces authorisation.
- Colours are CSS variables in `globals.css` with dark-mode overrides (`prefers-color-scheme` and `[data-theme]`); Tailwind references them.
- Security headers (CSP, X-Frame-Options, nosniff, Referrer-Policy) are set in `next.config.js`.
