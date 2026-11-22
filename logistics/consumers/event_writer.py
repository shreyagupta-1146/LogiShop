"""Process 1.0  Capture & Store Events  (Event Writer)

The only component that writes the append-only logs:
    D1 logistics.shipment_event, D2 logistics.gps_ping, D7 logistics.notification
Duplicates are ignored (ON CONFLICT DO NOTHING + the infra.processed_event inbox).
A foreign-key failure means "the shipment row is not projected yet" and is
retried by the base class until the State Updater has created it.
"""
from __future__ import annotations

import json

from .base import Consumer


class EventWriter(Consumer):
    group = "event-writer"
    topics = ["shipment-events", "gps-pings", "customer-requests", "notifications"]

    def handle(self, conn, m):
        v = m.value
        if m.topic == "gps-pings":
            conn.execute(
                "INSERT INTO logistics.gps_ping (shipment_id, vehicle_id, ping_time, lat, lon, speed_kmh, heading_deg) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (v["shipment_id"], v["vehicle_id"], v["ping_time"], v["lat"], v["lon"], v["speed_kmh"], v["heading_deg"]))
        elif m.topic == "notifications":
            conn.execute(
                "INSERT INTO logistics.notification (shipment_id, order_id, customer_id, notification_type, message, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'SENT', ?)",
                (v.get("shipment_id"), v.get("order_id"), v["customer_id"], v["notification_type"], v["message"], v["created_at"]))
        elif v["event_type"] == "REQUEST_ADVANCE":
            return []           # an operator command, not a fact: its outcome is logged as REQUEST_UPDATED
        else:
            conn.execute(
                "INSERT INTO logistics.shipment_event (event_id, shipment_id, event_type, event_time, payload) "
                "VALUES (?, ?, ?, ?, ?::jsonb) ON CONFLICT (event_id) DO NOTHING",
                (v["event_id"], v["shipment_id"], v["event_type"], v["event_time"], json.dumps(v.get("payload") or {})))
        return []
