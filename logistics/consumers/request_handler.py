"""Process 4.0  Process Customer Requests  (Request Handler)

Validates RESCHEDULE requests (allowed until the shipment is out for delivery) and
RETURN requests (allowed after delivery, within 7 days, only for items that were
shipped), stores them with status tracking in D5 and publishes the outcome events.
The same rules also exist as triggers in the database (defence in depth).
"""
from __future__ import annotations

from .. import config, domain
from ..domain import add_minutes, minutes_between
from ..events import make_event, make_notification
from .base import Consumer, Retry, SoftFail

NEXT_STATUS = {"PICKUP_SCHEDULED": "PICKED_UP", "PICKED_UP": "COMPLETED"}


class RequestHandler(Consumer):
    group = "request-handler"
    topics = ["customer-requests"]

    def handle(self, conn, m):
        ev = m.value
        if ev["event_type"] == "REQUEST_SUBMITTED":
            return self._submitted(conn, ev)
        if ev["event_type"] == "REQUEST_ADVANCE":
            return self._advance(conn, ev)
        return []           # outcome events published by this handler are only logged by the Event Writer

    # ------------------------------------------------------------------
    def _reject_event(self, ev, reason, extra=None):
        payload = {"rejected_event_id": ev["event_id"], "rejected_type": ev["event_type"], "reason": reason}
        payload.update(extra or {})
        return ("shipment-events", ev["shipment_id"],
                make_event("EVENT_REJECTED", ev["shipment_id"], payload, event_time=ev["event_time"],
                           event_id=f"REJ:{ev['event_id']}", producer=self.group))

    def _notify(self, ev, cust, text):
        return ("notifications", ev["shipment_id"],
                make_notification(ev["shipment_id"], cust, "REQUEST", text, ev["event_time"], ev["event_id"]))

    # ------------------------------------------------------------------
    def _submitted(self, conn, ev):
        p, sid, now = ev["payload"], ev["shipment_id"], ev["event_time"]
        rid, rtype = p["request_id"], p["request_type"]
        s = conn.execute("SELECT customer_id, status, origin_warehouse_id, actual_delivery_at, delivery_end "
                         "FROM shipment WHERE shipment_id = ?", (sid,)).fetchone()
        if s is None:
            raise Retry(f"shipment {sid} not projected yet")
        cust = s["customer_id"]
        if p.get("customer_id") != cust:
            raise SoftFail("request raised by a customer who does not own the shipment",
                           [self._reject_event(ev, "request must be raised by the shipment's own customer")])

        reason = None
        items = p.get("items") or []
        if rtype == "RESCHEDULE":
            start, end = p.get("requested_start"), p.get("requested_end")
            if not start or not end or end <= start:
                raise SoftFail("invalid window", [self._reject_event(ev, "requested window is missing or empty",
                                                                     {"request_id": rid})])
            if s["status"] not in domain.RESCHEDULABLE:
                reason = (f"shipment is {s['status']}: rescheduling is only possible until it is out for delivery")
            elif start < now:
                reason = "the requested window starts in the past"
        elif rtype == "RETURN":
            if s["status"] != "DELIVERED":
                reason = f"shipment is {s['status']}: returns are only possible after delivery"
            elif minutes_between(s["actual_delivery_at"], now) > config.RETURN_WINDOW_DAYS * 1440:
                reason = f"return window of {config.RETURN_WINDOW_DAYS} days has passed"
            elif not items:
                reason = "a return must list at least one item"
            else:
                for it in items:
                    shipped = conn.execute("SELECT quantity FROM shipment_item WHERE shipment_id=? AND sku_id=?",
                                           (sid, it["sku_id"])).fetchone()
                    already = conn.execute(
                        "SELECT COALESCE(SUM(ri.quantity), 0) FROM request_item ri "
                        "JOIN customer_request r ON r.request_id = ri.request_id "
                        "WHERE r.shipment_id = ? AND r.request_type = 'RETURN' AND r.status <> 'REJECTED' "
                        "AND ri.sku_id = ?", (sid, it["sku_id"])).fetchone()[0]
                    if shipped is None:
                        reason = f"{it['sku_id']} was not part of this shipment"
                    elif int(it["quantity"]) + already > shipped["quantity"]:
                        reason = (f"{it['sku_id']}: returning {it['quantity']} (+{already} already requested) "
                                  f"exceeds the {shipped['quantity']} shipped")
                    if reason:
                        break
        else:
            raise SoftFail("unknown request type", [self._reject_event(ev, f"unknown request type {rtype}")])

        cols = (rid, sid, cust, rtype)
        if reason:
            conn.execute(
                "INSERT INTO customer_request (request_id, shipment_id, customer_id, request_type, status, reason, "
                "requested_start, requested_end, created_at, updated_at) VALUES (?,?,?,?,'REJECTED',?,?,?,?,?)",
                (*cols, p.get("reason"), p.get("requested_start"), p.get("requested_end"), now, now))
            out = make_event("REQUEST_REJECTED", sid, {"request_id": rid, "request_type": rtype, "reason": reason},
                             event_time=now, event_id=f"REQ-REJ:{ev['event_id']}", producer=self.group)
            return [("customer-requests", sid, out),
                    self._notify(ev, cust, f"Your {rtype.lower()} request {rid} was rejected: {reason}.")]

        # accepted: stored as PENDING first, then moved through the status machine
        conn.execute(
            "INSERT INTO customer_request (request_id, shipment_id, customer_id, request_type, status, reason, "
            "requested_start, requested_end, created_at, updated_at) VALUES (?,?,?,?,'PENDING',?,?,?,?,?)",
            (*cols, p.get("reason"), p.get("requested_start"), p.get("requested_end"), now, now))
        out = []
        if rtype == "RESCHEDULE":
            conn.execute("UPDATE customer_request SET status = 'APPROVED', updated_at = ? WHERE request_id = ?",
                         (now, rid))
            out.append(("shipment-events", sid, make_event(
                "DELIVERY_RESCHEDULED", sid,
                {"request_id": rid, "new_start": p["requested_start"], "new_end": p["requested_end"]},
                event_time=now, event_id=f"RESCH:{ev['event_id']}", producer=self.group)))
            text = f"Your reschedule request {rid} was approved. New window: {p['requested_start']} to {p['requested_end']}."
        else:
            for it in items:
                conn.execute("INSERT INTO request_item (request_id, sku_id, quantity, item_condition) VALUES (?,?,?,?)",
                             (rid, it["sku_id"], int(it["quantity"]), it.get("item_condition", "GOOD")))
            pickup = add_minutes(now, 24 * 60)
            conn.execute("UPDATE customer_request SET status = 'PICKUP_SCHEDULED', pickup_scheduled_at = ?, "
                         "updated_at = ? WHERE request_id = ?", (pickup, now, rid))
            text = f"Your return request {rid} was accepted. Pickup scheduled for {pickup}."
        out.append(("customer-requests", sid, make_event(
            "REQUEST_ACCEPTED", sid, {"request_id": rid, "request_type": rtype}, event_time=now,
            event_id=f"REQ-ACC:{ev['event_id']}", producer=self.group)))
        out.append(self._notify(ev, cust, text))
        return out

    # ------------------------------------------------------------------
    def _advance(self, conn, ev):
        p, now = ev["payload"], ev["event_time"]
        rid, to = p["request_id"], p["to_status"]
        r = conn.execute("SELECT r.*, s.origin_warehouse_id FROM customer_request r "
                         "JOIN shipment s ON s.shipment_id = r.shipment_id WHERE r.request_id = ?", (rid,)).fetchone()
        if r is None:
            raise Retry(f"request {rid} not stored yet")
        sid = r["shipment_id"]
        if r["request_type"] != "RETURN" or NEXT_STATUS.get(r["status"]) != to:
            raise SoftFail("invalid request transition", [self._reject_event(
                ev, f"request {rid} is {r['status']}: cannot move to {to}", {"request_id": rid})])
        conn.execute("UPDATE customer_request SET status = ?, updated_at = ? WHERE request_id = ?", (to, now, rid))
        out = [("customer-requests", sid, make_event(
            "REQUEST_UPDATED", sid, {"request_id": rid, "status": to}, event_time=now,
            event_id=f"REQ-UPD:{ev['event_id']}", producer=self.group))]
        text = f"Your return {rid} is now {to.replace('_', ' ').lower()}."
        if to == "COMPLETED":
            items = [dict(i) for i in conn.execute(
                "SELECT sku_id, quantity, item_condition FROM request_item WHERE request_id = ?", (rid,))]
            out.append(("inventory-events", sid, make_event(
                "RETURN_RESTOCK", sid, {"request_id": rid, "warehouse_id": r["origin_warehouse_id"], "items": items},
                event_time=now, event_id=f"RESTOCK:{rid}", producer=self.group)))
            refund = conn.execute("SELECT shop.refund_return(?) AS amount", (rid,)).fetchone()["amount"]
            text += " Items in good condition were returned to stock."
            if refund:
                text += f" Refund of Rs {refund:,.2f} issued."
        out.append(self._notify(ev, r["customer_id"], text))
        return out
