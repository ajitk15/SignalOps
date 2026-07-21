"""Deterministic, zero-LLM signal detection and incident correlation."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


SEVERITY_RANK = {"P1": 1, "P2": 2, "P3": 3, "P4": 4}


@dataclass
class Observation:
    source: str
    object_type: str
    object_name: str
    metric: str
    value: Any
    timestamp: float = field(default_factory=time.time)
    labels: dict[str, str] = field(default_factory=dict)
    threshold: float | None = None


@dataclass
class Finding:
    fingerprint: str
    severity: str
    reason: str
    observation: Observation
    evidence: list[str]


class RuleEngine:
    def __init__(self, *, default_depth_threshold: float = 1000, trend_points: int = 3):
        self.default_depth_threshold = default_depth_threshold
        self.trend_points = trend_points
        self._history: dict[str, list[float]] = {}

    def evaluate(self, obs: Observation) -> Finding | None:
        key = f"{obs.source}:{obs.object_type}:{obs.object_name}:{obs.metric}"
        value_text = str(obs.value).upper()
        reason = ""
        severity = "P3"

        if obs.metric == "queue_depth":
            depth = float(obs.value)
            threshold = obs.threshold if obs.threshold is not None else self.default_depth_threshold
            history = self._history.setdefault(key, [])
            history.append(depth)
            del history[:-self.trend_points]
            is_dlq = "DLQ" in obs.object_name.upper() or obs.labels.get("role") == "dlq"
            rising = len(history) >= self.trend_points and all(a < b for a, b in zip(history, history[1:]))
            if is_dlq and depth > 0:
                reason, severity = f"DLQ contains {int(depth)} message(s)", "P2"
            elif depth > threshold:
                reason = f"Queue depth {int(depth)} exceeds threshold {int(threshold)}"
                severity = "P2" if depth >= threshold * 2 else "P3"
            elif rising:
                reason = f"Queue depth is rising across {self.trend_points} observations: {history}"
        elif obs.metric == "channel_status" and value_text not in {"RUNNING", "INACTIVE"}:
            reason, severity = f"Channel status is {value_text}", "P2"
        elif obs.metric == "ace_flow_status" and value_text not in {"RUNNING", "STARTED"}:
            reason, severity = f"ACE flow status is {value_text}", "P2"
        elif obs.metric in {"error_count", "exception_count"} and float(obs.value) > (obs.threshold or 0):
            reason, severity = f"{obs.metric} is {obs.value}", "P2"

        if not reason:
            return None
        service = obs.labels.get("service", obs.object_name)
        fingerprint = f"{obs.labels.get('environment','unknown')}:{service}:{obs.metric}"
        return Finding(fingerprint, severity, reason, obs, [reason])


class Correlator:
    """Suppresses repeated findings while an incident is already active."""
    def __init__(self, dedup_window_seconds: int = 900):
        self.dedup_window_seconds = dedup_window_seconds
        self._last_seen: dict[str, float] = {}

    def is_new(self, finding: Finding, now: float | None = None) -> bool:
        now = now or time.time()
        previous = self._last_seen.get(finding.fingerprint)
        if previous is None or now - previous >= self.dedup_window_seconds:
            self._last_seen[finding.fingerprint] = now
            return True
        return False
