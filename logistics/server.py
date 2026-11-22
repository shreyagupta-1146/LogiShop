"""HTTP/JSON server (standard library only, bound to localhost): routing, sessions, static files.

The route handlers - and the SQL behind every screen - are in logistics/api.py.
"""
from __future__ import annotations

import http.cookies
import json
import mimetypes
import os
import re
import socket
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import auth, config, db, dbsetup, seed, termlog
from .pipeline import Pipeline
from .producers import App

ROUTES: list[tuple[str, re.Pattern, callable, str | None]] = []
COOKIE = "lt_session"


def route(method: str, pattern: str, auth: str | None = None):
    """auth: None (public) | 'customer' | 'staff' | 'admin' | 'driver'"""
    rx = re.compile("^" + pattern + "$")

    def deco(fn):
        ROUTES.append((method, rx, fn, auth))
        return fn
    return deco


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class Ctx:
    def __init__(self):
        self.pipeline = Pipeline(verbose=True)
        self.app = App(self.pipeline)
        self.sims: dict[str, threading.Event] = {}
        self.state = {"seeding": False, "message": ""}


CTX: Ctx | None = None


class Req:
    def __init__(self, method, path, query, body, params, headers):
        self.method, self.path, self.query, self.body, self.params, self.headers = method, path, query, body, params, headers
        self.user: dict | None = None
        self.set_cookie: str | None = None
        self.clear_cookie = False

    def trace(self, label: str) -> str:
        who = f"{self.user['name']}: " if self.user else ""
        return CTX.pipeline.new_trace(who + label)


def current_user(headers) -> dict | None:
    raw = headers.get("Cookie")
    if not raw:
        return None
    jar = http.cookies.SimpleCookie()
    try:
        jar.load(raw)
    except Exception:
        return None
    if COOKIE not in jar:
        return None
    with db.POOL.connection() as c:
        c.logging = False
        row = c.execute(
            "SELECT s.subject_type, s.subject_id, COALESCE(cu.name, st.name) AS name, st.role, st.vehicle_id "
            "FROM infra.auth_session s "
            "LEFT JOIN shop.customer cu ON s.subject_type = 'CUSTOMER' AND cu.customer_id = s.subject_id "
            "LEFT JOIN logistics.staff_user st ON s.subject_type = 'STAFF' AND st.staff_id = s.subject_id "
            "WHERE s.token_hash = ? AND s.expires_at > now()", (auth.token_hash(jar[COOKIE].value),)).fetchone()
        c.logging = True
    if row is None:
        return None
    return {"type": row["subject_type"], "id": row["subject_id"], "name": row["name"],
            "role": row["role"] or "CUSTOMER", "vehicle_id": row["vehicle_id"]}


def _allowed(user: dict | None, need: str | None) -> tuple[bool, int]:
    if need is None:
        return True, 200
    if user is None:
        return False, 401
    ok = {"customer": user["type"] == "CUSTOMER", "staff": user["type"] == "STAFF",
          "admin": user["role"] == "ADMIN", "driver": user["role"] == "DRIVER"}[need]
    return ok, 403


class Handler(BaseHTTPRequestHandler):
    server_version = "LogiTrack/2.0"

    def log_message(self, fmt, *args):      # we log ourselves, in color
        pass

    def _send(self, status: int, payload, ctype="application/json; charset=utf-8", cookies: list[str] | None = None):
        data = payload if isinstance(payload, (bytes, bytearray)) else json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for ck in cookies or []:
            self.send_header("Set-Cookie", ck)
        self.end_headers()
        self.wfile.write(data)
        return status

    def _dispatch(self, method: str):
        t0 = time.perf_counter()
        u = urlparse(self.path)
        path = unquote(u.path)
        if method == "GET" and not path.startswith("/api/"):
            return self._static(path)
        if path.startswith("/api/delta"):            # endpoint of an older UI that a stale browser tab may still be polling
            return self._send(410, {"error": "retired endpoint - reload the page"})
        q = {k: v[-1] for k, v in parse_qs(u.query).items()}
        body = {}
        if method == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                try:
                    body = json.loads(self.rfile.read(n).decode("utf-8"))
                except Exception:
                    return self._send(400, {"error": "invalid JSON body"})
        status, who = 404, ""
        try:
            for m, rx, fn, need in ROUTES:
                if m != method:
                    continue
                mt = rx.match(path)
                if not mt:
                    continue
                req = Req(method, path, q, body, mt.groupdict(), self.headers)
                req.user = current_user(self.headers)
                who = req.user["name"] if req.user else ""
                ok, code = _allowed(req.user, need)
                if not ok:
                    status = self._send(code, {"error": "please sign in" if code == 401 else "not allowed for this account"})
                    break
                try:
                    res = fn(req)
                    cookies = []
                    if req.set_cookie:
                        cookies.append(f"{COOKIE}={req.set_cookie}; Path=/; HttpOnly; SameSite=Lax; Max-Age={auth.SESSION_HOURS * 3600}")
                    if req.clear_cookie:
                        cookies.append(f"{COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
                    status = self._send(200, res, cookies=cookies)
                except ApiError as e:
                    status = self._send(e.status, {"error": e.message})
                except KeyError as e:
                    status = self._send(400, {"error": f"missing field {e}"})
                except db.Error as e:           # business rule raised inside PostgreSQL (SQLSTATE SHxxx / 23514 ...)
                    msg = (e.diag.message_primary or str(e)).strip()
                    status = self._send(400 if (e.sqlstate or "").startswith(("SH", "23", "P0")) else 500, {"error": msg})
                except Exception as e:
                    traceback.print_exc()
                    status = self._send(500, {"error": f"{type(e).__name__}: {e}"})
                break
            else:
                status = self._send(404, {"error": "not found"})
        finally:
            quiet_poll = self.headers.get("X-Poll") == "1" and termlog.HTTP_LEVEL != "all"
            if not quiet_poll:
                termlog.http(method, path + (("?" + u.query) if u.query else ""), status, (time.perf_counter() - t0) * 1000, who)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _static(self, path: str):
        rel = {"/": "index.html", "/admin": "admin.html", "/driver": "driver.html"}.get(path, path.lstrip("/"))
        f = (config.WEB_DIR / rel).resolve()
        if config.WEB_DIR.resolve() not in f.parents or not f.is_file():
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, f.read_bytes(), ctype)


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


def reset_and_seed(history: int = 8):
    ctx = CTX
    ctx.state.update(seeding=True, message="Recreating the database and loading demo data ...")
    termlog.info("resetting the database (DROP SCHEMA ... CASCADE, recreate, reload demo data)", "yellow", tag="ADMIN")
    try:
        for ev in ctx.sims.values():
            ev.set()
        seed.full_reset(ctx.pipeline, ctx.app, history=history)
    except Exception:
        traceback.print_exc()
    finally:
        ctx.state.update(seeding=False, message="")
        termlog.info("demo data ready", "green", tag="ADMIN")


def main(history: int = 8) -> None:
    global CTX
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    # A second copy would fight the first over the port, the broker file and the database - stop before touching anything.
    with socket.socket() as probe:
        if probe.connect_ex((config.HOST, config.PORT)) == 0:
            raise SystemExit(f"LogiShop is already running on http://{config.HOST}:{config.PORT} - not starting a second copy.\n"
                             "Use that one, or close its window / run .\\scripts\\stop_all.ps1 first. The database was not touched.")
    dbsetup.wait_for_server()
    fresh = dbsetup.is_empty()          # decided as the superuser: only a really empty database is (re)created and seeded
    if fresh:
        termlog.info("empty database -> creating schemas", tag="APP")
        dbsetup.setup(quiet=True)
        for suffix in ('', '-wal', '-shm'):
            type(config.BROKER_PATH)(str(config.BROKER_PATH) + suffix).unlink(missing_ok=True)
    CTX = Ctx()
    from . import api  # noqa: F401  (registers the routes)
    CTX.pipeline.start()
    if fresh:
        CTX.state.update(seeding=True, message="Loading demo data ...")
        t0 = time.time()
        seed.seed_demo(CTX.pipeline, CTX.app, history=history, verbose=True)
        CTX.pipeline.wait_idle(60)
        CTX.state.update(seeding=False, message="")
        termlog.info(f"demo data ready in {time.time() - t0:.1f}s", "green", tag="APP")
    srv = QuietServer((config.HOST, config.PORT), Handler)
    url = f"http://{config.HOST}:{config.PORT}"
    termlog.banner([
        "=" * 72,
        f"  Shop (customers)   {url}/",
        f"  Back office        {url}/admin        admin@logistics.local / admin123",
        f"  Driver app         {url}/driver       ramesh@logistics.local / driver123",
        f"  Demo customer      asha.rao@example.com / demo123",
        f"  PostgreSQL         {config.PG_HOST}:{config.PG_PORT}  db={config.PG_DB}  (docker container logistics-db)",
        f"  Kafka stand-in     {config.BROKER_PATH}",
        "  This terminal shows every HTTP request, SQL statement, Kafka message and consumer decision.",
        "=" * 72])
    if not os.environ.get("NO_BROWSER"):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        termlog.info("shutting down", tag="APP")
    finally:
        CTX.pipeline.stop()
        srv.server_close()
