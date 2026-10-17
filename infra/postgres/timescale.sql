-- OPTIONAL - UNTESTED (no TimescaleDB binaries were available when this was written).
--
-- Converts flow_readings into a TimescaleDB hypertable with compression and retention policies.
-- Use with docker-compose.timescale.yml; run ONCE as the superuser AFTER `db-migrate`:
--   docker compose exec -T postgres psql -U aquaguard_admin -d aquaguard < infra/postgres/timescale.sql
--
-- !! BLOCKER FOR A DROP-IN SWITCH !!  A hypertable's unique indexes must include the partition
-- !! column (timestamp). The primary key (id) and the constraint uq_flow_readings_flat_message
-- !! (flat_id, message_id) therefore have to become (id, timestamp) / (flat_id, message_id,
-- !! timestamp). The backend de-duplicates with
-- !!   INSERT ... ON CONFLICT (flat_id, message_id) DO NOTHING
-- !! (app/repositories/reading.py, app/services/batcher.py). After this migration PostgreSQL
-- !! rejects that statement ("no unique or exclusion constraint matching the ON CONFLICT
-- !! specification"), so ingest would fail until the backend uses a bare ON CONFLICT DO NOTHING
-- !! (which then only de-duplicates exact (flat_id, message_id, timestamp) triples) AND the
-- !! migrations/models are updated. Do not apply this to a production system before that
-- !! change exists and has been tested.
--
-- Also: alembic's autogenerate does not know hypertables; keep this file as the single place
-- that owns them and re-run policy statements idempotently (if_not_exists / if_exists).

CREATE EXTENSION IF NOT EXISTS timescaledb;

BEGIN;
ALTER TABLE flow_readings DROP CONSTRAINT IF EXISTS flow_readings_pkey;
ALTER TABLE flow_readings DROP CONSTRAINT IF EXISTS uq_flow_readings_flat_message;
ALTER TABLE flow_readings ADD PRIMARY KEY (id, "timestamp");
ALTER TABLE flow_readings
  ADD CONSTRAINT uq_flow_readings_flat_message_ts UNIQUE (flat_id, message_id, "timestamp");
COMMIT;

SELECT create_hypertable('flow_readings', 'timestamp',
                         chunk_time_interval => INTERVAL '7 days',
                         migrate_data => true, if_not_exists => true);

ALTER TABLE flow_readings SET (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'flat_id',
  timescaledb.compress_orderby = '"timestamp" DESC'
);

-- compress chunks older than 7 days; drop chunks older than 90 days (keep in step with
-- READINGS_RETENTION_DAYS, and set that to 0 so the application does not also delete rows)
SELECT add_compression_policy('flow_readings', INTERVAL '7 days', if_not_exists => true);
SELECT add_retention_policy('flow_readings', INTERVAL '90 days', if_not_exists => true);
