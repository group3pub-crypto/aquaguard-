-- Role model (runs ONCE, on first initialisation of the data directory, as the bootstrap
-- superuser, via 10-roles.sh). Role names and passwords come from the environment of the
-- postgres container through psql's \getenv, so no secret is ever on a command line.
--
--   POSTGRES_USER (aquaguard_admin)  superuser: bootstrap, break-glass and restores. NOT used by
--                                    any service.
--   DB_OWNER_USER (aquaguard_owner)  owns the database and every table; used ONLY by the
--                                    one-shot `db-migrate` service (alembic upgrade head).
--   DB_APP_USER   (aquaguard_app)    runtime role of the backend: SELECT/INSERT/UPDATE/DELETE on
--                                    tables and USAGE on sequences; no DDL, no TRUNCATE, no
--                                    extensions, not a member of any other role.
--   DB_BACKUP_USER (aquaguard_backup) read-only (pg_read_all_data) for pg_dump.
--
-- Tables are created later by the owner (migrations); ALTER DEFAULT PRIVILEGES hands the app
-- role its rights on every future table/sequence the owner creates.

\set ON_ERROR_STOP on
\getenv db POSTGRES_DB
\getenv owner DB_OWNER_USER
\getenv owner_pw DB_OWNER_PASSWORD
\getenv app DB_APP_USER
\getenv app_pw DB_APP_PASSWORD
\getenv bkp DB_BACKUP_USER
\getenv bkp_pw DB_BACKUP_PASSWORD

CREATE ROLE :"owner" LOGIN PASSWORD :'owner_pw'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 5;
CREATE ROLE :"app" LOGIN PASSWORD :'app_pw'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 60;
CREATE ROLE :"bkp" LOGIN PASSWORD :'bkp_pw'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 3;
GRANT pg_read_all_data TO :"bkp";

ALTER ROLE :"app" SET idle_in_transaction_session_timeout = '120s';

ALTER DATABASE :"db" OWNER TO :"owner";
REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db" TO :"app", :"bkp";

ALTER SCHEMA public OWNER TO :"owner";
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO :"app", :"bkp";

ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO :"app";
