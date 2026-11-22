"""Process 5.0  Predict ETA & Detect Anomalies  (ML Service)

For every GPS ping of an active shipment:
  * predict the remaining time with the rule-based, linear-regression and
    gradient-boosting models (all three are stored in ml_prediction so their
    errors can be compared later; gradient boosting is the one used, with the
    rule-based estimate as automatic fallback)
  * score the ping with the Isolation Forest
  * publish ETA_UPDATE when the estimate moved noticeably
  * publish EXCEPTION_RISK / RISK_CLEARED when the shipment becomes (or stops
    being) at risk of delay
When a shipment is DELIVERED, every earlier ETA prediction receives its
abs_error_min (|predicted - actual|) -- the feedback loop behind the
"gradient boosting beats the rule-based ETA" requirement.

Throughput: prepare() computes the features of a whole polled batch and calls each
model once (vectorised); handle() then writes the rows message by message.
"""
from __future__ import annotations

import json

from .. import config
from ..domain import (add_minutes, angle_diff, bearing_deg, haversine_km, local_hour, minutes_between)
from ..events import make_event
from ..ml import simdata
from ..ml.models import MODEL_VERSION
from .base import Consumer, Retry

ACTIVE = ("PICKED_UP", "IN_TRANSIT", "OUT_FOR_DELIVERY")
ETA_CHANGE_MIN = 3.0
MOVING_KMH = 3.0

INSERT_PRED = ("INSERT INTO logistics.ml_prediction (shipment_id, prediction_type, model_name, model_version, features, "
               "predicted_eta, anomaly_score, flagged, created_at) VALUES (?,?,?,?,?::jsonb,?,?,?,?)")


class MLService(Consumer):
    group = "ml-service"
    topics = ["gps-pings", "shipment-events"]

    def __init__(self, pipeline):
        super().__init__(pipeline)
        self.last_eta: dict[str, str] = {}
        self.last_risk: dict[str, bool] = {}
        self._pre: dict[int, dict] = {}

    # ------------------------------------------------------------------
    def _features(self, conn, p):
        """Read the context of one ping and derive the model inputs.  None = shipment not active."""
        sid, t = p["shipment_id"], p["ping_time"]
        s = conn.execute(
            "SELECT s.status, s.dest_lat, s.dest_lon, s.stops_remaining, s.at_risk, s.eta_current, s.delivery_end, "
            "COALESCE(v.vehicle_type, 'VAN') AS vtype FROM shipment s "
            "LEFT JOIN vehicle v ON v.vehicle_id = s.vehicle_id WHERE s.shipment_id = ?", (sid,)).fetchone()
        if s is None:
            raise Retry(f"shipment {sid} not projected yet")
        if s["status"] not in ACTIVE:
            return None
        vtype, hour = s["vtype"], local_hour(t)
        dist = haversine_km(p["lat"], p["lon"], s["dest_lat"], s["dest_lon"])
        expected = simdata.expected_speed(vtype, hour)

        # recent pings (strictly before this one) for the speed / stand-still features
        prev = conn.execute("SELECT ping_time, speed_kmh FROM gps_ping WHERE shipment_id = ? AND ping_time < ? "
                            "ORDER BY ping_time DESC LIMIT 12", (sid, t)).fetchall()
        speeds = [p["speed_kmh"]] + [r["speed_kmh"] for r in prev]
        moving = [v for v in speeds[:5] if v >= MOVING_KMH]
        avg_speed = sum(moving) / len(moving) if moving else expected * 0.3

        stationary_min = 0.0
        if p["speed_kmh"] < MOVING_KMH:
            start = t
            for r in prev:
                if r["speed_kmh"] >= MOVING_KMH:
                    break
                start = r["ping_time"]
            stationary_min = minutes_between(start, t)
        # the bearing to the destination is meaningless within ~500 m of it (the vehicle is at the door)
        heading_dev = (angle_diff(p["heading_deg"], bearing_deg(p["lat"], p["lon"], s["dest_lat"], s["dest_lon"]))
                       if dist >= 0.5 else 0.0)
        return {"s": s, "vtype": vtype, "hour": hour, "dist": dist, "avg_speed": avg_speed,
                "stationary_min": stationary_min, "heading_dev": heading_dev,
                "speed_ratio": p["speed_kmh"] / expected if expected else 1.0}

    def prepare(self, conn, msgs):
        self._pre = {}
        todo = []
        for m in msgs:
            if m.topic != "gps-pings":
                continue
            try:
                f = self._features(conn, m.value)
            except Retry:
                continue                        # handle() will raise it for this message
            if f is not None:
                todo.append((m.id, f))
        if not todo:
            return
        etas = self.models.eta_minutes_batch([(f["dist"], f["avg_speed"], f["s"]["stops_remaining"], f["vtype"], f["hour"]) for _, f in todo])
        anoms = self.models.anomaly_batch([(f["speed_ratio"], f["stationary_min"], f["heading_dev"]) for _, f in todo])
        for (mid, f), eta, an in zip(todo, etas, anoms):
            f["preds"], f["anom"] = eta, an
            self._pre[mid] = f

    # ------------------------------------------------------------------
    def handle(self, conn, m):
        if m.topic == "shipment-events":
            ev = m.value
            if ev["event_type"] == "DELIVERED":
                conn.execute(
                    "UPDATE logistics.ml_prediction SET abs_error_min = ROUND(ABS(EXTRACT(EPOCH FROM (?::timestamptz - predicted_eta))) / 60.0, 2) "
                    "WHERE shipment_id = ? AND prediction_type = 'ETA' AND abs_error_min IS NULL",
                    (ev["event_time"], ev["shipment_id"]))
                self.last_eta.pop(ev["shipment_id"], None)
                self.last_risk.pop(ev["shipment_id"], None)
            return []
        f = self._pre.pop(m.id, None)
        if f is None:                           # not prepared (retry, inactive shipment, or prepare failed)
            f = self._features(conn, m.value)
            if f is None:
                return []
            f["preds"] = self.models.eta_minutes(f["dist"], f["avg_speed"], f["s"]["stops_remaining"], f["vtype"], f["hour"])
            f["anom"] = self.models.anomaly(f["speed_ratio"], f["stationary_min"], f["heading_dev"])
        return self._apply(conn, m.value, f)

    def _apply(self, conn, p, f):
        sid, t, s = p["shipment_id"], p["ping_time"], f["s"]
        preds = f["preds"]
        primary, minutes = self.models.primary_eta(preds)
        feats = {"dist_km": round(f["dist"], 3), "avg_speed_kmh": round(f["avg_speed"], 1),
                 "stops_remaining": s["stops_remaining"], "vehicle_type": f["vtype"], "hour": round(f["hour"], 2)}
        for name, mins in preds.items():
            version = MODEL_VERSION if name != "rule_based" else "rule-1"
            ft = dict(feats, minutes_remaining=round(mins, 1), primary=(name == primary))
            conn.execute(INSERT_PRED, (sid, "ETA", name, version, json.dumps(ft), add_minutes(t, mins), None, False, t))
        eta = add_minutes(t, minutes)

        out = []
        last = self.last_eta.get(sid) or s["eta_current"]
        if last is None or abs(minutes_between(last, eta)) >= ETA_CHANGE_MIN:
            self.last_eta[sid] = eta
            out.append(("shipment-events", sid, make_event(
                "ETA_UPDATE", sid, {"eta": eta, "model_name": primary, "remaining_min": round(minutes, 1),
                                    "dist_km": round(f["dist"], 2)}, event_time=t, producer=self.group)))

        # ---- anomaly --------------------------------------------------
        name, score, raw_flag = f["anom"]
        prev_flag = conn.execute(
            "SELECT flagged FROM ml_prediction WHERE shipment_id = ? AND prediction_type = 'ANOMALY' "
            "ORDER BY prediction_id DESC LIMIT 1", (sid,)).fetchone()
        alert = bool(raw_flag and prev_flag and prev_flag["flagged"])        # two anomalous pings in a row
        af = {"speed_ratio": round(f["speed_ratio"], 2), "stationary_min": round(f["stationary_min"], 1),
              "heading_dev_deg": round(f["heading_dev"], 1), "speed_kmh": p["speed_kmh"]}
        conn.execute(INSERT_PRED, (sid, "ANOMALY", name, MODEL_VERSION if name == "isolation_forest" else "rule-1",
                                   json.dumps(af), None, round(score, 4), bool(raw_flag), t))

        # ---- risk of delay -------------------------------------------
        late_min = minutes_between(s["delivery_end"], eta)
        late = late_min > config.LATE_GRACE_MIN
        risk = alert or late
        was = self.last_risk.get(sid, bool(s["at_risk"]))
        if risk and not was:
            why = []
            if alert:
                why.append(f"anomalous driving behaviour (score {score:.2f}, stationary {f['stationary_min']:.0f} min)")
            if late:
                why.append(f"predicted arrival {late_min:.0f} min after the delivery window")
            self.last_risk[sid] = True
            out.append(("shipment-events", sid, make_event(
                "EXCEPTION_RISK", sid, {"reason": "; ".join(why), "anomaly_score": round(score, 4),
                                        "predicted_eta": eta, "delivery_end": s["delivery_end"]},
                event_time=t, producer=self.group)))
        elif not risk and was:
            self.last_risk[sid] = False
            out.append(("shipment-events", sid, make_event(
                "RISK_CLEARED", sid, {"predicted_eta": eta, "delivery_end": s["delivery_end"]},
                event_time=t, producer=self.group)))
        return out
