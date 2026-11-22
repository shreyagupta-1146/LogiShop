-- =====================================================================
--  00  BOOTSTRAP  (run as the PostgreSQL superuser, in database "logistics")
--
--  Roles, extensions and the three schemas.  Re-runnable: it drops and recreates
--  the schemas (this is what "Reset demo" does); roles and extensions are kept.
--
--  Role model (least privilege, like a real deployment):
--     postgres         superuser - administration only, never used by the application
--     logistics_owner  owns every object, runs DDL / migrations
--     app_user         the application: DML on its tables, cannot change or delete the
--                      append-only logs, cannot write the audit log directly, no DDL
--     demo_viewer      read-only (pgAdmin / psql during the presentation)
-- =====================================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'logistics_owner') THEN
        CREATE ROLE logistics_owner LOGIN PASSWORD 'owner';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_user') THEN
        CREATE ROLE app_user LOGIN PASSWORD 'app';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'demo_viewer') THEN
        CREATE ROLE demo_viewer LOGIN PASSWORD 'demo';
    END IF;
END $$;

ALTER ROLE logistics_owner SET search_path = shop, logistics, infra, public;
ALTER ROLE app_user        SET search_path = shop, logistics, infra, public;
ALTER ROLE demo_viewer     SET search_path = shop, logistics, infra, public;
ALTER ROLE app_user        SET timezone = 'UTC';
ALTER ROLE demo_viewer     SET timezone = 'Asia/Kolkata';       -- the viewer sees local (IST) times
GRANT pg_read_all_stats TO demo_viewer;                           -- lets the viewer read pg_stat_statements / pg_stat_activity

DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO app_user, demo_viewer, logistics_owner', current_database());
END $$;

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;   -- which queries ran how often / how slowly
CREATE EXTENSION IF NOT EXISTS citext;               -- case-insensitive e-mail addresses
CREATE EXTENSION IF NOT EXISTS pg_trgm;              -- fuzzy product search

DROP SCHEMA IF EXISTS shop CASCADE;
DROP SCHEMA IF EXISTS logistics CASCADE;
DROP SCHEMA IF EXISTS infra CASCADE;

CREATE SCHEMA shop      AUTHORIZATION logistics_owner;   -- catalogue, customers, carts, orders, payments
CREATE SCHEMA logistics AUTHORIZATION logistics_owner;   -- warehouses, shipments, events, GPS, inventory, ML
CREATE SCHEMA infra     AUTHORIZATION logistics_owner;   -- audit log, outbox, idempotency inbox, sessions

COMMENT ON SCHEMA shop      IS 'Storefront: catalogue, customers, carts, orders, payments';
COMMENT ON SCHEMA logistics IS 'Fulfilment: event-sourced shipments, inventory ledger, GPS, ML predictions';
COMMENT ON SCHEMA infra     IS 'Plumbing: audit log, transactional outbox, consumer inbox, sessions';
