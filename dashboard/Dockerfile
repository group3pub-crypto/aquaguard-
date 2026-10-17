# syntax=docker/dockerfile:1
# Runtime configuration (no build args): AQUAGUARD_API_URL, AQUAGUARD_WS_URL, AQUAGUARD_BACKEND_TZ
# are read per request, so ONE image works behind any domain.
#
# Pin the base by digest for reproducible builds: `make pin-digests` prints the sha256 for the tag
# below; then build with --build-arg NODE_IMAGE=node:22.14.0-alpine3.21@sha256:<digest>.
ARG NODE_IMAGE=node:22.14.0-alpine3.21

FROM ${NODE_IMAGE} AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM ${NODE_IMAGE} AS builder
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM ${NODE_IMAGE} AS runner
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0 \
    AQUAGUARD_BACKEND_TZ=UTC
RUN addgroup -S -g 1001 nodejs && adduser -S -u 1001 -G nodejs nextjs
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
RUN mkdir -p public && chown nextjs:nodejs public
USER nextjs
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
  CMD wget -qO /dev/null http://127.0.0.1:3000/login || exit 1
CMD ["node", "server.js"]
