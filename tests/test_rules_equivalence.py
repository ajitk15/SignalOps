"""Proves the declarative rules engine reproduces the legacy if/elif engine.

FrozenLegacyRuleEngine below is a verbatim copy of detection.RuleEngine as it
stood before rules.yaml existed. It lives here, not in detection.py, so the
reference cannot drift with future refactors. Every case feeds the same
observation sequence to both engines and asserts identical outcomes.
"""
from __future__ import annotations

import time
import unittest
from dataclasses import dataclass, field
from typing import Any

from detection import Observation, RuleEngine


@dataclass
class _Finding:
    fingerprint: str
    severity: str
    reason: str
    observation: Observation
    evidence: list[str]


class FrozenLegacyRuleEngine:
    """Frozen copy of the pre-declarative engine. Do not modernise."""

    def __init__(self, *, default_depth_threshold: float = 1000, trend_points: int = 3):
        self.default_depth_threshold = default_depth_threshold
        self.trend_points = trend_points
        self._history: dict[str, list[float]] = {}

    def evaluate(self, obs: Observation):
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
        return _Finding(fingerprint, severity, reason, obs, [reason])


def _obs(metric, value, name="Q1", labels=None, threshold=None, object_type="queue"):
    return Observation("mq_mcp", object_type, name, metric, value,
                       labels=labels or {}, threshold=threshold)


class RulesEquivalenceTests(unittest.TestCase):
    """Each case is a sequence of observations; both engines must agree on
    every step: fire/no-fire, severity, reason text, and fingerprint."""

    CASES = {
        "dlq_by_name_no_role_label": [_obs("queue_depth", 7, name="QL.DLQ.ORDERS", threshold=100)],
        "dlq_by_lowercase_name": [_obs("queue_depth", 1, name="ql.dlq.x", threshold=100)],
        "dlq_by_role_label_only": [_obs("queue_depth", 3, name="QL.PARKED", labels={"role": "dlq"}, threshold=100)],
        "dlq_empty_does_not_fire": [_obs("queue_depth", 0, name="QL.DLQ.ORDERS", threshold=100)],
        "depth_exactly_double_is_p2_inclusive": [_obs("queue_depth", 10, threshold=5)],
        "depth_just_over_is_p3": [_obs("queue_depth", 6, threshold=5)],
        "depth_under_threshold_silent": [_obs("queue_depth", 4, threshold=5)],
        "depth_default_threshold_applies": [_obs("queue_depth", 1500)],
        "rising_three_points": [_obs("queue_depth", v, threshold=100) for v in (1, 2, 3)],
        "rising_needs_strict_increase": [_obs("queue_depth", v, threshold=100) for v in (1, 2, 2)],
        "history_not_starved_by_threshold_wins": (
            # Threshold rule fires on each of the first three; when the target's
            # threshold is later raised, the rising rule must still see the full
            # history — proving state updates are decoupled from rule matching.
            [_obs("queue_depth", v, threshold=100) for v in (101, 102, 103)]
            + [_obs("queue_depth", 104, threshold=1000)]
        ),
        "channel_lowercase_status": [_obs("channel_status", "Retrying", name="CH1", object_type="channel")],
        "channel_running_silent": [_obs("channel_status", "RUNNING", name="CH1", object_type="channel")],
        "channel_inactive_silent": [_obs("channel_status", "inactive", name="CH1", object_type="channel")],
        "ace_flow_stopped": [_obs("ace_flow_status", "Stopped", name="FLOW1", object_type="flow")],
        "ace_flow_started_silent": [_obs("ace_flow_status", "STARTED", name="FLOW1", object_type="flow")],
        "error_count_over_zero_default": [_obs("error_count", 5)],
        "error_count_zero_silent": [_obs("error_count", 0)],
        "exception_count_with_threshold": [_obs("exception_count", 12, threshold=10)],
        "exception_count_at_threshold_silent": [_obs("exception_count", 10, threshold=10)],
        "unknown_metric_silent": [_obs("cpu_percent", 99)],
        "fingerprint_uses_labels": [_obs("queue_depth", 200, threshold=5,
                                         labels={"service": "orders", "environment": "prod"})],
        "fingerprint_falls_back_to_object_name": [_obs("queue_depth", 200, threshold=5)],
    }

    def test_engines_agree_on_every_case(self):
        for case_name, sequence in self.CASES.items():
            with self.subTest(case=case_name):
                legacy = FrozenLegacyRuleEngine(default_depth_threshold=1000, trend_points=3)
                declarative = RuleEngine(default_depth_threshold=1000, trend_points=3)
                for step, obs in enumerate(sequence):
                    expected = legacy.evaluate(obs)
                    actual = declarative.evaluate(obs)
                    if expected is None:
                        self.assertIsNone(actual, f"{case_name}[{step}]: legacy silent, declarative fired: {actual}")
                    else:
                        self.assertIsNotNone(actual, f"{case_name}[{step}]: legacy fired, declarative silent")
                        self.assertEqual(actual.severity, expected.severity, f"{case_name}[{step}]")
                        self.assertEqual(actual.reason, expected.reason, f"{case_name}[{step}]")
                        self.assertEqual(actual.fingerprint, expected.fingerprint, f"{case_name}[{step}]")


if __name__ == "__main__":
    unittest.main()
