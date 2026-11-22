"""Readable, colored terminal log of everything the system does.

One line per event, in the order it happens:

    10:42:07.123  HTTP   POST /api/checkout -> 201 (84 ms)   Asha Rao
    10:42:07.040  SQL    web-api            SELECT shop.place_order('CUS-7', 3, 'UPI')            2.1 ms
    10:42:07.044  KAFKA  >> publish orders[1]@4  ORDER_PLACED  key=ORD-1002   (T0007)
    10:42:07.060  KAFKA  << order-handler  orders[1]@4 ORDER_PLACED
    10:42:07.071  SQL    order-handler      INSERT INTO ...

Verbosity:  LOG_SQL = writes (default: INSERT / UPDATE / DELETE / stored-function calls / rollbacks)
                     tx     (+ BEGIN, COMMIT, SAVEPOINT and the idempotency-inbox inserts)
                     all    (+ every SELECT)      off
            LOG_HTTP = api (default) | all | off
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time

if os.name == "nt":
    os.system("")                                    # enable ANSI escape sequences in the Windows console

_LOCK = threading.Lock()
SQL_LEVEL = os.environ.get("LOG_SQL", "writes").lower()
HTTP_LEVEL = os.environ.get("LOG_HTTP", "api").lower()
COLOR = not os.environ.get("NO_COLOR")

RESET, BOLD, DIM = "\x1b[0m", "\x1b[1m", "\x1b[2m"
C = {"cyan": "\x1b[36m", "blue": "\x1b[34m", "yellow": "\x1b[33m", "green": "\x1b[32m", "magenta": "\x1b[35m",
     "red": "\x1b[31m", "white": "\x1b[97m", "grey": "\x1b[90m", "orange": "\x1b[38;5;214m"}

ACTOR_COLORS = {"web-api": "cyan", "order-handler": "orange", "event-writer": "green", "state-updater": "yellow",
                "inventory-handler": "magenta", "request-handler": "blue", "ml-service": "white", "outbox-relay": "grey"}


def _c(text: str, color: str, bold: bool = False) -> str:
    if not COLOR:
        return text
    return f"{BOLD if bold else ''}{C.get(color, '')}{text}{RESET}"


def _ts() -> str:
    t = time.time()
    return time.strftime("%H:%M:%S", time.localtime(t)) + f".{int(t * 1000) % 1000:03d}"


QUIET = False


def set_quiet(flag: bool) -> None:
    """Silence the per-statement / per-message lines (used while bulk-loading demo data)."""
    global QUIET
    QUIET = flag


def _out(tag: str, color: str, actor: str, text: str, tail: str = "") -> None:
    if QUIET and tag not in ("SEED", "WARN", "APP"):
        return
    a = _c(f"{actor:<17}", ACTOR_COLORS.get(actor, "grey")) if actor else " " * 17
    line = f"{_c(_ts(), 'grey')}  {_c(f'{tag:<6}', color, True)} {a} {text}" + (f"  {_c(tail, 'grey')}" if tail else "")
    with _LOCK:
        try:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        except Exception:
            pass


# ---------------------------------------------------------------------------
_WRITE = re.compile(r"^\s*(INSERT|UPDATE|DELETE|BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE|TRUNCATE)\b", re.I)
_CALL = re.compile(r"^\s*SELECT\s+(shop|logistics|infra)\.\w+\(", re.I)
_HIDE = re.compile(r"set_config|pg_notify|^\s*SET\b|^\s*LISTEN\b", re.I)
_KW = re.compile(r"\b(INSERT INTO|VALUES|UPDATE|SET|DELETE FROM|SELECT|FROM|WHERE|ON CONFLICT|DO NOTHING|DO UPDATE|RETURNING|BEGIN|COMMIT|ROLLBACK)\b")


_TXN = re.compile(r"^\s*(BEGIN|COMMIT|SAVEPOINT|RELEASE)\b", re.I)
_ROLLBACK = re.compile(r"^\s*ROLLBACK\b", re.I)


def sql(actor: str, text: str, ms: float, rowcount: int | None = None) -> None:
    if SQL_LEVEL == "off" or _HIDE.search(text):
        return
    is_txn = bool(_TXN.match(text)) or "infra.processed_event" in text
    write = bool(_WRITE.match(text)) or bool(_CALL.match(text))
    if is_txn and SQL_LEVEL not in ("tx", "all"):
        return
    if not write and SQL_LEVEL != "all":
        return
    t = " ".join(text.split())
    if len(t) > 230:
        t = t[:227] + "..."
    hot = write and not _ROLLBACK.match(text)
    if COLOR:
        color = "red" if _ROLLBACK.match(text) else ("yellow" if hot else "blue")
        t = _KW.sub(lambda m: f"{BOLD}{C[color]}{m.group(1)}{RESET}", t)
    note = "   <- this message's changes are undone" if _ROLLBACK.match(text) else ""
    tail = f"{ms:.1f} ms" + (f", {rowcount} row(s)" if rowcount is not None and rowcount >= 0 and write and not is_txn else "")
    _out("SQL", "red" if _ROLLBACK.match(text) else ("yellow" if hot else "blue"), actor, t + note, tail)


def http(method: str, path: str, status: int, ms: float, who: str = "") -> None:
    if HTTP_LEVEL == "off":
        return
    color = "green" if status < 400 else "red"
    _out("HTTP", "cyan", "web-api", f"{_c(method, 'white', True)} {path} {_c('-> ' + str(status), color, True)}", f"{ms:.0f} ms  {who}")


def kafka_publish(topic: str, part: int, offset: int, key: str, etype: str, trace: str | None, source: str) -> None:
    _out("KAFKA", "green", source, f">> publish {_c(f'{topic}[{part}]@{offset}', 'green', True)}  {etype}  key={key}",
         f"({trace})" if trace else "")


def kafka_receive(group: str, topic: str, part: int, offset: int, etype: str, trace: str | None) -> None:
    _out("KAFKA", "green", group, f"<< consume {topic}[{part}]@{offset}  {_c(etype, 'white', True)}", f"({trace})" if trace else "")


def kafka_result(group: str, text: str, bad: bool = False) -> None:
    _out("KAFKA", "red" if bad else "green", group, ("   x " if bad else "   ok ") + text)


def info(text: str, color: str = "white", tag: str = "APP", actor: str = "") -> None:
    _out(tag, color, actor, text)


def warn(text: str, actor: str = "") -> None:
    _out("WARN", "red", actor, _c(text, "red"))


def banner(lines: list[str]) -> None:
    with _LOCK:
        sys.stdout.write("\n" + "\n".join(lines) + "\n\n")
        sys.stdout.flush()
