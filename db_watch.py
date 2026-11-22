"""Live feed of every row the application changes in the database, as it happens.

    python db_watch.py                       # everything
    python db_watch.py --hide ml_prediction gps_ping     # leave out noisy tables
    python db_watch.py --only shop           # only schema / table names containing "shop"
    python db_watch.py --since 15            # first show the last 15 changes

How it works (all inside PostgreSQL):
  * an AFTER INSERT/UPDATE/DELETE trigger on every table writes a row to infra.audit_log
    (with the component and the user action that caused it) and calls pg_notify('db_changes', id)
  * this script does  LISTEN db_changes  - PostgreSQL pushes a message at COMMIT time
  * it then reads the new audit rows and prints them: old -> new values

It connects as the read-only role demo_viewer, so it cannot change anything.
"""
import argparse
import os
import sys
import time

import psycopg
from psycopg.rows import dict_row

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
if os.name == "nt":
    os.system("")

HOST = os.environ.get("PGHOST", "127.0.0.1")
PORT = int(os.environ.get("PGPORT", "5433"))
DB = os.environ.get("PGDATABASE", "logistics")
USER, PASSWORD = "demo_viewer", os.environ.get("DEMO_VIEWER_PASSWORD", "demo")

R, B, D = "\x1b[0m", "\x1b[1m", "\x1b[2m"
COL = {"INSERT": "\x1b[32m", "UPDATE": "\x1b[33m", "DELETE": "\x1b[31m"}
SIGN = {"INSERT": "+ INSERT", "UPDATE": "~ UPDATE", "DELETE": "- DELETE"}
ACTOR = {"web-api": "\x1b[36m", "order-handler": "\x1b[38;5;214m", "event-writer": "\x1b[32m", "state-updater": "\x1b[33m",
         "inventory-handler": "\x1b[35m", "request-handler": "\x1b[34m", "ml-service": "\x1b[97m", "outbox-relay": "\x1b[90m"}


def short(v, n=46):
    if v is None:
        return "NULL"
    s = str(v) if not isinstance(v, (dict, list)) else str(v).replace("'", '"')
    return s if len(s) <= n else s[: n - 1] + "…"


def render(row) -> str:
    ts = row["ts"].astimezone().strftime("%H:%M:%S.%f")[:-3]
    who = row["actor"] + (f" ({row['trace_id']})" if row["trace_id"] else "")
    head = (f"{D}{ts}{R}  {COL[row['op']]}{B}{SIGN[row['op']]:<8}{R} {B}{row['table_name']:<28}{R} "
            f"{row['pk'] or '':<22} {D}by{R} {ACTOR.get(row['actor'], D)}{who}{R}")
    old, new = row["old_row"], row["new_row"]
    if row["op"] == "UPDATE":
        diff = [f"{k}: {D}{short(old.get(k))}{R} {B}→{R} {COL['UPDATE']}{short(new[k])}{R}" for k in new if new[k] != old.get(k)]
        body = "      " + ("   ".join(diff) if diff else f"{D}(no column changed){R}")
    else:
        r = new if row["op"] == "INSERT" else old
        body = "      " + f"{D}" + "  ".join(f"{k}={short(v, 28)}" for k, v in list(r.items()) if v is not None)[:230] + R
    return head + "\n" + body


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hide", nargs="*", default=[], help="table name fragments to hide")
    ap.add_argument("--only", nargs="*", default=[], help="show only tables containing one of these fragments")
    ap.add_argument("--since", type=int, default=0, help="print the last N changes first")
    a = ap.parse_args()
    try:
        conn = psycopg.connect(host=HOST, port=PORT, dbname=DB, user=USER, password=PASSWORD, autocommit=True, row_factory=dict_row)
    except Exception as e:
        sys.exit(f"cannot connect to PostgreSQL at {HOST}:{PORT} ({e}).\nStart it with: docker compose -f deploy/docker-compose.yml up -d")
    conn.execute("LISTEN db_changes")
    last = conn.execute("SELECT COALESCE(max(change_id), 0) AS m FROM infra.audit_log").fetchone()["m"]
    since = max(0, last - a.since) if a.since else last

    def wanted(r):
        t = r["table_name"]
        return not any(h in t for h in a.hide) and (not a.only or any(o in t for o in a.only))

    print(f"{B}Watching {DB} on {HOST}:{PORT} as {USER} (LISTEN db_changes) - Ctrl+C to stop{R}")
    print(f"{D}every INSERT / UPDATE / DELETE made by the application (or by anyone else) appears here the moment it is committed{R}\n")
    cursor = since
    while True:
        rows = conn.execute("SELECT * FROM infra.audit_log WHERE change_id > %s ORDER BY change_id", (cursor,)).fetchall()
        for r in rows:
            cursor = r["change_id"]
            if wanted(r):
                print(render(r))
        try:
            for _ in conn.notifies(timeout=2.0, stop_after=1):
                break
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
