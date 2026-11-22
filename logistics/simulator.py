"""Trip simulator: a driver app driving a vehicle from the warehouse to the customer.

It only *publishes events* (status changes, GPS pings, stop completions) through
the same App commands the web UI uses - the consumers do everything else.
Speeds follow ml/simdata.py (traffic by hour of day), so the ETA models are
predicting the same world that is being simulated.
"""
from __future__ import annotations

import random
import threading
import time

from .domain import (add_minutes, bearing_deg, haversine_km, interpolate, local_hour, now_ts)
from .ml import simdata


def _pick_vehicle(app, vtype_hint: str | None = None) -> str:
    rows = app.q("SELECT vehicle_id, vehicle_type FROM logistics.vehicle ORDER BY vehicle_id")
    for r in rows:
        if vtype_hint is None or r["vehicle_type"] == vtype_hint:
            return r["vehicle_id"]
    return rows[0]["vehicle_id"]


def drive(app, shipment_id: str, *, until="end", start_time: str | None = None, anomaly: str | None = None,
          per_ping_traces: bool = False, delay: float = 0.0, step_min: float = 2.0, seed: int | None = None,
          sync_every: int = 1, stop_event: threading.Event | None = None, label: str | None = None,
          breakdown_pings: int = 7, final_exception: str | None = None, vehicle_id: str | None = None) -> dict:
    """Drive `shipment_id` forward.

    until          'end' = deliver; 'ofd' = stop once it is out for delivery;
                   a float 0..1 = stop after that fraction of the route
    anomaly        None | 'breakdown' | 'traffic_jam'
    final_exception  if set, the driver reports an EXCEPTION with this reason when the run stops
    """
    rng = random.Random(seed)
    pipe = app.p
    s = app.shipment_row(shipment_id)
    if s["status"] == "DELIVERED":
        return {"shipment_id": shipment_id, "status": "DELIVERED", "pings": 0, "note": "already delivered"}
    wh = app.q1("SELECT g.center_lat AS lat, g.center_lon AS lon "
                "FROM logistics.warehouse w JOIN logistics.geofence g USING (geofence_id) WHERE w.warehouse_id = ?",
                (s["origin_warehouse_id"],))
    dest = (s["dest_lat"], s["dest_lon"])
    start = (s["current_lat"], s["current_lon"]) if s["current_lat"] is not None else (wh["lat"], wh["lon"])

    vehicle_hint = vehicle_id
    vehicle_id = s["vehicle_id"]
    t = start_time or now_ts()
    single = None if per_ping_traces else pipe.new_trace(label or f"Simulated trip {shipment_id}", "simulation")

    def tr(text):
        return pipe.new_trace(text, "simulation") if per_ping_traces else single

    if not vehicle_id:
        vehicle_id = vehicle_hint or _pick_vehicle(app)
        app.assign_vehicle(shipment_id, vehicle_id, event_time=t, trace_id=tr(f"Dispatcher assigns {vehicle_id} to {shipment_id}"))
        pipe.wait_idle()
    vtype = app.q1("SELECT vehicle_type FROM logistics.vehicle WHERE vehicle_id = ?", (vehicle_id,))["vehicle_type"]

    def status_now():
        return app.shipment_row(shipment_id)["status"]

    def cancelled():
        return stop_event is not None and stop_event.is_set()

    # -- leave the warehouse ---------------------------------------------------
    st = status_now()
    if st == "CREATED":
        app.set_status(shipment_id, "PICKED_UP", event_time=t, trace_id=tr(f"{shipment_id}: driver picks up the parcels"))
        pipe.wait_idle()
        t = add_minutes(t, 3)
        st = "PICKED_UP"
    if st in ("PICKED_UP", "EXCEPTION"):
        app.set_status(shipment_id, "IN_TRANSIT", event_time=t, trace_id=tr(f"{shipment_id}: vehicle leaves the warehouse"))
        pipe.wait_idle()
        t = add_minutes(t, 1)

    total_km = max(0.05, haversine_km(*start, *dest))
    traveled = 0.0
    noise = rng.lognormvariate(0.0, 0.12)
    stops_left = app.shipment_row(shipment_id)["stops_remaining"]
    stop_marks = [(k + 1) / (stops_left + 1) for k in range(stops_left)]
    heading0 = bearing_deg(*start, *dest)
    pings = 0
    broke = False

    def ping(lat, lon, speed, note=None):
        nonlocal t, pings
        heading = heading0 + rng.gauss(0, 5)
        app.send_ping(shipment_id, lat, lon, speed, heading, vehicle_id=vehicle_id, ping_time=t,
                      trace_id=tr(note or f"{shipment_id}: GPS ping {lat:.4f},{lon:.4f} @ {speed:.0f} km/h"))
        pings += 1
        if pings % sync_every == 0:
            pipe.wait_idle()
        if delay:
            time.sleep(delay)

    stopped_reason = "arrived"
    limit = None if until in ("end", "ofd", "breakdown") else float(until)
    while not cancelled():
        frac = traveled / total_km
        if limit is not None and frac >= limit:
            stopped_reason = "route fraction reached"
            break
        remaining = total_km - traveled
        if remaining <= 0.03:
            break
        hour = local_hour(t)
        v = simdata.BASE_SPEED[vtype] * simdata.traffic_factor(hour) * noise
        if anomaly == "traffic_jam" and 0.25 <= frac <= 0.75:
            v *= 0.3
        if anomaly == "breakdown" and not broke and frac >= 0.4:
            broke = True
            lat, lon = interpolate(*start, *dest, frac)
            for i in range(breakdown_pings):
                t = add_minutes(t, 5)
                ping(lat, lon, 0.0, f"{shipment_id}: vehicle STANDING STILL at {lat:.4f},{lon:.4f} (breakdown, {5 * (i + 1)} min)")
                if cancelled():
                    break
            if until == "breakdown":
                stopped_reason = "breakdown"
                break
            continue
        step_km = min(remaining, v / simdata.ROAD_FACTOR * step_min / 60.0)
        traveled += step_km
        t = add_minutes(t, step_min)
        lat, lon = interpolate(*start, *dest, traveled / total_km)
        ping(lat, lon, v * rng.gauss(1.0, 0.04))
        if stop_marks and traveled / total_km >= stop_marks[0]:
            stop_marks.pop(0)
            for _ in range(max(1, int(simdata.SERVICE_MIN[vtype] // step_min))):
                t = add_minutes(t, step_min)
                ping(lat, lon, 0.0, f"{shipment_id}: delivery stop at {lat:.4f},{lon:.4f}")
            app.stop_completed(shipment_id, event_time=t, trace_id=tr(f"{shipment_id}: intermediate stop completed"))
            pipe.wait_idle()
        if until == "ofd":
            pipe.wait_idle()
            if status_now() == "OUT_FOR_DELIVERY":
                stopped_reason = "out for delivery"
                break

    pipe.wait_idle()
    if stopped_reason == "arrived" and not cancelled():
        ping(dest[0], dest[1], 0.0, f"{shipment_id}: vehicle at the customer's door")
        pipe.wait_idle()
        t = add_minutes(t, 1)
        if status_now() == "IN_TRANSIT":
            app.set_status(shipment_id, "OUT_FOR_DELIVERY", event_time=t, trace_id=tr(f"{shipment_id}: out for delivery"))
            pipe.wait_idle()
            t = add_minutes(t, 1)
        app.set_status(shipment_id, "DELIVERED", event_time=t, trace_id=tr(f"{shipment_id}: DELIVERED (proof of delivery)"))
        pipe.wait_idle()
    elif final_exception and status_now() not in ("DELIVERED", "EXCEPTION"):
        t = add_minutes(t, 1)
        app.set_status(shipment_id, "EXCEPTION", reason=final_exception, event_time=t,
                       trace_id=tr(f"{shipment_id}: driver reports EXCEPTION ({final_exception})"))
        pipe.wait_idle()
    return {"shipment_id": shipment_id, "status": status_now(), "pings": pings, "sim_end": t,
            "stopped": stopped_reason}


def run_in_background(app, shipment_id: str, **kw) -> threading.Event:
    stop = threading.Event()

    def work():
        try:
            drive(app, shipment_id, stop_event=stop, **kw)
        except Exception as e:      # pragma: no cover
            print(f"[simulator] {shipment_id}: {type(e).__name__}: {e}")

    threading.Thread(target=work, daemon=True, name=f"sim-{shipment_id}").start()
    return stop
