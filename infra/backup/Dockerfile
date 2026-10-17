# syntax=docker/dockerfile:1
# Backup runner: pg_dump 16 + age. Pin with `make pin-digests`.
ARG POSTGRES_ALPINE_IMAGE=postgres:16.6-alpine3.21
FROM ${POSTGRES_ALPINE_IMAGE}
RUN apk add --no-cache age tzdata \
 && adduser -D -u 10002 backup \
 && mkdir /backups && chown backup /backups
COPY backup.sh restore.sh /usr/local/bin/
USER backup
VOLUME ["/backups"]
ENTRYPOINT ["/usr/local/bin/backup.sh"]
CMD ["daemon"]
