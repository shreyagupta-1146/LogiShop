"""The PostgreSQL SERVER's own log, made readable.

    python db_log.py            # data-changing statements, function calls and errors
    python db_log.py --all      # every statement the server receives (SELECT, BEGIN, COMMIT ...)

PostgreSQL is configured with log_statement = all and  log_line_prefix = '%m %u@%d [%a] ',
so for every statement it logs the time, the role, the database and the *application_name* of the
connection that sent it (web-api, state-updater, inventory-handler, ...).  This script just follows
`docker logs -f logistics-db`, joins each statement with its bound parameters and colours it.
Nothing here comes from the application - this is what the DATABASE saw.
"""
import argparse
import os
import re
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
if os.name == "nt":
    os.system("")

R, B, D = "\x1b[0m", "\x1b[1m", "\x1b[2m"
ACTOR = {"web-api": "\x1b[36m", "order-handler": "\x1b[38;5;214m", "event-writer": "\x1b[32m", "state-updater": "\x1b[33m",
         "inventory-handler": "\x1b[35m", "request-handler": "\x1b[34m", "ml-service": "\x1b[97m", "outbox-relay": "\x1b[90m",
         "replay": "\x1b[91m", "retention-job": "\x1b[91m", "demo_viewer": "\x1b[96m"}
HEAD = re.compile(r"^(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d\.\d+) \w+ (\S+)@(\S+) \[([^\]]*)\] (LOG|ERROR|WARNING|FATAL|DETAIL|STATEMENT|HINT|CONTEXT):\s+(.*)$")
KW = re.compile(r"\b(INSERT INTO|VALUES|UPDATE|SET|DELETE FROM|SELECT|FROM|WHERE|ON CONFLICT|DO NOTHING|DO UPDATE|RETURNING|JOIN|LEFT JOIN|ORDER BY|LIMIT)\b")
WRITE = re.compile(r"^\s*(INSERT|UPDATE|DELETE|TRUNCATE|CALL)\b|^\s*SELECT\s+(shop|logistics|infra)\.\w+\(", re.I)
NOISE = re.compile(r"pg_stat_activity|pg_notify|set_config|pg_catalog|^\s*SET\b|^\s*LISTEN\b", re.I)


def fill(sql: str, params: str) -> str:
    """Replace $1, $2 ... by the logged parameter values."""
    vals = dict(re.findall(r"\$(\d+) = ((?:'(?:[^']|'')*')|NULL|[^,]+)", params))
    return re.sub(r"\$(\d+)", lambda m: vals.get(m.group(1), m.group(0)), sql)


def show(entry, show_all):
    if not entry:
        return
    ts, user, db, app, level, msg, detail = entry
    m = re.match(r"(?:statement|execute [^:]*):\s*(.*)$", msg, re.S)
    if level == "LOG" and m:
        sql = " ".join(fill(m.group(1), detail).split())
        if NOISE.search(sql) or (not show_all and "infra.processed_event" in sql):      # the idempotency inbox: noise unless --all
            return
        write = bool(WRITE.match(sql))
        if not write and not show_all:
            return
        text = sql if len(sql) < 330 else sql[:327] + "..."
        col = "\x1b[33m" if write else "\x1b[34m"
        text = KW.sub(lambda k: f"{B}{col}{k.group(1)}{R}", text)
        print(f"{D}{ts}{R}  {ACTOR.get(app or user, D)}{(app or user):<17}{R} {text}")
    elif level in ("ERROR", "FATAL", "WARNING"):
        print(f"{D}{ts}{R}  {ACTOR.get(app or user, D)}{(app or user):<17}{R} \x1b[1;31m{level}: {msg}{R}" + (f"  {D}{detail}{R}" if detail else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="show every statement")
    ap.add_argument("--container", default="logistics-db")
    ap.add_argument("--tail", default="0")
    a = ap.parse_args()
    print(f"{B}PostgreSQL server log ({a.container}){R} - {'every statement' if a.all else 'writes, function calls and errors'} - Ctrl+C to stop\n")
    p = subprocess.Popen(["docker", "logs", "-f", "--tail", a.tail, a.container], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace", bufsize=1)
    entry = None
    try:
        for line in p.stdout:
            line = line.rstrip("\n")
            h = HEAD.match(line)
            if h:
                day, ts, user, db, app, level, msg = h.groups()
                if level in ("DETAIL", "STATEMENT", "HINT", "CONTEXT") and entry:
                    e = list(entry)
                    e[6] = (e[6] + " " + msg) if level == "DETAIL" else e[6] + f" [{level.lower()}: {msg}]"
                    entry = tuple(e)
                    continue
                show(entry, a.all)
                entry = (ts, user, db, app, level, msg, "")
            elif entry and line.startswith(("\t", " ")):
                e = list(entry); e[5] = e[5] + "\n" + line.strip(); entry = tuple(e)
        show(entry, a.all)
    except KeyboardInterrupt:
        pass
    finally:
        p.terminate()


if __name__ == "__main__":
    main()
