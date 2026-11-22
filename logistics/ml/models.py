"""ETA regression models and the Isolation-Forest anomaly detector.

Three ETA estimators are compared, exactly as in the requirement specification:
    rule_based          dist / 30 km/h + 5 min per stop
    linear_regression   scikit-learn LinearRegression
    gradient_boosting   scikit-learn GradientBoostingRegressor   (the primary model)
If scikit-learn or the trained files are unavailable the system falls back to
the rule-based estimate (ETA) and a heuristic score (anomaly).
"""
from __future__ import annotations

import json
import threading

import numpy as np

from .. import config
from . import simdata

try:
    import joblib
    from sklearn.ensemble import GradientBoostingRegressor, IsolationForest
    from sklearn.linear_model import LinearRegression
    from sklearn.metrics import mean_absolute_error
    from sklearn.model_selection import train_test_split
    SKLEARN = True
except Exception:       # pragma: no cover - exercised only without scikit-learn
    SKLEARN = False

MODEL_VERSION = "1.0"
REPORT_PATH = config.MODEL_DIR / "report.json"


class Models:
    def __init__(self):
        self.lr = None
        self.gbm = None
        self.iforest = None
        self.threshold = 0.6
        self.enabled = True              # the UI can switch ML off to demonstrate the fallback
        self.lock = threading.Lock()

    @property
    def eta_ready(self) -> bool:
        return self.enabled and SKLEARN and self.gbm is not None

    @property
    def anomaly_ready(self) -> bool:
        return self.enabled and SKLEARN and self.iforest is not None

    # -- ETA -------------------------------------------------------------
    def eta_minutes_batch(self, rows) -> list[dict[str, float]]:
        """rows: [(dist_km, avg_speed, stops, vehicle_type, hour)] -> remaining minutes per model.

        One vectorised call per model for the whole batch: scikit-learn's per-call overhead
        dominates single-row predictions, so batching is what makes the ML consumer fast.
        """
        out = [{"rule_based": simdata.rule_based_minutes(r[0], r[2])} for r in rows]
        if rows and SKLEARN and self.lr is not None and self.gbm is not None and self.enabled:
            x = np.array([[r[0], r[1], r[2], simdata.VEHICLE_CODE[r[3]], r[4]] for r in rows])
            with self.lock:
                lr, gbm = self.lr.predict(x), self.gbm.predict(x)
            for o, a, b in zip(out, lr, gbm):
                o["linear_regression"] = max(0.5, float(a))
                o["gradient_boosting"] = max(0.5, float(b))
        return out

    def eta_minutes(self, dist_km, avg_speed, stops, vehicle_type, hour) -> dict[str, float]:
        return self.eta_minutes_batch([(dist_km, avg_speed, stops, vehicle_type, hour)])[0]

    def primary_eta(self, preds: dict[str, float]) -> tuple[str, float]:
        if "gradient_boosting" in preds:
            return "gradient_boosting", preds["gradient_boosting"]
        return "rule_based", preds["rule_based"]          # fallback

    # -- anomaly ---------------------------------------------------------
    def anomaly_batch(self, rows) -> list[tuple[str, float, bool]]:
        """rows: [(speed_ratio, stationary_min, heading_dev)] -> [(model, score 0..1, flagged)]."""
        if rows and self.anomaly_ready:
            with self.lock:
                raw = -self.iforest.score_samples(np.array(rows))
            return [("isolation_forest", min(1.0, max(0.0, float(s))), float(s) >= self.threshold) for s in raw]
        out = []                                                   # heuristic fallback
        for _, stationary_min, heading_dev in rows:
            score = min(1.0, max(stationary_min / 20.0, (heading_dev - 60) / 120.0, 0.0))
            out.append(("heuristic_rules", score, score >= 0.6))
        return out

    def anomaly(self, speed_ratio, stationary_min, heading_dev) -> tuple[str, float, bool]:
        return self.anomaly_batch([(speed_ratio, stationary_min, heading_dev)])[0]


def train(verbose: bool = False) -> dict:
    """Train all models on simulated data, evaluate on a held-out split, persist."""
    if not SKLEARN:
        raise RuntimeError("scikit-learn is not installed")
    X, y = simdata.make_eta_dataset()
    Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.25, random_state=3)
    lr = LinearRegression().fit(Xtr, ytr)
    gbm = GradientBoostingRegressor(n_estimators=250, max_depth=3, learning_rate=0.08,
                                    subsample=0.9, random_state=0).fit(Xtr, ytr)
    rule = np.array([simdata.rule_based_minutes(r[0], r[2]) for r in Xte])
    mae = {
        "rule_based": float(mean_absolute_error(yte, rule)),
        "linear_regression": float(mean_absolute_error(yte, lr.predict(Xte))),
        "gradient_boosting": float(mean_absolute_error(yte, gbm.predict(Xte))),
    }

    Xtrain, Xn = simdata.make_ping_training_set()
    iso = IsolationForest(n_estimators=300, max_samples=256, contamination="auto", random_state=0).fit(Xtrain)
    normal_scores = -iso.score_samples(Xn)
    threshold = float(max(0.6, normal_scores.max() + 0.005))   # nothing in normal driving exceeds it
    # sanity probes: a sustained breakdown must be flagged, normal driving and delivery stops must not
    probes = {
        "normal": [1.0, 0.0, 10.0], "delivery_stop": [0.05, 4.0, 15.0],
        "breakdown_20min": [0.0, 20.0, 10.0], "breakdown_25min": [0.0, 25.0, 10.0],
        "wrong_way": [1.0, 0.0, 120.0], "slow_and_off_route": [0.3, 0.0, 110.0],
    }
    probe_scores = {k: float(-iso.score_samples(np.array([v]))[0]) for k, v in probes.items()}

    config.MODEL_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump({"lr": lr, "gbm": gbm, "iforest": iso, "threshold": threshold, "version": MODEL_VERSION},
                config.MODEL_DIR / "models.joblib")
    report = {
        "model_version": MODEL_VERSION,
        "eta_holdout_samples": int(len(yte)),
        "eta_mae_minutes": {k: round(v, 3) for k, v in mae.items()},
        "gbm_beats_rule_based": mae["gradient_boosting"] < mae["rule_based"],
        "anomaly_threshold": round(threshold, 4),
        "anomaly_probe_scores": {k: round(v, 4) for k, v in probe_scores.items()},
        "training_data": "simulated (logistics/ml/simdata.py)",
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if verbose:
        print(json.dumps(report, indent=2))
    return report


def load_or_train(verbose: bool = False) -> Models:
    m = Models()
    if not SKLEARN:
        if verbose:
            print("[ml] scikit-learn not available -> rule-based fallback")
        return m
    path = config.MODEL_DIR / "models.joblib"
    try:
        if not path.exists():
            if verbose:
                print("[ml] training models on simulated trips ...")
            train(verbose)
        d = joblib.load(path)
        m.lr, m.gbm, m.iforest, m.threshold = d["lr"], d["gbm"], d["iforest"], d["threshold"]
    except Exception as e:      # corrupt file etc. -> retrain once, else fall back
        try:
            train(verbose)
            d = joblib.load(path)
            m.lr, m.gbm, m.iforest, m.threshold = d["lr"], d["gbm"], d["iforest"], d["threshold"]
        except Exception as e2:
            if verbose:
                print(f"[ml] could not load/train models ({e}; {e2}) -> rule-based fallback")
    return m


def load_report() -> dict | None:
    try:
        return json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


if __name__ == "__main__":     # python -m logistics.ml.models
    train(verbose=True)
