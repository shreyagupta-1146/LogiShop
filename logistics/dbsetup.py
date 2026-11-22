"""Create (or recreate) the database objects.

    python -m logistics.dbsetup              # drop and recreate the schemas of database "logistics"  (ERASES all data!)
    python -m logistics.dbsetup --db logistics_test

DANGER: this wipes the tables. From the command line it refuses to touch a database that holds data unless you pass --force.

Runs sql/00..04 as the PostgreSQL superuser; every object ends up owned by `logistics_owner`.
"""
from __future__ import annotations

import argparse
import sys
import time

import psycopg

from . import config, db

FILES = ["00_bootstrap.sql", "01_schema.sql", "02_functions_triggers.sql", "03_views.sql", "04_grants.sql"]


def wait_for_server(timeout: float = 60.0) -> None:
    end = time.time() + timeout
    err = None
    while time.time() < end:
        try:
            psycopg.connect(**db.dsn(config.PG_SUPER_USER, config.PG_SUPER_PASSWORD, "postgres"), connect_timeout=3).close()
            return
        except Exception as e:                      # container still starting
            err = e
            time.sleep(1)
    raise SystemExit(f"PostgreSQL is not reachable on {config.PG_HOST}:{config.PG_PORT} ({err}).\n"
                     f"Start it with:  docker compose -f deploy/docker-compose.yml up -d db")


def ensure_database(name: str) -> None:
    with psycopg.connect(**db.dsn(config.PG_SUPER_USER, config.PG_SUPER_PASSWORD, "postgres"), autocommit=True) as c:
        if not c.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            c.execute(f'CREATE DATABASE "{name}"')


def is_empty(dbname: str | None = None) -> bool:
    """True if the database does not exist yet, has no schema, or holds no customers (i.e. nothing worth keeping).

    Asked as the superuser, so a transient problem with the application's own login can never be mistaken for 'empty'."""
    dbname = dbname or config.PG_DB
    wait_for_server()
    with psycopg.connect(**db.dsn(config.PG_SUPER_USER, config.PG_SUPER_PASSWORD, "postgres"), autocommit=True) as c:
        if not c.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)).fetchone():
            return True
    with psycopg.connect(**db.dsn(config.PG_SUPER_USER, config.PG_SUPER_PASSWORD, dbname), autocommit=True) as c:
        if c.execute("SELECT to_regclass('shop.customer') IS NULL").fetchone()[0]:
            return True
        return c.execute("SELECT count(*) FROM shop.customer").fetchone()[0] == 0


def setup(dbname: str | None = None, quiet: bool = False) -> None:
    dbname = dbname or config.PG_DB
    wait_for_server()
    ensure_database(dbname)
    conn = db.connect("dbsetup", user=config.PG_SUPER_USER, password=config.PG_SUPER_PASSWORD, dbname=dbname, log=False)
    try:
        for f in FILES:
            t0 = time.time()
            conn.execute((config.SQL_DIR / f).read_text(encoding="utf-8"))
            if not quiet:
                print(f"  {f:<28} ok  ({(time.time() - t0) * 1000:.0f} ms)")
    finally:
        conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--force", action="store_true", help="erase a database that already contains data")
    a = ap.parse_args()
    if not a.force and not is_empty(a.db):
        raise SystemExit(f"Refusing to run: database '{a.db or config.PG_DB}' already contains data and this command ERASES all of it\n"
                         "(it drops and recreates every table). Nothing was changed.\n"
                         "If you really want a clean database use the back office > System > 'Reset demo database' button\n"
                         "(it also reloads the demo data), or run this command again with --force.")
    print(f"Creating schemas in PostgreSQL {config.PG_HOST}:{config.PG_PORT}/{a.db or config.PG_DB}")
    setup(a.db)
    print("done.")
