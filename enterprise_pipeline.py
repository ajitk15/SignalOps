"""Cost-gated enterprise incident pipeline.

Signals enter as observations. Rules, correlation and KB lookup cost no LLM
tokens. AI is invoked only for a new incident without a strong KB match.
"""
from __future__ import annotations
import asyncio, json, logging, os, time
from pathlib import Path
import yaml
from dataclasses import asdict
from agents import diagnostician, report_writer
from agents.common import AgentCallResult, load_agent_models, load_watchlist
from detection import Correlator, Finding, Observation, RuleEngine, SEVERITY_RANK
from events import Event, bus
from integrations.context import readers_from_env
from integrations.servicenow import reader_from_env as servicenow_reader
from knowledge.service import search as search_kb
from platforms import PlatformProfile, profile_for
import store

logger = logging.getLogger("enterprise_pipeline")
AI_AGENT_TIMEOUT_SECONDS = int(os.getenv("AI_AGENT_TIMEOUT_SECONDS", "120"))

class EnterprisePipeline:
    def __init__(self, *, use_ai: bool = True, rule_engine=None, correlator=None):
        config_path = Path(__file__).resolve().parent / "config" / "enterprise.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        detection_cfg, knowledge_cfg = config["detection"], config["knowledge"]
        self.use_ai = use_ai
        self._detection_cfg = detection_cfg
        self.rules = rule_engine or RuleEngine(default_depth_threshold=detection_cfg["default_depth_threshold"],
                                               trend_points=detection_cfg["trend_points"])
        self.correlator = correlator or Correlator(detection_cfg["dedup_window_seconds"])
        self.minimum_ai_severity = detection_cfg["minimum_ai_severity"]
        self.maximum_ai_calls = detection_cfg["maximum_ai_calls_per_incident"]
        self.kb_search_threshold = knowledge_cfg["similarity_threshold"]
        self.kb_reuse_threshold = knowledge_cfg["zero_ai_reuse_threshold"]
        self.models = load_agent_models()
        # Last reading per watched object, so a dashboard loading mid-cycle can
        # render current state instead of waiting for the next poll's event.
        # Keyed by (source, object_name): two sources may watch same-named objects.
        self.latest_observations: dict[tuple[str, str], dict] = {}
        # Source name -> platform profile, from the watchlist. Push sources that
        # are not in the watchlist fall back to the generic profile.
        try:
            self._platform_by_source = {s.name: s.platform for s in load_watchlist().sources}
        except Exception:
            logger.exception("could not load watchlist for platform mapping; using generic profiles")
            self._platform_by_source = {}

    def _profile(self, observation: Observation) -> PlatformProfile:
        return profile_for(self._platform_by_source.get(observation.source))

    def watched_objects(self) -> list[dict]:
        return list(self.latest_observations.values())

    def reload_rules(self) -> None:
        """Rebuild the rule engine after a rules file change. Trend history
        resets — acceptable for a rule edit, and stated in the UI."""
        self.rules = RuleEngine(default_depth_threshold=self._detection_cfg["default_depth_threshold"],
                                trend_points=self._detection_cfg["trend_points"])

    async def ingest(self, observation: Observation) -> dict:
        finding = self.rules.evaluate(observation)
        bus.publish(Event("observation_received", {"observation": asdict(observation), "finding": asdict(finding) if finding else None}))
        self.latest_observations[(observation.source, observation.object_name)] = {
            "source": observation.source,
            "object_name": observation.object_name, "object_type": observation.object_type,
            "status": "anomaly" if finding else "ok", "timestamp": observation.timestamp}
        if not finding: return {"outcome": "healthy", "ai_calls": 0}
        if not self.correlator.is_new(finding): return {"outcome": "deduplicated", "fingerprint": finding.fingerprint, "ai_calls": 0}

        kb_matches = search_kb(f"{finding.reason} {observation.object_name}", self.kb_search_threshold)
        context = await self._historical_context(finding)
        if kb_matches and kb_matches[0]["score"] >= self.kb_reuse_threshold:
            report = {"title": f"Known issue: {finding.reason}", "severity": finding.severity,
                      "markdown_report": kb_matches[0]["content"] + "\n\nNo changes were made; remediation requires an authorised operator."}
            diagnosis = {"root_cause_hypothesis": "Matched an approved KB article", "confidence": "high",
                         "severity": finding.severity, "evidence": finding.evidence, "kb_match": kb_matches[0]["title"]}
            incident_id = self._save(finding, diagnosis, report, 0.0, context, "kb_reuse")
            return {"outcome": "incident_created", "incident_id": incident_id, "route": "kb_reuse", "ai_calls": 0}

        profile = self._profile(observation)
        ai_severity_allowed = SEVERITY_RANK[finding.severity] <= SEVERITY_RANK[self.minimum_ai_severity]
        if not self.use_ai or not ai_severity_allowed or self.maximum_ai_calls < 2:
            diagnosis = {"root_cause_hypothesis": "Rule-based detection requires investigation", "confidence": "low",
                         "severity": finding.severity, "evidence": finding.evidence, "historical_context": context}
            report = {"title": finding.reason, "severity": finding.severity,
                      "markdown_report": f"## Evidence\n- {finding.reason}\n\n## Next step\n{profile.next_step}\n\nNo changes were made."}
            incident_id = self._save(finding, diagnosis, report, 0.0, context, "rule_only")
            return {"outcome": "incident_created", "incident_id": incident_id, "route": "rule_only", "ai_calls": 0}

        anomaly = asdict(finding.observation) | {"reason": finding.reason, "severity": finding.severity,
                                                  "historical_context": context, "kb_matches": kb_matches[:3]}
        ai_cost = 0.0
        active_agent = "diagnostician"
        try:
            bus.publish(Event("agent_started", {"agent": active_agent, "object_name": observation.object_name}))
            diag = await asyncio.wait_for(
                diagnostician.diagnose(self.models.diagnostician, anomaly, profile), timeout=AI_AGENT_TIMEOUT_SECONDS
            )
            ai_cost += diag.cost_usd
            bus.publish(Event("agent_completed", {"agent": active_agent, "object_name": observation.object_name,
                                                    "model": diag.model, "cost_usd": diag.cost_usd}))
            diagnosis = diag.parsed or {}
            active_agent = "report_writer"
            bus.publish(Event("agent_started", {"agent": active_agent, "object_name": observation.object_name}))
            report_result = await asyncio.wait_for(
                report_writer.write_report(self.models.report_writer, anomaly, diagnosis, profile), timeout=AI_AGENT_TIMEOUT_SECONDS
            )
            ai_cost += report_result.cost_usd
            bus.publish(Event("agent_completed", {"agent": active_agent, "object_name": observation.object_name,
                                                    "model": report_result.model, "cost_usd": report_result.cost_usd}))
            report = report_result.parsed or {}
            incident_id = self._save(finding, diagnosis, report, ai_cost, context, "ai")
            return {"outcome": "incident_created", "incident_id": incident_id, "route": "ai", "ai_calls": 2}
        except Exception as exc:
            logger.exception("AI investigation failed for %s", observation.object_name)
            bus.publish(Event("agent_failed", {"agent": active_agent, "object_name": observation.object_name}))
            diagnosis = {"root_cause_hypothesis": "AI investigation unavailable; manual investigation required",
                         "confidence": "low", "severity": finding.severity, "evidence": finding.evidence,
                         "ai_error": str(exc)}
            report = {"title": finding.reason, "severity": finding.severity,
                      "markdown_report": f"## Evidence\n- {finding.reason}\n\n## Next step\n{profile.next_step} AI investigation did not complete.\n\nNo changes were made."}
            incident_id = self._save(finding, diagnosis, report, ai_cost, context, "ai_failed_rule_only")
            return {"outcome": "incident_created", "incident_id": incident_id, "route": "ai_failed_rule_only", "ai_calls": 0}

    async def _historical_context(self, finding: Finding) -> dict:
        splunk, dynatrace = readers_from_env(); result = {}
        service = finding.observation.labels.get("service", finding.observation.object_name)
        if splunk:
            try: result["splunk"] = await asyncio.to_thread(splunk.search, f'service="{service}" (ERROR OR WARN)')
            except Exception as exc: result["splunk_error"] = str(exc)
        if dynatrace:
            try: result["dynatrace"] = await asyncio.to_thread(dynatrace.problems, f'type("SERVICE"),entityName.equals("{service}")')
            except Exception as exc: result["dynatrace_error"] = str(exc)
        servicenow = servicenow_reader()
        if servicenow:
            # "What changed?" — the first question on-call asks. Truncated hard:
            # this text reaches agent prompts and is treated there as untrusted
            # evidence, never as instructions.
            try:
                changes = await asyncio.to_thread(servicenow.recent_changes, service)
                result["recent_changes"] = [
                    {k: str(change.get(k, ""))[:200] for k in ("number", "short_description", "state", "sys_updated_on")}
                    for change in changes[:5]]
            except Exception as exc:
                result["servicenow_error"] = str(exc)
        return result

    def _save(self, finding, diagnosis, report, cost, context, route):
        snapshot = asdict(finding.observation) | {"reason": finding.reason, "fingerprint": finding.fingerprint, "context": context}
        # An explicitly configured severity is the operator's decision and the
        # AI must not quietly downgrade it. Only "default" and "ai" severities
        # are provisional, so only those defer to the AI's verdict.
        severity = finding.severity if finding.severity_source == "rule" \
            else report.get("severity", finding.severity)
        title = report.get("title", finding.reason)
        created_at = time.time()
        incident_id = store.save_incident(object_name=finding.observation.object_name, object_type=finding.observation.object_type,
            severity=severity, title=title,
            markdown_report=report.get("markdown_report", ""), watcher_json=snapshot, diagnosis_json=diagnosis,
            report_json=report | {"route": route}, total_cost_usd=cost, trigger_source=finding.observation.source,
            created_at=created_at)
        # The dashboard renders this row directly, so it must carry everything
        # /api/incidents would return — otherwise a live row shows a blank
        # severity until a reload replaces it with the stored version.
        bus.publish(Event("incident_created", {"incident_id": incident_id, "title": title, "route": route,
                                               "total_cost_usd": cost, "severity": severity,
                                               "object_name": finding.observation.object_name,
                                               "object_type": finding.observation.object_type,
                                               "severity_source": finding.severity_source,
                                               "trigger_source": finding.observation.source,
                                               "created_at": created_at}))
        return incident_id
