"""Process 2.0  Maintain Shipment State  (State Updater)

Maintains D3 `logistics.shipment` (+ `shipment_item`) as a projection of the event stream:
  * applies lifecycle / ETA / risk / reschedule events through domain.apply_event
    (invalid transitions are NOT applied; an EVENT_REJECTED event records why)
  * keeps the latest GPS position on the shipment
  * detects arrival in the delivery zone with a geofence query (point-in-polygon, GiST) and
    then emits ARRIVED_AT_ZONE + OUT_FOR_DELIVERY
  * emits customer notifications for the changes it applied
  * compensation: if the warehouse could not serve an order (EXCEPTION / INSUFFICIENT_STOCK)
    the order is cancelled and the payment refunded (stored function shop.cancel_order)
The database triggers then keep customer_order.status in step with the shipment.
"""
from __future__ import annotations

from .. import config, domain
from ..domain import Rejected, apply_event, changed_columns, minutes_between
from ..events import STATUS_LABEL, make_event, make_notification
from .base import Consumer, Poison, Retry

AUDIT_ONLY = {"EVENT_REJECTED", "ARRIVED_AT_ZONE", "REQUEST_SUBMITTED", "REQUEST_ACCEPTED",
              "REQUEST_REJECTED", "REQUEST_UPDATED"}

INSERT_SHIPMENT = (
    "INSERT INTO logistics.shipment (shipment_id, order_id, customer_id, origin_warehouse_id, vehicle_id, zone_id, status, "
    "current_lat, current_lon, eta_original, eta_current, at_risk, delivery_start, delivery_end, "
    "destination_address, dest_lat, dest_lon, stops_remaining, actual_delivery_at, created_at, updated_at) "
    "VALUES (:shipment_id, :order_id, :customer_id, :origin_warehouse_id, :vehicle_id, :zone_id, :status, "
    ":current_lat, :current_lon, :eta_original, :eta_current, :at_risk, :delivery_start, :delivery_end, "
    ":destination_address, :dest_lat, :dest_lon, :stops_remaining, :actual_delivery_at, :created_at, :updated_at)"
)


def load_state(conn, shipment_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM logistics.shipment WHERE shipment_id = ?", (shipment_id,)).fetchone()
    if row is None:
        return None
    s = {c: row[c] for c in domain.SHIPMENT_COLUMNS}
    s["items"] = [dict(r) for r in conn.execute(
        "SELECT sku_id, quantity FROM logistics.shipment_item WHERE shipment_id = ? ORDER BY sku_id", (shipment_id,))]
    return s


class StateUpdater(Consumer):
    group = "state-updater"
    topics = ["shipment-events", "gps-pings"]

    # ------------------------------------------------------------------
    def handle(self, conn, m):
        if m.topic == "gps-pings":
            return self._on_ping(conn, m.value)
        return self._on_event(conn, m.value)

    # ------------------------------------------------------------------
    def _on_event(self, conn, ev):
        et, sid = ev["event_type"], ev["shipment_id"]
        if et in AUDIT_ONLY:
            return []
        old = load_state(conn, sid)
        if old is None and et != "SHIPMENT_CREATED":
            raise Poison(f"event for unknown shipment {sid}")
        try:
            new = apply_event(old, ev)
        except Rejected as r:
            rej = make_event("EVENT_REJECTED", sid, {
                "rejected_event_id": ev["event_id"], "rejected_type": et,
                "current_status": old["status"] if old else None, "reason": str(r)},
                event_time=ev["event_time"], event_id=f"REJ:{ev['event_id']}", producer=self.group)
            return [("shipment-events", sid, rej)]

        if old is None:
            conn.execute(INSERT_SHIPMENT, new)
            for it in new["items"]:
                conn.execute("INSERT INTO logistics.shipment_item (shipment_id, sku_id, quantity) VALUES (?, ?, ?)",
                             (sid, it["sku_id"], it["quantity"]))
        else:
            diff = changed_columns(old, new)
            if diff:
                sets = ", ".join(f"{c} = ?" for c in diff)
                conn.execute(f"UPDATE logistics.shipment SET {sets} WHERE shipment_id = ?", (*diff.values(), sid))
        out = self._notifications(ev, old, new)
        # saga compensation: the warehouse could not serve this order -> cancel it and refund the payment
        p = ev.get("payload") or {}
        if et == "EXCEPTION" and p.get("reason") == "INSUFFICIENT_STOCK" and new.get("order_id"):
            cancelled = conn.execute("SELECT shop.cancel_order(?, ?) AS done",
                                     (new["order_id"], f"Out of stock: {p.get('sku_id')}")).fetchone()["done"]
            if cancelled:
                out.append(("notifications", sid, make_notification(
                    sid, new["customer_id"], "STATUS",
                    f"We are sorry - order {new['order_id']} was cancelled because {p.get('sku_id')} went out of stock. "
                    f"Your payment is being refunded.", ev["event_time"], f"{ev['event_id']}:CANCEL", order_id=new["order_id"])))
        return out

    # ------------------------------------------------------------------
    def _notifications(self, ev, old, new):
        et, sid, cust = ev["event_type"], ev["shipment_id"], new["customer_id"]
        p = ev.get("payload") or {}
        ts = ev["event_time"]
        oid = new.get("order_id")

        def note(kind, text):
            return ("notifications", sid, make_notification(sid, cust, kind, text, ts, ev["event_id"], order_id=oid))

        ref = f"order {oid}" if oid else f"shipment {sid}"
        if et == "SHIPMENT_CREATED":
            return [note("STATUS", f"Your {ref} is confirmed and being prepared at the warehouse.")]
        if et in domain.STATUS_EVENTS:
            text = f"Your {ref} is {STATUS_LABEL[new['status']]}."
            if et == "EXCEPTION" and p.get("reason"):
                text += f" Reason: {p['reason']}"
                if p.get("sku_id"):
                    text += f" ({p['sku_id']}: requested {p.get('requested')}, available {p.get('available')})"
            return [note("STATUS", text)]
        if et == "ETA_UPDATE" and old and old.get("eta_current") and new.get("eta_current") != old["eta_current"]:
            if abs(minutes_between(old["eta_current"], new["eta_current"])) >= 10:
                return [note("ETA", f"New estimated delivery time for your {ref}: {new['eta_current']} "
                                    f"(model: {p.get('model_name', '?')}).")]
        if et == "EXCEPTION_RISK":
            return [note("ALERT", f"Your {ref} may be delayed: {p.get('reason', 'unusual trip behaviour')}.")]
        return []

    # ------------------------------------------------------------------
    def _on_ping(self, conn, p):
        sid = p["shipment_id"]
        row = conn.execute("SELECT status, zone_id FROM logistics.shipment WHERE shipment_id = ?", (sid,)).fetchone()
        if row is None:
            raise Retry(f"shipment {sid} not projected yet")
        if row["status"] == "DELIVERED":
            return []
        conn.execute("UPDATE logistics.shipment SET current_lat = ?, current_lon = ? WHERE shipment_id = ?",
                     (p["lat"], p["lon"], sid))
        if row["status"] == "IN_TRANSIT" and row["zone_id"]:
            # geofence query: is the vehicle inside the delivery zone?  (point-in-polygon on a GiST-indexed polygon)
            hit = conn.execute("SELECT logistics.in_zone(?, ?, ?) AS hit", (row["zone_id"], p["lat"], p["lon"])).fetchone()["hit"]
            if hit:
                t = p["ping_time"]
                arrived = make_event("ARRIVED_AT_ZONE", sid, {"zone_id": row["zone_id"], "lat": p["lat"], "lon": p["lon"]},
                                     event_time=t, event_id=f"ARRIVED:{sid}", producer=self.group)
                ofd = make_event("OUT_FOR_DELIVERY", sid, {"reason": "geofence_arrival", "zone_id": row["zone_id"]},
                                 event_time=t, event_id=f"AUTO-OFD:{sid}", producer=self.group)
                return [("shipment-events", sid, arrived), ("shipment-events", sid, ofd)]
        return []
