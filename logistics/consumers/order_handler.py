"""Process 0.5  Fulfil Orders  (Order Handler)

Consumes ORDER_PLACED (relayed from the transactional outbox) and turns an order into a shipment:
  1. choose the nearest warehouse that has every ordered item in stock  (one SQL query with
     NOT EXISTS + haversine distance)
  2. publish SHIPMENT_CREATED (shipment-events) and STOCK_OUT (inventory-events); the State
     Updater and the Inventory Handler do the writes
  3. if no warehouse can serve the whole order: cancel it and refund the payment
     (stored function shop.cancel_order)
"""
from __future__ import annotations

from ..domain import add_minutes, haversine_km
from ..events import make_event, make_notification
from ..ml import simdata
from .base import Consumer, Retry

PICK_WAREHOUSE = """
SELECT w.warehouse_id, g.center_lat, g.center_lon,
       logistics.haversine_km(g.center_lat, g.center_lon, ?, ?) AS km
  FROM logistics.warehouse w
  JOIN logistics.geofence g USING (geofence_id)
 WHERE NOT EXISTS (SELECT 1
                     FROM shop.order_line l
                     LEFT JOIN logistics.inventory_balance b
                            ON b.warehouse_id = w.warehouse_id AND b.sku_id = l.sku_id
                    WHERE l.order_id = ? AND COALESCE(b.available_qty, 0) < l.quantity)
 ORDER BY km
 LIMIT 1
"""


class OrderHandler(Consumer):
    group = "order-handler"
    topics = ["orders"]

    def handle(self, conn, m):
        ev = m.value
        if ev.get("event_type") != "ORDER_PLACED":
            return []
        oid = ev["order_id"]
        o = conn.execute("SELECT customer_id, status, ship_address, ship_lat, ship_lon, placed_at "
                         "FROM shop.customer_order WHERE order_id = ?", (oid,)).fetchone()
        if o is None:
            raise Retry(f"order {oid} not visible yet")
        if o["status"] == "CANCELLED":
            return []
        lines = ev["lines"]
        wh = conn.execute(PICK_WAREHOUSE, (o["ship_lat"], o["ship_lon"], oid)).fetchone()
        t = o["placed_at"]

        if wh is None:
            conn.execute("SELECT shop.cancel_order(?, 'No warehouse has all the items in stock')", (oid,))
            return [("notifications", oid, make_notification(
                None, o["customer_id"], "STATUS",
                f"Sorry - order {oid} could not be fulfilled because an item is out of stock. Your payment is being refunded.",
                t, f"{ev['event_id']}:NOSTOCK", order_id=oid))]

        sid = conn.execute("SELECT 'SHP-' || nextval('logistics.shipment_seq') AS id").fetchone()["id"]
        zone = conn.execute("SELECT logistics.zone_for_point(?, ?) AS z", (o["ship_lat"], o["ship_lon"])).fetchone()["z"]
        stops = sum(map(ord, oid)) % 3                              # other drops before this one on the same run
        km = wh["km"]
        eta0 = add_minutes(t, 30 + simdata.rule_based_minutes(km, stops))   # quote: 30 min prep + rule of thumb
        start = add_minutes(t, 60)
        end = add_minutes(t, 240)
        created = make_event("SHIPMENT_CREATED", sid, {
            "order_id": oid, "customer_id": o["customer_id"], "origin_warehouse_id": wh["warehouse_id"],
            "vehicle_id": None, "zone_id": zone, "delivery_start": start, "delivery_end": end,
            "destination_address": o["ship_address"], "dest_lat": o["ship_lat"], "dest_lon": o["ship_lon"],
            "stops_remaining": stops, "eta_original": eta0, "items": lines},
            event_time=t, event_id=f"SHIP-CREATED:{oid}", producer=self.group)
        stock = make_event("STOCK_OUT", sid, {"warehouse_id": wh["warehouse_id"], "items": lines},
                           event_time=t, event_id=f"STOCK-OUT:{oid}", producer=self.group)
        return [("shipment-events", sid, created), ("inventory-events", sid, stock)]
