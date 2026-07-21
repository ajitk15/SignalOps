"""Deterministic, zero-LLM signal detection and incident correlation.

Detection rules live in config/rules.yaml; this module only provides the
evaluation semantics. Adding a platform's rules is a config edit, not code.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SEVERITY_RANK = {"P1": 1, "P2": 2, "P3": 3, "P4": 4}
RULES_PATH = Path(__file__).resolve().parent / "config" / "rules.yaml"
# UI-created rules live in their own file, appended AFTER built-ins: with
# first-match-wins they add detections but cannot shadow built-in behaviour.
CUSTOM_RULES_PATH = Path(__file__).resolve().parent / "config" / "rules.custom.yaml"


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


DEFAULT_SEVERITY = "P3"
# A rule may set severity to a fixed P1-P4, omit it (default), or delegate to
# the AI. AI mode needs a provisional severity because severity is what decides
# whether AI runs at all — see AI_SEVERITY_MODE below.
AI_SEVERITY_MODE = "ai"


@dataclass
class Finding:
    fingerprint: str
    severity: str
    reason: str
    observation: Observation
    evidence: list[str]
    # "rule"    — operator set an explicit severity; AI must not override it.
    # "default" — nothing configured, fell back to DEFAULT_SEVERITY.
    # "ai"      — provisional only; the AI's verdict replaces it when it runs.
    severity_source: str = "rule"


def merge_rules(builtin: list[dict], custom: list[dict]) -> list[dict]:
    """Combine shipped rules with the UI-managed ones.

    A custom entry sharing a built-in's id REPLACES it in place, so editing a
    shipped rule cannot silently reorder evaluation (order is behaviour under
    first-match-wins). `disabled: true` removes a rule. Genuinely new custom
    rules append after the built-ins, where they can add detections without
    shadowing shipped behaviour.
    """
    overrides = {rule["id"]: rule for rule in custom}
    merged = []
    for rule in builtin:
        override = overrides.pop(rule["id"], None)
        if override is None:
            merged.append(rule)
        elif not override.get("disabled"):
            merged.append(override)
    merged.extend(rule for rule in overrides.values() if not rule.get("disabled"))
    return merged


class RuleEngine:
    """Evaluates config/rules.yaml against observations, first match wins.

    Four semantics preserve the original hardcoded elif engine exactly:
    - a rule whose `when` matches but whose condition is false falls through;
    - trend history updates for every observation of a metric that has a
      `rising` rule, before rule selection, so a higher rule winning cannot
      starve a later rule's state;
    - `not_in` comparisons are case-folded;
    - `escalate.at_factor` is inclusive (value >= threshold * factor).
    """

    def __init__(self, *, default_depth_threshold: float = 1000, trend_points: int = 3,
                 rules_path: Path | None = None):
        # Settings referenced from rules.yaml via "${name}" placeholders.
        self.settings = {"default_depth_threshold": default_depth_threshold}
        self.trend_points = trend_points
        builtin = yaml.safe_load((rules_path or RULES_PATH).read_text(encoding="utf-8"))["rules"]
        custom = []
        if rules_path is None and CUSTOM_RULES_PATH.exists():
            custom = (yaml.safe_load(CUSTOM_RULES_PATH.read_text(encoding="utf-8")) or {}).get("rules", [])
        self.rules = merge_rules(builtin, custom)
        self._history: dict[str, list[float]] = {}
        # Metrics with at least one `rising` rule need history for every
        # observation, whichever rule ends up firing.
        self._stateful_metrics = {metric for rule in self.rules
                                  if rule["condition"]["type"] == "rising"
                                  for metric in self._metrics_of(rule)}

    @staticmethod
    def _metrics_of(rule: dict) -> list[str]:
        metric = rule.get("when", {}).get("metric", [])
        return metric if isinstance(metric, list) else [metric]

    def evaluate(self, obs: Observation) -> Finding | None:
        history = self._update_history(obs)
        for rule in self.rules:
            if not self._when_matches(rule.get("when", {}), obs):
                continue
            context = self._check_condition(rule["condition"], obs, history)
            if context is None:
                continue  # condition false — fall through, as the elif chain did
            severity, source = self._severity_for(rule)
            escalate = rule.get("escalate")
            if escalate and context.get("threshold") is not None \
                    and float(obs.value) >= context["threshold"] * escalate["at_factor"]:
                severity = escalate["severity"]
            reason = rule["message"].format(**context)
            service = obs.labels.get("service", obs.object_name)
            fingerprint = f"{obs.labels.get('environment', 'unknown')}:{service}:{obs.metric}"
            return Finding(fingerprint, severity, reason, obs, [reason], severity_source=source)
        return None

    @staticmethod
    def _severity_for(rule: dict) -> tuple[str, str]:
        """Resolve a rule's severity and where it came from.

        AI mode is deliberately given a concrete provisional severity rather
        than nothing: severity is the input to the AI cost gate, so a rule that
        wants AI to decide must still be rankable before AI has run. It also
        means an AI-mode rule degrades to a sensible fixed severity when
        ENABLE_INCIDENT_AI is off, instead of failing.
        """
        configured = rule.get("severity")
        if configured is None:
            return DEFAULT_SEVERITY, "default"
        if str(configured).lower() == AI_SEVERITY_MODE:
            return rule.get("ai_provisional", DEFAULT_SEVERITY), "ai"
        return configured, "rule"

    # -- state -----------------------------------------------------------------

    def _update_history(self, obs: Observation) -> list[float] | None:
        if obs.metric not in self._stateful_metrics:
            return None
        key = f"{obs.source}:{obs.object_type}:{obs.object_name}:{obs.metric}"
        history = self._history.setdefault(key, [])
        history.append(float(obs.value))
        del history[:-self.trend_points]
        return history

    # -- matching --------------------------------------------------------------

    def _when_matches(self, when: dict, obs: Observation) -> bool:
        metrics = self._metrics_of({"when": when})
        if metrics and obs.metric not in metrics:
            return False
        any_of = when.get("any_of")
        if any_of and not any(self._selector_matches(selector, obs) for selector in any_of):
            return False
        return True

    @staticmethod
    def _selector_matches(selector: dict, obs: Observation) -> bool:
        if "name_contains" in selector:
            return selector["name_contains"].upper() in obs.object_name.upper()
        if "label_equals" in selector:
            return all(obs.labels.get(k) == v for k, v in selector["label_equals"].items())
        return False

    # -- conditions ------------------------------------------------------------

    def _resolve(self, value):
        """Resolve "${threshold}"-style placeholders against engine settings."""
        if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
            return self.settings[value[2:-1]]
        return value

    def _check_condition(self, condition: dict, obs: Observation,
                         history: list[float] | None) -> dict | None:
        """Return the message-template context when true, else None."""
        kind = condition["type"]

        if kind == "greater_than":
            numeric = float(obs.value)
            configured = condition["value"]
            if configured == "${threshold}":
                threshold = obs.threshold if obs.threshold is not None \
                    else float(self._resolve(condition.get("default", 0)))
            else:
                threshold = float(self._resolve(configured))
            if numeric > threshold:
                return self._context(obs, history, threshold=threshold)
            return None

        if kind == "not_in":
            value_text = str(obs.value).upper()
            if value_text not in {str(v).upper() for v in condition["values"]}:
                return self._context(obs, history)
            return None

        if kind == "rising":
            if history is not None and len(history) >= self.trend_points \
                    and all(a < b for a, b in zip(history, history[1:])):
                return self._context(obs, history)
            return None

        raise ValueError(f"unknown condition type: {kind}")

    def _context(self, obs: Observation, history: list[float] | None,
                 threshold: float | None = None) -> dict:
        context = {"value": obs.value, "value_text": str(obs.value).upper(),
                   "metric": obs.metric, "object_name": obs.object_name,
                   "history": history, "trend_points": self.trend_points,
                   "threshold": threshold}
        try:
            context["value_int"] = int(float(obs.value))
        except (TypeError, ValueError):
            pass
        if threshold is not None:
            context["threshold_int"] = int(threshold)
        return context


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
