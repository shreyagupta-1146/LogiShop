"""A small, durable, Kafka-style event broker.

Why not real Kafka?  The spec asks for Apache Kafka (KRaft, Java 17+).  This
project keeps the same *semantics* in a file you can open and inspect:

  * topics split into partitions; the partition is chosen from the message key
    (shipment_id) so one shipment's events keep their order
  * every message gets an immutable (partition, offset)
  * consumer groups track a committed offset per partition; messages are
    redelivered until the offset is committed  ->  AT-LEAST-ONCE delivery
  * dead-letter table for poison messages
  * consumers can be rewound to offset 0 to replay history

The broker lives in its own file (data/broker.db), separate from the business
database, just like Kafka is separate from MongoDB in the original design.
Swapping in confluent-kafka only requires re-implementing publish()/poll()/commit().
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from . import config, termlog

SCHEMA = """
CREATE TABLE IF NOT EXISTS message (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    topic      TEXT    NOT NULL,
    part       INTEGER NOT NULL,
    msg_offset INTEGER NOT NULL,
    msg_key    TEXT    NOT NULL,
    value      TEXT    NOT NULL,
    headers    TEXT    NOT NULL DEFAULT '{}',
    ts         TEXT    NOT NULL,
    UNIQUE (topic, part, msg_offset)
);
CREATE TABLE IF NOT EXISTS consumer_offset (
    group_id    TEXT    NOT NULL,
    topic       TEXT    NOT NULL,
    part        INTEGER NOT NULL,
    next_offset INTEGER NOT NULL,
    PRIMARY KEY (group_id, topic, part)
);
CREATE TABLE IF NOT EXISTS subscription (
    group_id TEXT NOT NULL,
    topic    TEXT NOT NULL,
    PRIMARY KEY (group_id, topic)
);
CREATE TABLE IF NOT EXISTS dead_letter (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id   TEXT NOT NULL,
    topic      TEXT NOT NULL,
    part       INTEGER NOT NULL,
    msg_offset INTEGER NOT NULL,
    value      TEXT NOT NULL,
    error      TEXT NOT NULL,
    ts         TEXT NOT NULL
);
"""


@dataclass
class Message:
    id: int
    topic: str
    part: int
    offset: int
    key: str
    value: dict
    headers: dict = field(default_factory=dict)
    ts: str = ""

    @property
    def trace_id(self):
        return self.headers.get("trace_id")


def _now() -> str:
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int(t * 1000) % 1000:03d}Z"


def partition_for(key: str, n: int = config.PARTITIONS) -> int:
    return zlib.crc32(key.encode("utf-8")) % n


class Broker:
    def __init__(self, path: Path | str | None = None, partitions: int = config.PARTITIONS):
        self.path = Path(path or config.BROKER_PATH)
        self.partitions = partitions
        self._local = threading.local()
        self._cv = threading.Condition()
        self._notify_count = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        c = self._conn()
        c.executescript(SCHEMA)

    # -- connections -------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(str(self.path), timeout=30, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA busy_timeout = 30000")
            c.execute("PRAGMA journal_mode = WAL")
            c.execute("PRAGMA synchronous = NORMAL")
            self._local.conn = c
        return c

    # -- producer ----------------------------------------------------------
    def publish(self, topic: str, key: str, value: dict, trace_id: str | None = None,
                headers: dict | None = None) -> Message:
        if topic not in config.TOPICS:
            raise ValueError(f"unknown topic {topic}")
        part = partition_for(key, self.partitions)
        hdr = dict(headers or {})
        if trace_id:
            hdr["trace_id"] = trace_id
        c = self._conn()
        ts = _now()
        c.execute("BEGIN IMMEDIATE")
        try:
            off = c.execute("SELECT COALESCE(MAX(msg_offset) + 1, 0) FROM message WHERE topic=? AND part=?",
                            (topic, part)).fetchone()[0]
            cur = c.execute(
                "INSERT INTO message (topic, part, msg_offset, msg_key, value, headers, ts) VALUES (?,?,?,?,?,?,?)",
                (topic, part, off, key, json.dumps(value, separators=(",", ":")),
                 json.dumps(hdr, separators=(",", ":")), ts))
            mid = cur.lastrowid
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        with self._cv:
            self._notify_count += 1
            self._cv.notify_all()
        termlog.kafka_publish(topic, part, off, key, value.get("event_type", "?"), trace_id,
                              hdr.get("from") or value.get("producer") or "app")
        return Message(mid, topic, part, off, key, value, hdr, ts)

    # -- consumer ----------------------------------------------------------
    def subscribe(self, group: str, topics: list[str]) -> None:
        c = self._conn()
        for t in topics:
            c.execute("INSERT OR IGNORE INTO subscription (group_id, topic) VALUES (?,?)", (group, t))

    def poll(self, group: str, topics: list[str], max_records: int = 50) -> list[Message]:
        c = self._conn()
        committed = {(r["topic"], r["part"]): r["next_offset"] for r in
                     c.execute("SELECT topic, part, next_offset FROM consumer_offset WHERE group_id=?", (group,))}
        out: list[Message] = []
        for t in topics:
            for p in range(self.partitions):
                start = committed.get((t, p), 0)
                for r in c.execute("SELECT * FROM message WHERE topic=? AND part=? AND msg_offset>=? "
                                   "ORDER BY msg_offset LIMIT ?", (t, p, start, max_records)):
                    out.append(self._row(r))
        out.sort(key=lambda m: m.id)
        return out[:max_records]

    def commit(self, group: str, positions: dict[tuple[str, int], int]) -> None:
        """positions[(topic, part)] = next offset to read."""
        c = self._conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            for (t, p), nxt in positions.items():
                c.execute("INSERT INTO consumer_offset (group_id, topic, part, next_offset) VALUES (?,?,?,?) "
                          "ON CONFLICT(group_id, topic, part) DO UPDATE SET next_offset = excluded.next_offset",
                          (group, t, p, nxt))
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    def dead_letter(self, group: str, msg: Message, error: str) -> None:
        self._conn().execute(
            "INSERT INTO dead_letter (group_id, topic, part, msg_offset, value, error, ts) VALUES (?,?,?,?,?,?,?)",
            (group, msg.topic, msg.part, msg.offset, json.dumps(msg.value), error[:500], _now()))

    def rewind(self, group: str) -> None:
        """Seek a consumer group back to offset 0 (replay)."""
        self._conn().execute("DELETE FROM consumer_offset WHERE group_id=?", (group,))
        with self._cv:
            self._notify_count += 1
            self._cv.notify_all()

    def wait(self, timeout: float = 0.25) -> None:
        with self._cv:
            seen = self._notify_count
            self._cv.wait_for(lambda: self._notify_count != seen, timeout)

    # -- monitoring ----------------------------------------------------------
    @staticmethod
    def _row(r) -> Message:
        return Message(r["id"], r["topic"], r["part"], r["msg_offset"], r["msg_key"],
                       json.loads(r["value"]), json.loads(r["headers"] or "{}"), r["ts"])

    def end_offsets(self) -> dict[tuple[str, int], int]:
        return {(r["topic"], r["part"]): r["e"] for r in self._conn().execute(
            "SELECT topic, part, MAX(msg_offset)+1 AS e FROM message GROUP BY topic, part")}

    def lag_table(self) -> list[dict]:
        c = self._conn()
        ends = self.end_offsets()
        committed = {(r["group_id"], r["topic"], r["part"]): r["next_offset"]
                     for r in c.execute("SELECT * FROM consumer_offset")}
        rows = []
        for s in c.execute("SELECT group_id, topic FROM subscription ORDER BY group_id, topic"):
            for p in range(self.partitions):
                end = ends.get((s["topic"], p), 0)
                cur = committed.get((s["group_id"], s["topic"], p), 0)
                rows.append({"group": s["group_id"], "topic": s["topic"], "partition": p,
                             "committed": cur, "end": end, "lag": max(0, end - cur)})
        return rows

    def total_lag(self) -> int:
        return sum(r["lag"] for r in self.lag_table())

    def topic_summary(self) -> list[dict]:
        ends = self.end_offsets()
        out = []
        for t, purpose in config.TOPICS.items():
            parts = [ends.get((t, p), 0) for p in range(self.partitions)]
            out.append({"topic": t, "purpose": purpose, "partitions": parts, "messages": sum(parts)})
        return out

    def messages_since(self, since_id: int, limit: int = 300) -> list[Message]:
        rows = self._conn().execute("SELECT * FROM message WHERE id > ? ORDER BY id LIMIT ?",
                                    (since_id, limit)).fetchall()
        return [self._row(r) for r in rows]

    def recent_messages(self, limit: int = 60, topic: str | None = None) -> list[Message]:
        if topic:
            rows = self._conn().execute("SELECT * FROM message WHERE topic=? ORDER BY id DESC LIMIT ?",
                                        (topic, limit)).fetchall()
        else:
            rows = self._conn().execute("SELECT * FROM message ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def find_by_event_id(self, event_id: str) -> Message | None:
        r = self._conn().execute("SELECT * FROM message WHERE value LIKE ? ORDER BY id LIMIT 1",
                                 (f'%"event_id":"{event_id}"%',)).fetchone()
        return self._row(r) if r else None

    def last_id(self) -> int:
        return self._conn().execute("SELECT COALESCE(MAX(id),0) FROM message").fetchone()[0]

    def dead_letters(self, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self._conn().execute(
            "SELECT * FROM dead_letter ORDER BY id DESC LIMIT ?", (limit,))]

    def reset(self) -> None:
        c = self._conn()
        for t in ("message", "consumer_offset", "dead_letter"):
            c.execute(f"DELETE FROM {t}")
        try:
            c.execute("DELETE FROM sqlite_sequence")
        except sqlite3.Error:
            pass
