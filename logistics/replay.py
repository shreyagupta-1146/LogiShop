"""Event-sourcing recovery: verify and rebuild the derived tables.

`logistics.shipment` / `shipment_item` are a projection of `shipment_event` (+ latest GPS ping) and
`inventory_balance` is a projection of `inventory_ledger`.  verify() recomputes them in
memory and diffs; rebuild() drops and recreates them from the logs inside ONE transaction
(all foreign keys are DEFERRABLE, so SET CONSTRAINTS ALL DEFERRED lets the rows be replaced and
PostgreSQL re-checks every constraint at COMMIT).
"""
from __future__ import annotations

import time

from . import db, domain
from .consumers.state_updater import INSERT_SHIPMENT, load_state


def _events(conn):
    for r in conn.execute("SELECT event_id, shipment_id, event_type, event_time, payload "
                          "FROM logistics.shipment_event ORDER BY log_seq"):
        yield {"event_id": r["event_id"], "shipment_id": r["shipment_id"], "event_type": r["event_type"],
               "event_time": r["event_time"], "payload": r["payload"]}


def expected_states(conn) -> tuple[dict, int]:
    """Fold the event log (+ GPS pings for the position) into {shipment_id: state}."""
    n = 0

    def counted():
        nonlocal n
        for e in _events(conn):
            n += 1
            yield e

    states = domain.fold(counted())
    for sid, st in states.items():
        if st["status"] == "DELIVERED":
            ping = conn.execute("SELECT lat, lon FROM logistics.gps_ping WHERE shipment_id = ? AND ping_time <= ? "
                                "ORDER BY ping_time DESC, ping_id DESC LIMIT 1", (sid, st["actual_delivery_at"])).fetchone()
        else:
            ping = conn.execute("SELECT lat, lon FROM logistics.gps_ping WHERE shipment_id = ? "
                                "ORDER BY ping_time DESC, ping_id DESC LIMIT 1", (sid,)).fetchone()
        st["current_lat"], st["current_lon"] = (ping["lat"], ping["lon"]) if ping else (None, None)
    return states, n


def verify(conn) -> dict:
    t0 = time.perf_counter()
    exp, n_events = expected_states(conn)
    live = {r["shipment_id"]: load_state(conn, r["shipment_id"])
            for r in conn.execute("SELECT shipment_id FROM logistics.shipment")}
    mism = []
    for sid, e in exp.items():
        l = live.get(sid)
        if l is None:
            continue
        for col in domain.SHIPMENT_COLUMNS + ["items"]:
            a, b = l.get(col), e.get(col)
            if isinstance(a, float) and isinstance(b, float):
                same = abs(a - b) < 1e-9
            else:
                same = a == b
            if not same:
                mism.append({"shipment_id": sid, "field": col, "live": a, "replayed": b})
    missing = sorted(set(exp) - set(live))
    extra = sorted(set(live) - set(exp))
    inv = [dict(r) for r in conn.execute("SELECT * FROM logistics.v_inventory_audit WHERE audit <> 'OK'")]
    st_audit = [dict(r) for r in conn.execute("SELECT * FROM logistics.v_status_audit WHERE audit <> 'OK'")]
    ok = not (mism or missing or extra or inv or st_audit)
    return {"ok": ok, "shipments_in_log": len(exp), "shipments_live": len(live), "events_replayed": n_events,
            "mismatches": mism[:50], "mismatch_count": len(mism), "missing_in_live": missing,
            "extra_in_live": extra, "inventory_mismatches": inv, "status_audit_mismatches": st_audit,
            "foreign_key_violations": 0, "ms": round((time.perf_counter() - t0) * 1000, 1)}


def rebuild(pipeline) -> dict:
    """Drop and rebuild shipment, shipment_item and inventory_balance from the logs."""
    trace = pipeline.new_trace("REBUILD derived tables by replaying the event log", "maintenance")
    pipeline.wait_idle()
    pipeline.pause()
    c = db.connect("replay")
    try:
        before = verify(c)
        t0 = time.perf_counter()
        with db.write_tx(c, "replay", trace):
            c.execute("SET CONSTRAINTS ALL DEFERRED")        # checked again at COMMIT
            c.execute("DELETE FROM logistics.shipment_item")
            c.execute("DELETE FROM logistics.shipment")
            c.execute("DELETE FROM logistics.inventory_balance")
            states, _ = expected_states(c)
            for st in states.values():
                c.execute(INSERT_SHIPMENT, st)
                for it in st["items"]:
                    c.execute("INSERT INTO logistics.shipment_item (shipment_id, sku_id, quantity) VALUES (?, ?, ?)",
                              (st["shipment_id"], it["sku_id"], it["quantity"]))
            c.execute("INSERT INTO logistics.inventory_balance (warehouse_id, sku_id, available_qty, updated_at) "
                      "SELECT warehouse_id, sku_id, sum(quantity_delta), max(event_time) "
                      "FROM logistics.inventory_ledger GROUP BY warehouse_id, sku_id")
        ms = round((time.perf_counter() - t0) * 1000, 1)
        after = verify(c)
        n_ship = c.execute("SELECT count(*) AS n FROM logistics.shipment").fetchone()["n"]
        n_bal = c.execute("SELECT count(*) AS n FROM logistics.inventory_balance").fetchone()["n"]
    finally:
        c.close()
        pipeline.resume()
    return {"trace_id": trace, "rebuilt_shipments": n_ship, "rebuilt_balances": n_bal, "ms": ms,
            "before": {"ok": before["ok"], "mismatches": before["mismatch_count"],
                       "inventory_mismatches": len(before["inventory_mismatches"])},
            "after": after}
