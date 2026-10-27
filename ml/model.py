"""The trained scorer: load coefficients, predict, assign a band, explain."""
import json
import math
from functools import lru_cache
from pathlib import Path

ARTIFACT_PATH = Path(__file__).resolve().parent / "artifacts" / "hit_model.json"
DEFAULT_AUTO_CLEAR = 0.20      # the upper limit for auto-clear (training may lower it)
ESCALATE_THRESHOLD = 0.80


def _sigmoid(z: float) -> float:
    """Squash any number into 0..1 (written to avoid overflow)."""
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


class HitModel:
    def __init__(self, data: dict):
        self.data = data
        self.version = data["model_version"]
        self.feature_names = data["feature_names"]
        self.coefficients = data["coefficients"]
        self.intercept = data["intercept"]
        self.auto_clear = data["thresholds"]["auto_clear"]
        self.escalate = data["thresholds"]["escalate"]

    def _log_odds(self, features: dict) -> float:
        return self.intercept + sum(
            w * features[n] for n, w in zip(self.feature_names, self.coefficients))

    def predict(self, features: dict) -> float:
        return _sigmoid(self._log_odds(features))

    def band(self, probability: float) -> str:
        if probability < self.auto_clear:
            return "AUTO_CLEAR"
        if probability > self.escalate:
            return "ESCALATE"
        return "REVIEW"

    def explain(self, features: dict) -> dict:
        """Each feature's contribution = coefficient x value. Contributions plus
        the intercept add up to the log-odds, so nothing is hidden."""
        z, rows = self.intercept, []
        for name, weight in zip(self.feature_names, self.coefficients):
            value = float(features[name])
            contribution = weight * value
            z += contribution
            rows.append({"name": name, "value": round(value, 4),
                         "weight": round(weight, 4),
                         "contribution": round(contribution, 4)})
        probability = _sigmoid(z)
        rows.sort(key=lambda r: abs(r["contribution"]), reverse=True)
        return {
            "model_version": self.version,
            "intercept": round(self.intercept, 4),
            "log_odds": round(z, 4),
            "probability": round(probability, 6),
            "band": self.band(probability),
            "thresholds": {"auto_clear": self.auto_clear, "escalate": self.escalate},
            "features": rows,
            "supports_match": [r["name"] for r in rows if r["contribution"] >= 0.5],
            "missing_evidence": [r["name"] for r in rows
                                 if r["weight"] > 0 and r["value"] < 0.5],
        }


def load_model(path: Path = ARTIFACT_PATH) -> HitModel:
    if not path.exists():
        raise FileNotFoundError(
            "Model file not found. Run first: python -m scripts.train_model")
    return HitModel(json.loads(path.read_text(encoding="utf-8")))


@lru_cache(maxsize=1)
def get_model() -> HitModel:
    """Load once per server start. Restart the server after retraining."""
    return load_model()