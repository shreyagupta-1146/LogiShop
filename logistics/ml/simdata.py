"""Simulated traffic / trip physics.

The spec states the system uses simulated data only.  This module is the single
"ground truth" of how long a trip really takes; both the trip simulator and the
ML training set are generated from it, so the models are learning the same
world the simulator then plays back.
"""
from __future__ import annotations

import numpy as np

BASE_SPEED = {"BIKE": 28.0, "VAN": 24.0, "TRUCK": 20.0}      # free-flow road speed, km/h
SERVICE_MIN = {"BIKE": 4.0, "VAN": 6.0, "TRUCK": 9.0}        # minutes per intermediate stop
VEHICLE_CODE = {"BIKE": 0, "VAN": 1, "TRUCK": 2}
ROAD_FACTOR = 1.35                                           # road distance / straight-line distance

FEATURES = ["dist_km", "avg_speed_kmh", "stops_remaining", "vehicle_code", "hour"]


def traffic_factor(hour):
    """Multiplier on free-flow speed by local hour (works on scalars and arrays)."""
    h = np.asarray(hour, dtype=float)
    f = np.select(
        [(h >= 8) & (h < 10.5), (h >= 17) & (h < 20.5),
         ((h >= 7) & (h < 8)) | ((h >= 10.5) & (h < 11.5)) | ((h >= 16) & (h < 17)) | ((h >= 20.5) & (h < 21.5)),
         (h >= 22) | (h < 5)],
        [0.55, 0.50, 0.80, 1.20], default=1.0)
    return float(f) if f.ndim == 0 else f


def expected_speed(vehicle_type: str, hour: float) -> float:
    return BASE_SPEED[vehicle_type] * traffic_factor(hour)


def remaining_minutes(dist_km, hour, vehicle_type: str, stops, trip_noise: float = 1.0) -> float:
    """True minutes still needed to reach the destination."""
    speed = BASE_SPEED[vehicle_type] * traffic_factor(hour) * trip_noise
    return dist_km * ROAD_FACTOR / speed * 60.0 + stops * SERVICE_MIN[vehicle_type]


def rule_based_minutes(dist_km: float, stops: int) -> float:
    """The dispatcher's naive estimate: 30 km/h everywhere + 5 minutes per stop."""
    return dist_km / 30.0 * 60.0 + stops * 5.0


def make_eta_dataset(n: int = 8000, seed: int = 7):
    rng = np.random.default_rng(seed)
    dist = rng.uniform(0.3, 25.0, n)
    hour = rng.uniform(0.0, 24.0, n)
    vcode = rng.integers(0, 3, n)
    stops = rng.integers(0, 6, n)
    names = np.array(["BIKE", "VAN", "TRUCK"])[vcode]
    noise = rng.lognormal(0.0, 0.12, n)
    base = np.array([BASE_SPEED[v] for v in names])
    serv = np.array([SERVICE_MIN[v] for v in names])
    true_speed = base * traffic_factor(hour) * noise
    y = dist * ROAD_FACTOR / true_speed * 60.0 + stops * serv + rng.normal(0.0, 1.5, n)
    y = np.maximum(y, 0.5)
    observed = true_speed * rng.normal(1.0, 0.06, n)          # what the GPS pings report
    X = np.column_stack([dist, observed, stops, vcode, hour])
    return X, y


def make_normal_ping_dataset(n: int = 8000, seed: int = 11):
    """Features of *normal* driving: [speed_ratio, stationary_min, heading_dev_deg].

    speed_ratio    observed speed / expected speed for this vehicle at this hour
    stationary_min minutes the vehicle has been (almost) standing still
    heading_dev    angle between the vehicle's heading and the bearing to its destination
    Normal driving includes short delivery stops (<= 6 min).
    """
    rng = np.random.default_rng(seed)
    speed_ratio = np.clip(rng.normal(1.0, 0.12, n), 0.4, 1.7)
    stationary = np.zeros(n)
    stopping = rng.random(n) < 0.10
    stationary[stopping] = rng.uniform(0.0, 6.0, stopping.sum())
    speed_ratio[stopping] = rng.uniform(0.0, 0.25, stopping.sum())
    heading_dev = np.clip(np.abs(rng.normal(0.0, 12.0, n)), 0, 180)
    return np.column_stack([speed_ratio, stationary, heading_dev])


def make_ping_training_set(n_normal: int = 8000, seed: int = 11, n_anomalies: int = 160):
    """Training data for the Isolation Forest.

    Real telemetry always contains a small fraction of unlabelled anomalies (~2 %),
    so the training set mixes them in: breakdown-like (long stand-still) and
    wrong-way-like (heading far from the destination).  Returns (X_train, X_normal);
    X_normal is used only to calibrate the alert threshold.
    """
    normal = make_normal_ping_dataset(n_normal, seed)
    rng = np.random.default_rng(seed + 1)
    k = n_anomalies // 2
    breakdown = np.column_stack([rng.uniform(0.0, 0.15, k), rng.uniform(12.0, 50.0, k),
                                 np.abs(rng.normal(0.0, 20.0, k))])
    wrong_way = np.column_stack([np.clip(rng.normal(0.6, 0.3, k), 0.0, 1.5), rng.uniform(0.0, 3.0, k),
                                 rng.uniform(90.0, 180.0, k)])
    return np.vstack([normal, breakdown, wrong_way]), normal
