"""Base class shared by the consumer programs.

Delivery semantics (spec: "at-least-once delivery with idempotent consumers"):

  1. poll a batch of messages from the broker (offsets are NOT committed yet)
  2. BEGIN  -- one PostgreSQL transaction for the batch
  3. per message: SAVEPOINT, record event_id in infra.processed_event (the inbox),
     run handle(); on success RELEASE, on failure ROLLBACK TO the savepoint
  4. COMMIT, publish any follow-up messages, and only then commit the offsets

If the process dies between COMMIT and the offset commit the broker redelivers
the message; the inbox row makes the second delivery a no-op.
"""
from __future__ import annotations

import threading
import time
import traceback

from .. import db, termlog
from ..broker import Message

MAX_RETRIES = 60            # x RETRY_SLEEP seconds before a stuck message is dead-lettered
RETRY_SLEEP = 0.05
BATCH = 25


class Retry(Exception):
    """A dependency (e.g. the shipment row) does not exist yet: try again later."""


class Poison(Exception):
    """The message can never be processed: send it to the dead-letter table."""


class SoftFail(Exception):
    """Business-level refusal: undo this message's DB effects but treat it as handled.

    `outbound` is a list of (topic, key, value) messages to publish (e.g. an EVENT_REJECTED).
    """

    def __init__(self, note: str, outbound: list | None = None):
        super().__init__(note)
        self.note = note
        self.outbound = outbound or []


class Consumer(threading.Thread):
    group = "?"
    topics: list[str] = []

    def __init__(self, pipeline):
        super().__init__(daemon=True, name=self.group)
        self.pipeline = pipeline
        self.broker = pipeline.broker
        self.models = pipeline.models
        self.stop_flag = threading.Event()
        self.paused = threading.Event()
        self.busy = False
        self.conn: db.Connection | None = None
        self.stats = {"handled": 0, "duplicates": 0, "retries": 0, "soft_fails": 0, "dead": 0}
        self.attempts: dict[tuple, int] = {}
        self.last_error = ""

    # -- to override -------------------------------------------------------
    def handle(self, conn: db.Connection, msg: Message) -> list[tuple[str, str, dict]]:
        raise NotImplementedError

    def prepare(self, conn: db.Connection, msgs: list[Message]) -> None:
        """Optional hook, called once per polled batch inside the transaction, before the
        messages are handled one by one (used by the ML service for vectorised scoring)."""

    # -- helpers -----------------------------------------------------------
    def _note(self, msg: Message, status: str) -> None:
        """status == 'received' opens the message block in the log; anything else is its outcome."""
        et = msg.value.get("event_type", "?")
        if status == "received":
            termlog.kafka_receive(self.group, msg.topic, msg.part, msg.offset, et, msg.trace_id)
        else:
            termlog.kafka_result(self.group, status, bad=bool(__import__("re").search(r"REJECT|DEAD|DUPLICATE|waiting", status)))

    def _mark_processed(self, conn, event_id: str) -> bool:
        cur = conn.execute(
            "INSERT INTO infra.processed_event (consumer, event_id) VALUES (?, ?) "
            "ON CONFLICT (consumer, event_id) DO NOTHING", (self.group, event_id))
        return cur.rowcount == 1

    # -- main loop ---------------------------------------------------------
    def run(self) -> None:
        self.conn = db.connect(self.group)
        self.broker.subscribe(self.group, self.topics)
        while not self.stop_flag.is_set():
            if self.paused.is_set():
                time.sleep(0.05)
                continue
            try:
                msgs = self.broker.poll(self.group, self.topics, BATCH)
            except Exception as e:      # pragma: no cover
                self.last_error = f"poll: {e}"
                time.sleep(0.3)
                continue
            if not msgs:
                self.broker.wait(0.2)
                continue
            self.busy = True
            try:
                self._process_batch(msgs)
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                traceback.print_exc()
                if self.conn is None or self.conn.closed:
                    try:
                        self.conn = db.connect(self.group)
                    except Exception:
                        pass
                time.sleep(0.3)
            finally:
                self.busy = False

    def _process_batch(self, msgs: list[Message]) -> None:
        conn = self.conn
        outbound: list[tuple[str, str, dict, str | None]] = []
        positions: dict[tuple[str, int], int] = {}
        dead: list[tuple[Message, str]] = []
        retry_wait = False

        with db.write_tx(conn, self.group, msgs[0].trace_id):
            try:
                self.prepare(conn, msgs)
            except db.OperationalError:
                raise
            except Exception:                      # an optimisation hook must never block the stream
                traceback.print_exc()
            for m in msgs:
                conn.trace_id = m.trace_id
                conn.execute("SELECT set_config('app.trace_id', ?, true)", (m.trace_id or "",))
                key = (m.topic, m.part, m.offset)
                conn.execute("SAVEPOINT msg")
                try:
                    self._note(m, "received")
                    event_id = m.value.get("event_id") or f"{m.topic}:{m.part}:{m.offset}"
                    if not self._mark_processed(conn, event_id):
                        conn.execute("RELEASE SAVEPOINT msg")
                        self.stats["duplicates"] += 1
                        self._note(m, "DUPLICATE ignored (already in infra.processed_event)")
                    else:
                        out = self.handle(conn, m) or []
                        conn.execute("RELEASE SAVEPOINT msg")
                        outbound.extend((t, k, v, m.trace_id) for t, k, v in out)
                        self.stats["handled"] += 1
                        self._note(m, "applied")
                    positions[(m.topic, m.part)] = m.offset + 1
                    self.attempts.pop(key, None)
                except SoftFail as sf:
                    conn.execute("ROLLBACK TO SAVEPOINT msg")
                    conn.execute("RELEASE SAVEPOINT msg")
                    self._mark_processed(conn, m.value.get("event_id") or f"{m.topic}:{m.part}:{m.offset}")
                    outbound.extend((t, k, v, m.trace_id) for t, k, v in sf.outbound)
                    self.stats["soft_fails"] += 1
                    self._note(m, f"REJECTED, changes rolled back ({sf.note})")
                    positions[(m.topic, m.part)] = m.offset + 1
                except (Retry, db.IntegrityError) as e:
                    conn.execute("ROLLBACK TO SAVEPOINT msg")
                    conn.execute("RELEASE SAVEPOINT msg")
                    fk = isinstance(e, (Retry, db.ForeignKeyViolation))
                    if fk:
                        n = self.attempts.get(key, 0) + 1
                        self.attempts[key] = n
                        self.stats["retries"] += 1
                        if n <= MAX_RETRIES:
                            self._note(m, f"waiting for dependency ({str(e).splitlines()[0]}) - retry {n}")
                            retry_wait = True
                            break               # keep order: stop the batch, retry this message later
                        dead.append((m, f"dependency never appeared: {e}"))
                    else:
                        dead.append((m, f"{type(e).__name__}: {str(e).splitlines()[0]}"))
                    self._note(m, f"DEAD-LETTERED ({dead[-1][1]})")
                    positions[(m.topic, m.part)] = m.offset + 1
                except Poison as e:
                    conn.execute("ROLLBACK TO SAVEPOINT msg")
                    conn.execute("RELEASE SAVEPOINT msg")
                    dead.append((m, str(e)))
                    self._note(m, f"DEAD-LETTERED ({e})")
                    positions[(m.topic, m.part)] = m.offset + 1
                except db.OperationalError:
                    raise                        # connection loss, deadlock...: abort the batch, retry everything
                except Exception as e:
                    conn.execute("ROLLBACK TO SAVEPOINT msg")
                    conn.execute("RELEASE SAVEPOINT msg")
                    dead.append((m, f"{type(e).__name__}: {str(e).splitlines()[0]}"))
                    self.last_error = f"{type(e).__name__}: {e}"
                    traceback.print_exc()
                    self._note(m, f"DEAD-LETTERED ({type(e).__name__}: {str(e).splitlines()[0]})")
                    positions[(m.topic, m.part)] = m.offset + 1
        conn.trace_id = None            # after COMMIT, so the COMMIT statement is attributed to the last message

        # transaction committed -> publish follow-ups, then commit offsets
        for topic, key, value, trace in outbound:
            self.broker.publish(topic, key, value, trace_id=trace, headers={"from": self.group})
        for m, err in dead:
            self.stats["dead"] += 1
            self.broker.dead_letter(self.group, m, err)
        if positions:
            self.broker.commit(self.group, positions)
        if retry_wait:
            time.sleep(RETRY_SLEEP)

    def stop(self) -> None:
        self.stop_flag.set()
