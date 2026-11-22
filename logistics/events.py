"""Constructors for the messages that travel through the broker."""
from __future__ import annotations

from .domain import new_id, now_ts


def make_event(event_type: str, shipment_id: str, payload: dict | None = None, *,
               event_time: str | None = None, event_id: str | None = None, producer: str = "app") -> dict:
    return {
        "event_id": event_id or new_id("evt"),
        "event_type": event_type,
        "shipment_id": shipment_id,
        "event_time": event_time or now_ts(),
        "payload": payload or {},
        "producer": producer,
    }


def make_ping(shipment_id: str, vehicle_id: str, lat: float, lon: float, speed_kmh: float,
              heading_deg: float, ping_time: str | None = None, producer: str = "driver-app") -> dict:
    return {
        "event_id": new_id("png"),
        "event_type": "GPS_PING",
        "shipment_id": shipment_id,
        "vehicle_id": vehicle_id,
        "ping_time": ping_time or now_ts(),
        "lat": round(float(lat), 6),
        "lon": round(float(lon), 6),
        "speed_kmh": round(max(0.0, float(speed_kmh)), 1),
        "heading_deg": round(float(heading_deg) % 360.0, 1),
        "producer": producer,
    }


def make_notification(shipment_id: str | None, customer_id: str, ntype: str, message: str,
                      created_at: str, source_event_id: str | None = None, order_id: str | None = None) -> dict:
    return {
        "event_id": f"NTF:{source_event_id}:{ntype}" if source_event_id else new_id("ntf"),
        "event_type": "NOTIFICATION",
        "shipment_id": shipment_id,
        "order_id": order_id,
        "customer_id": customer_id,
        "notification_type": ntype,
        "message": message,
        "created_at": created_at,
    }


STATUS_LABEL = {
    "CREATED": "created", "PICKED_UP": "picked up from the warehouse", "IN_TRANSIT": "in transit",
    "OUT_FOR_DELIVERY": "out for delivery", "DELIVERED": "delivered", "EXCEPTION": "delayed by an exception",
}
