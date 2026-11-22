"""The application layer commands: dispatcher, driver app, customer portal and warehouse.

Nothing here writes shipment state.  Every command is turned into an *event* and published
to a Kafka-style topic keyed by shipment_id; the consumers do the rest.  Reference lookups
(does this vehicle / SKU exist?) read the database.

(Checkout is different: it is a single PostgreSQL transaction - the stored function
shop.place_order - that also writes an outbox row; see outbox.py.)
"""
from __future__ import annotations

from . import db
from .domain import add_minutes, haversine_km, now_ts
from .events import make_event, make_ping
from .ml import simdata


class App:
    def __init__(self, pipeline):
        self.p = pipeline

    # -- read helpers ------------------------------------------------------
    def q(self, sql, params=None):
        with db.POOL.connection() as c:
            return c.execute(sql, params).fetchall()

    def q1(self, sql, params=None):
        with db.POOL.connection() as c:
            return c.execute(sql, params).fetchone()

    def _trace(self, trace_id, label, kind="action"):
        return trace_id or self.p.new_trace(label, kind)

    def _pub(self, topic, key, ev, trace_id):
        m = self.p.publish(topic, key, ev, trace_id)
        return {"event_id": ev["event_id"], "event_type": ev["event_type"], "topic": topic,
                "partition": m.part, "offset": m.offset}

    def shipment_row(self, shipment_id: str):
        row = self.q1("SELECT * FROM logistics.shipment WHERE shipment_id = ?", (shipment_id,))
        if row is None:
            raise ValueError(f"unknown shipment {shipment_id}")
        return row

    # -- direct shipment creation (admin tools / tests; the shop uses orders instead) ----------
    def create_shipment(self, customer_id, warehouse_id, items, destination_address, dest_lat, dest_lon,
                        window_start=None, window_end=None, stops=0, vehicle_id=None, event_time=None,
                        shipment_id=None, trace_id=None, order_id=None):
        if not self.q1("SELECT 1 FROM shop.customer WHERE customer_id = ?", (customer_id,)):
            raise ValueError(f"unknown customer {customer_id}")
        wh = self.q1("SELECT w.warehouse_id, g.center_lat AS lat, g.center_lon AS lon FROM logistics.warehouse w "
                     "JOIN logistics.geofence g USING (geofence_id) WHERE w.warehouse_id = ?", (warehouse_id,))
        if wh is None:
            raise ValueError(f"unknown warehouse {warehouse_id}")
        if vehicle_id and not self.q1("SELECT 1 FROM logistics.vehicle WHERE vehicle_id = ?", (vehicle_id,)):
            raise ValueError(f"unknown vehicle {vehicle_id}")
        merged: dict[str, int] = {}
        for it in items or []:
            q = int(it["quantity"])
            if q < 1:
                raise ValueError("item quantity must be at least 1")
            merged[it["sku_id"]] = merged.get(it["sku_id"], 0) + q
        if not merged:
            raise ValueError("a shipment needs at least one item")
        for sku in merged:
            if not self.q1("SELECT 1 FROM shop.sku WHERE sku_id = ?", (sku,)):
                raise ValueError(f"unknown SKU {sku}")
        dest_lat, dest_lon = float(dest_lat), float(dest_lon)
        stops = max(0, int(stops or 0))
        t = event_time or now_ts()
        zone = self.q1("SELECT logistics.zone_for_point(?, ?) AS z", (dest_lat, dest_lon))["z"]
        dist = haversine_km(wh["lat"], wh["lon"], dest_lat, dest_lon)
        eta0 = add_minutes(t, 30 + simdata.rule_based_minutes(dist, stops))
        ws = window_start or add_minutes(t, 60)
        we = window_end or add_minutes(t, 240)
        if we <= ws:
            raise ValueError("delivery window end must be after its start")
        sid = shipment_id or self.q1("SELECT 'SHP-' || nextval('logistics.shipment_seq') AS id")["id"]
        trace = self._trace(trace_id, f"Create shipment {sid}")
        lines = [{"sku_id": k, "quantity": v} for k, v in sorted(merged.items())]
        created = make_event("SHIPMENT_CREATED", sid, {
            "order_id": order_id, "customer_id": customer_id, "origin_warehouse_id": warehouse_id, "vehicle_id": vehicle_id,
            "zone_id": zone, "delivery_start": ws, "delivery_end": we,
            "destination_address": destination_address, "dest_lat": dest_lat, "dest_lon": dest_lon,
            "stops_remaining": stops, "eta_original": eta0, "items": lines}, event_time=t, producer="order-app")
        out = [self._pub("shipment-events", sid, created, trace)]
        stock = make_event("STOCK_OUT", sid, {"warehouse_id": warehouse_id, "items": lines},
                           event_time=t, producer="order-app")
        out.append(self._pub("inventory-events", sid, stock, trace))
        return {"trace_id": trace, "shipment_id": sid, "published": out}

    # -- dispatcher / driver ------------------------------------------------
    def assign_vehicle(self, shipment_id, vehicle_id, event_time=None, trace_id=None):
        self.shipment_row(shipment_id)
        if not self.q1("SELECT 1 FROM logistics.vehicle WHERE vehicle_id = ?", (vehicle_id,)):
            raise ValueError(f"unknown vehicle {vehicle_id}")
        trace = self._trace(trace_id, f"Dispatcher assigns {vehicle_id} to {shipment_id}")
        ev = make_event("VEHICLE_ASSIGNED", shipment_id, {"vehicle_id": vehicle_id},
                        event_time=event_time, producer="dispatcher-app")
        return {"trace_id": trace, "shipment_id": shipment_id, "published": [self._pub("shipment-events", shipment_id, ev, trace)]}

    def set_status(self, shipment_id, status, reason=None, event_time=None, trace_id=None):
        if status not in ("PICKED_UP", "IN_TRANSIT", "OUT_FOR_DELIVERY", "DELIVERED", "EXCEPTION"):
            raise ValueError(f"invalid status {status}")
        self.shipment_row(shipment_id)
        trace = self._trace(trace_id, f"{shipment_id}: status -> {status}")
        payload = {"reason": reason} if reason else {}
        ev = make_event(status, shipment_id, payload, event_time=event_time, producer="driver-app")
        return {"trace_id": trace, "shipment_id": shipment_id, "published": [self._pub("shipment-events", shipment_id, ev, trace)]}

    def stop_completed(self, shipment_id, event_time=None, trace_id=None):
        self.shipment_row(shipment_id)
        trace = self._trace(trace_id, f"{shipment_id}: intermediate stop completed")
        ev = make_event("STOP_COMPLETED", shipment_id, {}, event_time=event_time, producer="driver-app")
        return {"trace_id": trace, "shipment_id": shipment_id, "published": [self._pub("shipment-events", shipment_id, ev, trace)]}

    def send_ping(self, shipment_id, lat, lon, speed_kmh, heading_deg=0.0, vehicle_id=None, ping_time=None,
                  trace_id=None, label=None):
        row = self.shipment_row(shipment_id)
        vehicle_id = vehicle_id or row["vehicle_id"]
        if not vehicle_id:
            raise ValueError("no vehicle assigned to this shipment")
        trace = self._trace(trace_id, label or f"GPS ping {shipment_id} ({float(lat):.4f}, {float(lon):.4f}) @ {float(speed_kmh):.0f} km/h")
        ping = make_ping(shipment_id, vehicle_id, lat, lon, speed_kmh, heading_deg, ping_time)
        return {"trace_id": trace, "shipment_id": shipment_id, "published": [self._pub("gps-pings", shipment_id, ping, trace)]}

    # -- customer portal -----------------------------------------------------
    def submit_request(self, shipment_id, request_type, reason=None, requested_start=None, requested_end=None,
                       items=None, event_time=None, trace_id=None):
        row = self.shipment_row(shipment_id)
        request_type = request_type.upper()
        if request_type not in ("RESCHEDULE", "RETURN"):
            raise ValueError("request_type must be RESCHEDULE or RETURN")
        rid = self.q1("SELECT 'REQ-' || nextval('logistics.request_seq') AS id")["id"]
        trace = self._trace(trace_id, f"{row['customer_id']} requests {request_type.lower()} for {shipment_id} ({rid})")
        ev = make_event("REQUEST_SUBMITTED", shipment_id, {
            "request_id": rid, "customer_id": row["customer_id"], "request_type": request_type, "reason": reason,
            "requested_start": requested_start, "requested_end": requested_end,
            "items": [{"sku_id": i["sku_id"], "quantity": int(i["quantity"]),
                       "item_condition": i.get("item_condition", "GOOD")} for i in (items or [])]},
            event_time=event_time, producer="customer-portal")
        return {"trace_id": trace, "shipment_id": shipment_id, "request_id": rid,
                "published": [self._pub("customer-requests", shipment_id, ev, trace)]}

    def advance_request(self, request_id, to_status, event_time=None, trace_id=None):
        r = self.q1("SELECT shipment_id FROM logistics.customer_request WHERE request_id = ?", (request_id,))
        if r is None:
            raise ValueError(f"unknown request {request_id}")
        trace = self._trace(trace_id, f"Warehouse: request {request_id} -> {to_status}")
        ev = make_event("REQUEST_ADVANCE", r["shipment_id"], {"request_id": request_id, "to_status": to_status},
                        event_time=event_time, producer="warehouse-app")
        return {"trace_id": trace, "shipment_id": r["shipment_id"], "request_id": request_id,
                "published": [self._pub("customer-requests", r["shipment_id"], ev, trace)]}

    # -- warehouse ---------------------------------------------------------------
    def stock_in(self, warehouse_id, sku_id, quantity, event_time=None, trace_id=None):
        quantity = int(quantity)
        if quantity < 1:
            raise ValueError("quantity must be at least 1")
        if not self.q1("SELECT 1 FROM logistics.warehouse WHERE warehouse_id = ?", (warehouse_id,)):
            raise ValueError(f"unknown warehouse {warehouse_id}")
        if not self.q1("SELECT 1 FROM shop.sku WHERE sku_id = ?", (sku_id,)):
            raise ValueError(f"unknown SKU {sku_id}")
        trace = self._trace(trace_id, f"Warehouse {warehouse_id}: receive {quantity} x {sku_id}")
        ev = make_event("STOCK_IN", warehouse_id, {"warehouse_id": warehouse_id, "sku_id": sku_id, "quantity": quantity},
                        event_time=event_time, producer="warehouse-app")
        # STOCK_IN is not about a shipment; the key is the warehouse so per-warehouse order is kept
        return {"trace_id": trace, "published": [self._pub("inventory-events", warehouse_id, ev, trace)]}

    # -- failure-mode demos ----------------------------------------------------------
    def redeliver(self, event_id, trace_id=None):
        """Publish the *same* message again (a producer retry / broker redelivery)."""
        msg = self.p.broker.find_by_event_id(event_id)
        if msg is None:
            raise ValueError(f"no message with event_id {event_id}")
        trace = self._trace(trace_id, f"DUPLICATE delivery of {event_id} ({msg.value.get('event_type')})")
        m = self.p.publish(msg.topic, msg.key, msg.value, trace)
        return {"trace_id": trace, "published": [{"event_id": event_id, "event_type": msg.value.get("event_type"),
                                                  "topic": msg.topic, "partition": m.part, "offset": m.offset}]}

    def purge_old_pings(self, days: int = 90):
        """Retention job: the SQL counterpart of the 90-day TTL index on gps_pings."""
        trace = self.p.new_trace(f"Retention job: delete GPS pings older than {days} days", "maintenance")
        with db.POOL.connection() as c:
            with db.write_tx(c, "retention-job", trace):
                n = c.execute("DELETE FROM logistics.gps_ping WHERE ping_time < now() - make_interval(days => ?)",
                              (int(days),)).rowcount
        return {"trace_id": trace, "deleted": n}
