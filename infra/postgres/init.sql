-- Runs once on first database initialisation.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- TimescaleDB is optional: only enabled if the server ships it.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb') THEN
    CREATE EXTENSION IF NOT EXISTS timescaledb;
  ELSE
    RAISE NOTICE 'timescaledb not available; using plain PostgreSQL tables';
  END IF;
END
$$;
