"""Runtime wiring: broker + consumers + outbox relay + the action (trace) registry."""
from __future__ import annotations

import threading
import time
from collections import deque

from . import config, db, termlog
from .broker import Broker
from .consumers.event_writer import EventWriter
from .consumers.inventory_handler import InventoryHandler
from .consumers.ml_service import MLService
from .consumers.order_handler import OrderHandler
from .consumers.request_handler import RequestHandler
from .consumers.state_updater import StateUpdater
from .ml import models as ml_models
from .outbox import OutboxRelay


class Pipeline:
    def __init__(self, models=None, verbose: bool = False):
        self.broker = Broker()
        self.models = models or ml_models.load_or_train(verbose=verbose)
        self.consumers = [OrderHandler(self), EventWriter(self), StateUpdater(self), InventoryHandler(self),
                          RequestHandler(self), MLService(self)]
        self.relay = OutboxRelay(self)
        self._lock = threading.Lock()
        self._traces: deque = deque(maxlen=800)
        self._trace_seq = 0
        self._trace_no = self._initial_trace_no()
        self.started = False

    # -- lifecycle ---------------------------------------------------------
    def _initial_trace_no(self) -> int:
        try:
            with db.POOL.connection() as c:
                c.logging = False
                r = c.execute("SELECT COALESCE(MAX(substr(trace_id, 2)::int), 0) AS n FROM infra.audit_log "
                              "WHERE trace_id ~ '^T[0-9]+$'").fetchone()
                c.logging = True
            return int(r["n"])
        except Exception:
            return 0

    def start(self) -> None:
        if self.started:
            return
        for c in self.consumers:
            c.start()
        self.relay.start()
        self.started = True

    def stop(self) -> None:
        for c in self.consumers:
            c.stop()
        self.relay.stop()
        self.started = False

    # -- traces ("user actions") ------------------------------------------
    def new_trace(self, label: str, kind: str = "action", meta: dict | None = None) -> str:
        with self._lock:
            self._trace_no += 1
            self._trace_seq += 1
            tid = f"T{self._trace_no:04d}"
            self._traces.append({"seq": self._trace_seq, "trace_id": tid, "label": label, "kind": kind,
                                 "ts": time.time(), "meta": meta or {}})
        if kind not in ("seed", "simulation-quiet"):
            termlog.info(f"{label}", "white", tag="ACTION", actor=tid)
        return tid

    # -- publishing --------------------------------------------------------
    def publish(self, topic: str, key: str, value: dict, trace_id: str | None):
        return self.broker.publish(topic, key, value, trace_id=trace_id, headers={"from": value.get("producer", "app")})

    # -- synchronisation ---------------------------------------------------
    def flush_outbox(self) -> int:
        return self.relay.drain()

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Block until every consumer has processed everything published (lag 0, nobody busy)."""
        end = time.time() + timeout
        stable = 0
        while time.time() < end:
            if self.broker.total_lag() == 0 and not any(c.busy for c in self.consumers):
                stable += 1
                if stable >= 2:
                    return True
            else:
                stable = 0
            time.sleep(0.02)
        return False

    def pause(self) -> None:
        for c in [*self.consumers, self.relay]:
            c.paused.set()
        end = time.time() + 10
        while any(c.busy for c in [*self.consumers, self.relay]) and time.time() < end:
            time.sleep(0.01)

    def resume(self) -> None:
        for c in [*self.consumers, self.relay]:
            c.paused.clear()
        self.broker.wait(0)

    def reset_state(self) -> None:
        """Forget in-memory state (used after a database reset)."""
        for c in self.consumers:
            c.attempts.clear()
            for k in c.stats:
                c.stats[k] = 0
            if hasattr(c, "last_eta"):
                c.last_eta.clear()
                c.last_risk.clear()
        self._trace_no = 0

    def status(self) -> list[dict]:
        lag = {}
        for r in self.broker.lag_table():
            lag[r["group"]] = lag.get(r["group"], 0) + r["lag"]
        out = [{"group": c.group, "topics": c.topics, "alive": c.is_alive(), "busy": c.busy,
                "lag": lag.get(c.group, 0), "paused": c.paused.is_set(), "stats": dict(c.stats),
                "last_error": c.last_error} for c in self.consumers]
        out.insert(0, {"group": self.relay.group, "topics": ["infra.outbox"], "alive": self.relay.is_alive(),
                       "busy": self.relay.busy, "lag": 0, "paused": self.relay.paused.is_set(),
                       "stats": dict(self.relay.stats), "last_error": self.relay.last_error})
        return out
