"""Process 3.0  Manage Inventory  (Inventory Handler)

Stock is only ever changed by an *atomic conditional update* + a ledger row
inside one transaction:

    UPDATE inventory_balance SET available_qty = available_qty - :q
     WHERE warehouse_id = :w AND sku_id = :s AND available_qty >= :q

If the row count is 0 there is not enough stock; every earlier line of the same
shipment is rolled back (SoftFail -> ROLLBACK TO SAVEPOINT) and an EXCEPTION
event is published for the shipment.  The CHECK (available_qty >= 0) on the
table is the last line of defence.
"""
from __future__ import annotations

from ..events import make_event
from .base import Consumer, Retry, SoftFail

UPSERT_BALANCE = (
    "INSERT INTO logistics.inventory_balance (warehouse_id, sku_id, available_qty, updated_at) VALUES (?, ?, ?, ?) "
    "ON CONFLICT (warehouse_id, sku_id) DO UPDATE SET "
    "available_qty = inventory_balance.available_qty + EXCLUDED.available_qty, updated_at = EXCLUDED.updated_at"
)
LEDGER = ("INSERT INTO logistics.inventory_ledger (sku_id, warehouse_id, movement_type, quantity_delta, event_time, "
          "shipment_id, request_id) VALUES (?, ?, ?, ?, ?, ?, ?)")


class InventoryHandler(Consumer):
    group = "inventory-handler"
    topics = ["inventory-events"]

    def handle(self, conn, m):
        ev = m.value
        t, p, ts = ev["event_type"], ev.get("payload") or {}, ev["event_time"]

        if t == "STOCK_IN":
            q = int(p["quantity"])
            conn.execute(UPSERT_BALANCE, (p["warehouse_id"], p["sku_id"], q, ts))
            conn.execute(LEDGER, (p["sku_id"], p["warehouse_id"], "STOCK_IN", q, ts, None, None))
            return []

        if t == "STOCK_OUT":
            sid, wh = ev["shipment_id"], p["warehouse_id"]
            if conn.execute("SELECT 1 FROM logistics.shipment WHERE shipment_id = ?", (sid,)).fetchone() is None:
                raise Retry(f"shipment {sid} not projected yet")
            for it in sorted(p["items"], key=lambda i: i["sku_id"]):
                q = int(it["quantity"])
                cur = conn.execute(
                    "UPDATE logistics.inventory_balance SET available_qty = available_qty - ?, updated_at = ? "
                    "WHERE warehouse_id = ? AND sku_id = ? AND available_qty >= ?",
                    (q, ts, wh, it["sku_id"], q))
                if cur.rowcount == 0:
                    row = conn.execute("SELECT available_qty FROM logistics.inventory_balance WHERE warehouse_id = ? "
                                       "AND sku_id = ?", (wh, it["sku_id"])).fetchone()
                    avail = row["available_qty"] if row else 0
                    exc = make_event("EXCEPTION", sid, {
                        "reason": "INSUFFICIENT_STOCK", "warehouse_id": wh, "sku_id": it["sku_id"],
                        "requested": q, "available": avail}, event_time=ts,
                        event_id=f"STK-EXC:{ev['event_id']}", producer=self.group)
                    raise SoftFail(f"insufficient stock for {it['sku_id']}: requested {q}, available {avail}",
                                   [("shipment-events", sid, exc)])
                conn.execute(LEDGER, (it["sku_id"], wh, "SHIPMENT_OUT", -q, ts, sid, None))
            return []

        if t == "RETURN_RESTOCK":
            rid, wh = p["request_id"], p["warehouse_id"]
            if conn.execute("SELECT 1 FROM logistics.customer_request WHERE request_id = ?", (rid,)).fetchone() is None:
                raise Retry(f"request {rid} not stored yet")
            for it in sorted(p["items"], key=lambda i: i["sku_id"]):
                if it.get("item_condition", "GOOD") != "GOOD":
                    continue                        # damaged / opened goods are not put back on the shelf
                q = int(it["quantity"])
                conn.execute(UPSERT_BALANCE, (wh, it["sku_id"], q, ts))
                conn.execute(LEDGER, (it["sku_id"], wh, "RETURN_IN", q, ts, None, rid))
            return []

        return []
