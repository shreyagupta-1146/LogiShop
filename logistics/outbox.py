"""Transactional outbox relay.

Checkout writes the order AND an `infra.outbox` row in ONE PostgreSQL transaction
(function shop.place_order).  This relay then publishes the outbox rows to Kafka:

    LISTEN outbox                     -- PostgreSQL pushes a notification when a row is committed
    SELECT ... FOR UPDATE SKIP LOCKED -- claim pending rows (several relays could run side by side)
    publish to the broker, then UPDATE ... SET published_at = now()

If the process dies after publishing but before the UPDATE, the row is published again: that is
the at-least-once guarantee, and the consumers' idempotency inbox makes the duplicate harmless.
"""
from __future__ import annotations

import threading
import time
import traceback

from . import db, termlog


class OutboxRelay(threading.Thread):
    group = "outbox-relay"

    def __init__(self, pipeline):
        super().__init__(daemon=True, name=self.group)
        self.pipeline = pipeline
        self.stop_flag = threading.Event()
        self.paused = threading.Event()
        self.busy = False
        self.stats = {"handled": 0, "duplicates": 0, "retries": 0, "soft_fails": 0, "dead": 0}
        self.last_error = ""
        self._drain_lock = threading.Lock()
        self.conn = None

    def drain(self) -> int:
        """Publish every pending outbox row; returns how many were published."""
        with self._drain_lock:
            if self.conn is None or self.conn.closed:
                self.conn = db.connect(self.group)
            conn = self.conn
            n = 0
            while True:
                with db.write_tx(conn, self.group):
                    rows = conn.execute(
                        "SELECT outbox_id, topic, msg_key, payload, trace_id FROM infra.outbox "
                        "WHERE published_at IS NULL ORDER BY outbox_id LIMIT 50 FOR UPDATE SKIP LOCKED").fetchall()
                    if not rows:
                        return n
                    for r in rows:
                        self.pipeline.broker.publish(r["topic"], r["msg_key"], r["payload"], trace_id=r["trace_id"],
                                                     headers={"from": self.group})
                    conn.execute("UPDATE infra.outbox SET published_at = now() WHERE outbox_id = ANY(?)",
                                 ([r["outbox_id"] for r in rows],))
                    n += len(rows)
                    self.stats["handled"] += len(rows)

    def run(self) -> None:
        listener = db.connect(self.group, log=False)
        listener.execute("LISTEN outbox")
        while not self.stop_flag.is_set():
            if self.paused.is_set():
                time.sleep(0.05)
                continue
            self.busy = True
            try:
                self.drain()
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                traceback.print_exc()
                time.sleep(0.5)
            finally:
                self.busy = False
            try:
                for _ in listener.notifies(timeout=1.0, stop_after=1):     # sleep until PostgreSQL says "new outbox row"
                    break
            except Exception:
                time.sleep(0.2)
        listener.close()

    def stop(self) -> None:
        self.stop_flag.set()
