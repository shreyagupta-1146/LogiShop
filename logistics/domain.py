"""Pure domain logic: time helpers, geo helpers and the shipment state machine.

`apply_event` is the single definition of how an event changes a shipment.  The
live State Updater and the replay / verify code both call it, so the live
projection and a rebuild from the event log can never disagree.
"""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone

from . import config

# ----------------------------------------------------------------------
#  time
# ----------------------------------------------------------------------
TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def parse_ts(s: str) -> datetime:
    s = s.replace("Z", "+00:00")
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def fmt_ts(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime(TS_FMT)


def now_ts() -> str:
    return fmt_ts(datetime.now(timezone.utc))


def add_minutes(ts: str, minutes: float) -> str:
    return fmt_ts(parse_ts(ts) + timedelta(minutes=minutes))


def minutes_between(a: str, b: str) -> float:
    """b - a in minutes."""
    return (parse_ts(b) - parse_ts(a)).total_seconds() / 60.0


def local_hour(ts: str) -> float:
    d = parse_ts(ts) + timedelta(minutes=config.TZ_OFFSET_MIN)
    return d.hour + d.minute / 60.0


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


# ----------------------------------------------------------------------
#  geo
# ----------------------------------------------------------------------
def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bearing_deg(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def interpolate(lat1, lon1, lat2, lon2, f: float) -> tuple[float, float]:
    return lat1 + (lat2 - lat1) * f, lon1 + (lon2 - lon1) * f


def angle_diff(a: float, b: float) -> float:
    d = abs(a - b) % 360
    return 360 - d if d > 180 else d


# ----------------------------------------------------------------------
#  shipment lifecycle
# ----------------------------------------------------------------------
STATUSES = ["CREATED", "PICKED_UP", "IN_TRANSIT", "OUT_FOR_DELIVERY", "DELIVERED", "EXCEPTION"]

# event_type -> status it moves the shipment to
STATUS_EVENTS = {
    "SHIPMENT_CREATED": "CREATED",
    "PICKED_UP": "PICKED_UP",
    "IN_TRANSIT": "IN_TRANSIT",
    "OUT_FOR_DELIVERY": "OUT_FOR_DELIVERY",
    "DELIVERED": "DELIVERED",
    "EXCEPTION": "EXCEPTION",
}

TRANSITIONS = {
    "CREATED": {"PICKED_UP", "EXCEPTION"},
    "PICKED_UP": {"IN_TRANSIT", "EXCEPTION"},
    "IN_TRANSIT": {"OUT_FOR_DELIVERY", "EXCEPTION"},
    "OUT_FOR_DELIVERY": {"DELIVERED", "EXCEPTION"},
    "EXCEPTION": {"PICKED_UP", "IN_TRANSIT", "OUT_FOR_DELIVERY"},
    "DELIVERED": set(),
}

SHIPMENT_COLUMNS = [
    "shipment_id", "order_id", "customer_id", "origin_warehouse_id", "vehicle_id", "zone_id", "status",
    "current_lat", "current_lon", "eta_original", "eta_current", "at_risk", "delivery_start",
    "delivery_end", "destination_address", "dest_lat", "dest_lon", "stops_remaining",
    "actual_delivery_at", "created_at", "updated_at",
]
# columns whose value comes from events (location comes from GPS pings)
EVENT_DERIVED = [c for c in SHIPMENT_COLUMNS if c not in ("current_lat", "current_lon")]


class Rejected(Exception):
    """The event is not applicable to the current state; it must not be applied."""


def _required(p: dict, *keys):
    miss = [k for k in keys if p.get(k) in (None, "")]
    if miss:
        raise Rejected(f"malformed event, missing: {', '.join(miss)}")


def apply_event(state: dict | None, ev: dict) -> dict | None:
    """Return the new shipment state after `ev`, or raise Rejected.

    `state` is a dict of SHIPMENT_COLUMNS plus 'items' (list of {sku_id, quantity}),
    or None if the shipment does not exist yet.  Events that do not influence the
    shipment (audit-only events) return the state unchanged.
    """
    t, p, ts = ev["event_type"], ev.get("payload") or {}, ev["event_time"]

    if t == "SHIPMENT_CREATED":
        if state is not None:
            raise Rejected("shipment already exists")
        _required(p, "customer_id", "origin_warehouse_id", "delivery_start", "delivery_end",
                  "destination_address", "dest_lat", "dest_lon")
        items = p.get("items") or []
        if not items:
            raise Rejected("a shipment needs at least one item")
        return {
            "shipment_id": ev["shipment_id"], "order_id": p.get("order_id"), "customer_id": p["customer_id"],
            "origin_warehouse_id": p["origin_warehouse_id"], "vehicle_id": p.get("vehicle_id"),
            "zone_id": p.get("zone_id"), "status": "CREATED", "current_lat": None, "current_lon": None,
            "eta_original": p.get("eta_original"), "eta_current": p.get("eta_original"), "at_risk": False,
            "delivery_start": p["delivery_start"], "delivery_end": p["delivery_end"],
            "destination_address": p["destination_address"], "dest_lat": p["dest_lat"], "dest_lon": p["dest_lon"],
            "stops_remaining": int(p.get("stops_remaining", 0)), "actual_delivery_at": None,
            "created_at": ts, "updated_at": ts,
            "items": sorted(({"sku_id": i["sku_id"], "quantity": int(i["quantity"])} for i in items),
                            key=lambda i: i["sku_id"]),
        }

    if state is None:
        raise Rejected("shipment does not exist")
    s = dict(state)
    delivered = s["status"] == "DELIVERED"

    if t == "VEHICLE_ASSIGNED":
        _required(p, "vehicle_id")
        if delivered:
            raise Rejected("shipment is already delivered")
        s["vehicle_id"] = p["vehicle_id"]

    elif t in STATUS_EVENTS:
        new = STATUS_EVENTS[t]
        cur = s["status"]
        if new not in TRANSITIONS[cur]:
            raise Rejected(f"invalid transition {cur} -> {new}")
        if new == "PICKED_UP" and not s["vehicle_id"]:
            raise Rejected("cannot pick up: no vehicle assigned")
        s["status"] = new
        if new == "DELIVERED":
            s["actual_delivery_at"] = ts
            s["eta_current"] = ts
            s["at_risk"] = False
            s["stops_remaining"] = 0

    elif t == "STOP_COMPLETED":
        if not delivered:
            s["stops_remaining"] = max(0, s["stops_remaining"] - 1)
        else:
            return state

    elif t == "DELIVERY_RESCHEDULED":
        _required(p, "new_start", "new_end")
        if s["status"] not in ("CREATED", "PICKED_UP", "IN_TRANSIT", "EXCEPTION"):
            raise Rejected(f"cannot reschedule a shipment that is {s['status']}")
        s["delivery_start"], s["delivery_end"] = p["new_start"], p["new_end"]

    elif t == "ETA_UPDATE":
        if delivered:
            return state
        _required(p, "eta")
        s["eta_current"] = p["eta"]

    elif t == "EXCEPTION_RISK":
        if delivered:
            return state
        s["at_risk"] = True

    elif t == "RISK_CLEARED":
        if delivered:
            return state
        s["at_risk"] = False

    else:       # ARRIVED_AT_ZONE, REQUEST_*, EVENT_REJECTED : audit-only
        return state

    s["updated_at"] = ts
    return s


def fold(events) -> dict[str, dict]:
    """Replay an ordered iterable of event dicts into {shipment_id: state}."""
    states: dict[str, dict] = {}
    for ev in events:
        try:
            new = apply_event(states.get(ev["shipment_id"]), ev)
        except Rejected:
            continue
        if new is not None:
            states[ev["shipment_id"]] = new
    return states


def changed_columns(old: dict, new: dict) -> dict:
    return {c: new[c] for c in EVENT_DERIVED if old.get(c) != new.get(c)}


# ----------------------------------------------------------------------
#  customer-request rules (shared by the handler and documentation)
# ----------------------------------------------------------------------
RESCHEDULABLE = ("CREATED", "PICKED_UP", "IN_TRANSIT", "EXCEPTION")
REQUEST_NEXT = {
    ("RETURN", "PICKUP_SCHEDULED"): "PICKED_UP",
    ("RETURN", "PICKED_UP"): "COMPLETED",
}
