import asyncio
import logging
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import collector_loop
from agents.common import Watchlist
from collector_loop import collect_forever, collector_health
import store
from detection import Correlator, Finding, Observation, RuleEngine
from enterprise_pipeline import EnterprisePipeline
from events import bus
from integrations.mq_ace_mcp import _queue_readings, _split_multi
from integrations.context import _get

class DetectionTests(unittest.TestCase):
    def test_healthy_queue_costs_no_ai(self):
        pipeline = EnterprisePipeline(use_ai=False)
        result = asyncio.run(pipeline.ingest(Observation("simulation", "queue", "Q1", "queue_depth", 2, threshold=10)))
        self.assertEqual(result, {"outcome": "healthy", "ai_calls": 0})

    def test_duplicate_is_suppressed(self):
        pipeline = EnterprisePipeline(use_ai=False, correlator=Correlator(900))
        obs = Observation("simulation", "queue", "Q1", "queue_depth", 20, labels={"service":"orders"}, threshold=10)
        with tempfile.TemporaryDirectory() as temp:
            with patch("store.DB_PATH", Path(temp) / "incidents.db"):
                first = asyncio.run(pipeline.ingest(obs)); second = asyncio.run(pipeline.ingest(obs))
        self.assertEqual(first["outcome"], "incident_created")
        self.assertEqual(second["outcome"], "deduplicated")
        self.assertEqual(second["ai_calls"], 0)

    def test_rising_depth_rule(self):
        rules = RuleEngine(default_depth_threshold=999, trend_points=3)
        values = [1, 2, 3]
        findings = [rules.evaluate(Observation("mq_mcp", "queue", "Q2", "queue_depth", v)) for v in values]
        self.assertIsNone(findings[0]); self.assertIsNone(findings[1]); self.assertIsNotNone(findings[2])

    def test_bad_channel_detected(self):
        finding = RuleEngine().evaluate(Observation("mq_mcp", "channel", "CH1", "channel_status", "RETRYING"))
        self.assertEqual(finding.severity, "P2")

    def test_live_mcp_queue_format(self):
        text = "══ Queue: QL.INPUT ══\nResolution chain: QL.INPUT(QM1)\nCURDEPTH(12)\nResolution chain: QL.INPUT(QM2)\nCURDEPTH(0)"
        self.assertIn("QL.INPUT", _split_multi(text))
        self.assertEqual(_queue_readings(_split_multi(text)["QL.INPUT"]), [("QM1", 12), ("QM2", 0)])

class _StopLoop(BaseException):
    """Breaks collect_forever's infinite loop without the `except Exception` catching it."""


class DashboardStateTests(unittest.TestCase):
    """Covers what the WebSocket state_snapshot frame is built from."""

    def test_watched_objects_records_healthy_and_anomalous_readings(self):
        pipeline = EnterprisePipeline(use_ai=False)
        with tempfile.TemporaryDirectory() as temp:
            with patch("store.DB_PATH", Path(temp) / "incidents.db"):
                asyncio.run(pipeline.ingest(Observation("mq_mcp", "queue", "QM1/OK", "queue_depth", 1, threshold=10)))
                asyncio.run(pipeline.ingest(Observation("mq_mcp", "queue", "QM1/BAD", "queue_depth", 99, threshold=10)))
        by_name = {o["object_name"]: o for o in pipeline.watched_objects()}
        self.assertEqual(by_name["QM1/OK"]["status"], "ok")
        self.assertEqual(by_name["QM1/BAD"]["status"], "anomaly")
        # A healthy object still needs a tile, so it must survive the early return.
        self.assertEqual(by_name["QM1/OK"]["object_type"], "queue")
        self.assertIsNotNone(by_name["QM1/BAD"]["timestamp"])

    def test_incident_event_carries_every_field_the_row_renders(self):
        """A live row must not need a page reload to show its severity."""
        pipeline = EnterprisePipeline(use_ai=False)
        obs = Observation("mq_mcp", "queue", "QM1/DEEP", "queue_depth", 99,
                          labels={"service": "orders"}, threshold=10)
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "incidents.db"
            with patch("store.DB_PATH", db):
                asyncio.run(pipeline.ingest(obs))
            conn = sqlite3.connect(db)
            try:
                stored = conn.execute(
                    "SELECT severity, created_at FROM incidents ORDER BY id DESC LIMIT 1").fetchone()
            finally:
                conn.close()  # Windows cannot remove the temp dir while it is open.
        event = [e for e in bus.recent(20) if e.type == "incident_created"][-1]
        for field in ("incident_id", "title", "severity", "object_name", "object_type",
                      "trigger_source", "created_at"):
            self.assertIn(field, event.payload)
        self.assertEqual(event.payload["severity"], stored[0])
        # Event and stored row must report the same instant, not two time.time() calls.
        self.assertEqual(event.payload["created_at"], stored[1])


_WATCHLIST = Watchlist(poll_interval_seconds=60, sources=[],
                       max_consecutive_failures_before_backoff=3,
                       backoff_multiplier=2, max_backoff_seconds=600)


class _FakeCollector:
    """Scripted collector: each cycle either raises or returns observations."""

    def __init__(self, name: str, failures: int = 0, yields=None):
        self.name = name
        self.failures = failures
        self.yields = yields
        self.attempt_times: list[float] = []
        self._calls = 0

    async def collect(self):
        self.attempt_times.append(collector_loop.time.time())
        self._calls += 1
        if self._calls <= self.failures:
            raise RuntimeError(f"{self.name} unreachable")
        if self.yields is not None:
            return list(self.yields)
        return [Observation(self.name, "queue", f"{self.name}/Q1", "queue_depth", 1, threshold=10)]


class _Pipeline:
    async def ingest(self, observation):
        return {"outcome": "healthy", "ai_calls": 0}


class CollectorResilienceTests(unittest.TestCase):
    def setUp(self):
        collector_loop._health.clear()
        # These tests fail collection on purpose; the resulting tracebacks are
        # correct behaviour but would bury the actual test results.
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def _run_ticks(self, collectors, ticks: int):
        """Drive collect_forever on a fake clock that advances one poll
        interval per tick, so backoff shows up as skipped attempts."""
        clock = {"now": 0.0}

        async def tick_sleep(seconds):
            clock["now"] += seconds
            if clock["now"] >= ticks * _WATCHLIST.poll_interval_seconds:
                raise _StopLoop

        with patch.object(collector_loop.time, "time", lambda: clock["now"]), \
                patch("asyncio.sleep", tick_sleep):
            with self.assertRaises(_StopLoop):
                asyncio.run(collect_forever(_Pipeline(), watchlist=_WATCHLIST,
                                            collectors=collectors))

    def test_failures_back_off_after_threshold_then_reset_on_recovery(self):
        # threshold 3, multiplier 2: attempts at 0,60,120 fail; backoff skips
        # tick 180; attempt 240 fails; skips 300..420; attempt 480 succeeds;
        # normal cadence resumes.
        collector = _FakeCollector("mq", failures=4)
        self._run_ticks([collector], ticks=10)
        self.assertEqual(collector.attempt_times, [0, 60, 120, 240, 480, 540])
        self.assertEqual(collector_health()["status"], "ok")

    def test_loop_survives_failure_and_reports_health(self):
        self._run_ticks([_FakeCollector("mq", failures=99)], ticks=2)
        health = collector_health()
        self.assertEqual(health["status"], "failing")
        self.assertEqual(health["consecutive_failures"], 2)
        self.assertIn("mq unreachable", health["last_error"])

    def test_backoff_is_capped(self):
        collector = _FakeCollector("mq", failures=99)
        self._run_ticks([collector], ticks=40)
        gaps = [b - a for a, b in zip(collector.attempt_times, collector.attempt_times[1:])]
        self.assertEqual(max(gaps), _WATCHLIST.max_backoff_seconds)

    def test_one_failing_collector_does_not_stop_the_others(self):
        healthy = _FakeCollector("mq")
        broken = _FakeCollector("prom", failures=99)
        self._run_ticks([healthy, broken], ticks=6)
        # The healthy collector keeps its full cadence while the broken one
        # backs off independently.
        self.assertEqual(healthy.attempt_times, [0, 60, 120, 180, 240, 300])
        health = collector_health()
        self.assertEqual(health["collectors"]["mq"]["status"], "ok")
        self.assertEqual(health["collectors"]["prom"]["status"], "failing")
        # Aggregate reflects the worst collector so the dashboard stays honest.
        self.assertEqual(health["status"], "failing")

    def test_successful_cycle_with_no_readings_is_not_reported_as_ok(self):
        """A reachable endpoint in front of dead backends still collects
        nothing; calling that "ok" is the silent failure this guards."""
        collector = _FakeCollector("mq", yields=[])
        self._run_ticks([collector], ticks=2)
        self.assertEqual(collector_health()["status"], "degraded")
        # The endpoint is healthy, so the poll cadence must stay normal.
        self.assertEqual(collector.attempt_times, [0, 60])

    def test_readings_restore_ok_from_degraded(self):
        collector = _FakeCollector("mq", yields=[])
        self._run_ticks([collector], ticks=1)
        self.assertEqual(collector_health()["status"], "degraded")
        collector.yields = None
        self._run_ticks([collector], ticks=1)
        self.assertEqual(collector_health()["status"], "ok")


class CustomRulesTests(unittest.TestCase):
    def test_custom_rules_load_after_builtins(self):
        import detection
        # Patch onto a temp path: writing the real config/rules.custom.yaml
        # would destroy rules the user created through the Rules tab.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        custom = Path(temp.name) / "rules.custom.yaml"
        patcher = patch.object(detection, "CUSTOM_RULES_PATH", custom)
        patcher.start()
        self.addCleanup(patcher.stop)
        custom.write_text("rules:\n  - id: test-latency\n    when: {metric: proxy_latency_ms}\n"
                          "    condition: {type: greater_than, value: '${threshold}', default: 1000}\n"
                          "    severity: P3\n    message: 'Latency {value_int}ms exceeds {threshold_int}ms'\n",
                          encoding="utf-8")
        engine = RuleEngine()
        # Custom rule fires for its own metric…
        finding = engine.evaluate(Observation("apigee", "proxy", "orders-api", "proxy_latency_ms", 2500))
        self.assertIsNotNone(finding)
        self.assertEqual(finding.reason, "Latency 2500ms exceeds 1000ms")
        # …and built-ins keep priority: the last rule id must be the custom one.
        self.assertEqual(engine.rules[-1]["id"], "test-latency")


class ServiceNowOutboxTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def _seed_incident(self) -> int:
        import store as store_module
        return store_module.save_incident(
            object_name="QM1/Q", object_type="queue", severity="P2", title="depth",
            markdown_report="r", watcher_json={}, diagnosis_json={}, report_json={},
            total_cost_usd=0.0, trigger_source="mq_mcp")

    def test_dry_run_marks_each_incident_exactly_once(self):
        import store as store_module
        from integrations import servicenow
        with tempfile.TemporaryDirectory() as temp:
            with patch("store.DB_PATH", Path(temp) / "incidents.db"):
                incident_id = self._seed_incident()
                self.assertEqual([i["id"] for i in store_module.incidents_missing_ref("servicenow")],
                                 [incident_id])

                async def one_sweep(seconds):
                    raise _StopLoop  # first sleep ends the worker after one pass

                with patch.dict("os.environ", {"SERVICENOW_MODE": "dry_run"}), \
                        patch("asyncio.sleep", one_sweep):
                    with self.assertRaises(_StopLoop):
                        asyncio.run(servicenow.deliver_forever())
                # Marked once; the outbox is now empty — one incident, one delivery.
                self.assertEqual(store_module.incidents_missing_ref("servicenow"), [])
                refs = store_module.list_incidents()[0]["external_refs"]
                self.assertEqual(refs["servicenow"]["mode"], "dry_run")

    def test_mode_off_disables_worker(self):
        from integrations import servicenow
        with patch.dict("os.environ", {"SERVICENOW_MODE": "off"}):
            asyncio.run(servicenow.deliver_forever())  # returns immediately


class RecurrenceTests(unittest.TestCase):
    """The decided answer to: an incident is closed and the alert fires again."""

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        patcher = patch("store.DB_PATH", Path(self._temp.name) / "incidents.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.pipeline = EnterprisePipeline(use_ai=False)

    def _breach(self):
        return asyncio.run(self.pipeline.ingest(
            Observation("mq_mcp", "queue", "QM1/Q", "queue_depth", 99,
                        labels={"service": "orders", "environment": "prod"}, threshold=10)))

    def test_active_incident_suppresses_indefinitely(self):
        """One standing condition is one incident — not one per dedup window."""
        first = self._breach()
        self.assertEqual(first["outcome"], "incident_created")
        # Far beyond the 900s dedup window: an OPEN incident still suppresses.
        with patch("time.time", return_value=time.time() + 10_000):
            again = self._breach()
        self.assertEqual(again["outcome"], "deduplicated")
        self.assertEqual(again["incident_id"], first["incident_id"])
        self.assertEqual(len(store.list_incidents()), 1)

    def test_recurrence_soon_after_close_reopens_the_same_incident(self):
        first = self._breach()
        store.set_incident_status(first["incident_id"], "closed", actor="ops")
        self.pipeline.correlator.forget("prod:orders:queue_depth")
        again = self._breach()
        self.assertEqual(again["outcome"], "incident_reopened")
        self.assertEqual(again["incident_id"], first["incident_id"])
        row = store.get_incident(first["incident_id"])
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["reopen_count"], 1)
        self.assertIsNone(row["closed_at"])  # reopen clears the closure stamp
        self.assertEqual(len(store.list_incidents()), 1)

    def test_recurrence_after_the_window_opens_a_linked_new_incident(self):
        first = self._breach()
        store.set_incident_status(first["incident_id"], "closed", actor="ops")
        self.pipeline.correlator.forget("prod:orders:queue_depth")
        beyond = time.time() + self.pipeline.reopen_window_seconds + 60
        with patch("time.time", return_value=beyond):
            again = self._breach()
        self.assertEqual(again["outcome"], "incident_created")
        self.assertNotEqual(again["incident_id"], first["incident_id"])
        self.assertEqual(store.get_incident(again["incident_id"])["previous_incident_id"],
                         first["incident_id"])

    def test_closing_clears_dedup_so_a_recurrence_is_never_swallowed(self):
        """The bug this feature exists to fix: without forget(), a recurrence
        inside the dedup window after a close vanished silently."""
        finding = self.pipeline.rules.evaluate(
            Observation("mq_mcp", "queue", "QM1/Q", "queue_depth", 99,
                        labels={"service": "orders", "environment": "prod"}, threshold=10))
        self.pipeline.correlator.is_new(finding)          # stamps _last_seen
        self.assertFalse(self.pipeline.correlator.is_new(finding))
        self.pipeline.correlator.forget(finding.fingerprint)
        self.assertTrue(self.pipeline.correlator.is_new(finding))

    def test_status_transitions_are_audited(self):
        first = self._breach()
        incident_id = first["incident_id"]
        store.set_incident_status(incident_id, "acknowledged", actor="alice")
        store.set_incident_status(incident_id, "resolved", actor="bob", note="restarted consumer")
        store.set_incident_status(incident_id, "closed", actor="bob")
        actions = [e["action"] for e in store.audit_entries("incident", str(incident_id))]
        self.assertEqual(actions, ["incident_closed", "incident_resolved", "incident_acknowledged"])
        resolved = [e for e in store.audit_entries("incident", str(incident_id))
                    if e["action"] == "incident_resolved"][0]
        self.assertEqual(resolved["actor"], "bob")
        self.assertEqual(resolved["detail"]["note"], "restarted consumer")

    def test_mttr_is_measured_over_resolved_incidents(self):
        first = self._breach()
        store.set_incident_status(first["incident_id"], "resolved", actor="ops")
        metrics = store.incident_metrics()
        self.assertEqual(metrics["counts"]["resolved"], 1)
        self.assertIsNotNone(metrics["mttr_seconds"])

    def test_closing_without_an_explicit_resolve_still_counts_toward_mttr(self):
        """Closing straight from open is the common path; MTTR must not
        silently ignore it."""
        first = self._breach()
        store.set_incident_status(first["incident_id"], "closed", actor="ops")
        row = store.get_incident(first["incident_id"])
        self.assertIsNotNone(row["resolved_at"])
        self.assertIsNotNone(store.incident_metrics()["mttr_seconds"])

    def test_false_positive_is_excluded_from_mttr(self):
        first = self._breach()
        store.set_incident_status(first["incident_id"], "false_positive", actor="ops")
        self.assertIsNone(store.get_incident(first["incident_id"])["resolved_at"])
        self.assertIsNone(store.incident_metrics()["mttr_seconds"])


class SeverityModeTests(unittest.TestCase):
    """Severity is either fixed by the operator, defaulted, or delegated to AI."""

    def _finding(self, rule: dict):
        import detection
        engine = RuleEngine.__new__(RuleEngine)
        engine.settings = {"default_depth_threshold": 1000}
        engine.trend_points = 3
        engine.rules = [rule]
        engine._history = {}
        engine._stateful_metrics = set()
        return engine.evaluate(Observation("s", "queue", "Q", "queue_depth", 50, threshold=10))

    def test_fixed_severity_is_marked_as_operator_owned(self):
        finding = self._finding({"id": "r", "when": {"metric": "queue_depth"},
                                 "condition": {"type": "greater_than", "value": "${threshold}"},
                                 "severity": "P1", "message": "over"})
        self.assertEqual((finding.severity, finding.severity_source), ("P1", "rule"))

    def test_missing_severity_falls_back_to_p3(self):
        finding = self._finding({"id": "r", "when": {"metric": "queue_depth"},
                                 "condition": {"type": "greater_than", "value": "${threshold}"},
                                 "message": "over"})
        self.assertEqual((finding.severity, finding.severity_source), ("P3", "default"))

    def test_ai_mode_carries_a_provisional_severity(self):
        """AI mode must still be rankable before AI runs — severity is the input
        to the cost gate that decides whether AI runs at all."""
        finding = self._finding({"id": "r", "when": {"metric": "queue_depth"},
                                 "condition": {"type": "greater_than", "value": "${threshold}"},
                                 "severity": "ai", "message": "over"})
        self.assertEqual((finding.severity, finding.severity_source), ("P3", "ai"))
        custom = self._finding({"id": "r", "when": {"metric": "queue_depth"},
                                "condition": {"type": "greater_than", "value": "${threshold}"},
                                "severity": "ai", "ai_provisional": "P2", "message": "over"})
        self.assertEqual((custom.severity, custom.severity_source), ("P2", "ai"))

    def test_ai_cannot_override_an_operator_set_severity(self):
        """The regression this guards: _save used to take the AI's severity
        unconditionally, silently downgrading a deliberate P1."""
        pipeline = EnterprisePipeline(use_ai=False)
        obs = Observation("s", "queue", "Q", "queue_depth", 50, threshold=10)
        fixed = Finding("fp", "P1", "reason", obs, ["reason"], severity_source="rule")
        provisional = Finding("fp2", "P3", "reason", obs, ["reason"], severity_source="ai")
        with tempfile.TemporaryDirectory() as temp:
            with patch("store.DB_PATH", Path(temp) / "incidents.db"):
                pipeline._save(fixed, {}, {"severity": "P4"}, 0.0, {}, "ai")
                pipeline._save(provisional, {}, {"severity": "P1"}, 0.0, {}, "ai")
                stored = {row["title"]: row["severity"] for row in store.list_incidents()}
        # Operator's P1 survives an AI verdict of P4; the provisional P3 defers.
        self.assertEqual(set(stored.values()), {"P1"})


class RuleOverrideTests(unittest.TestCase):
    def test_override_replaces_builtin_in_place(self):
        from detection import merge_rules
        builtin = [{"id": "a", "severity": "P1"}, {"id": "b", "severity": "P2"}]
        merged = merge_rules(builtin, [{"id": "b", "severity": "P4"}, {"id": "c", "severity": "P3"}])
        # Order is behaviour under first-match-wins: the override must not move.
        self.assertEqual([r["id"] for r in merged], ["a", "b", "c"])
        self.assertEqual(merged[1]["severity"], "P4")

    def test_disabled_builtin_is_removed(self):
        from detection import merge_rules
        merged = merge_rules([{"id": "a"}, {"id": "b"}], [{"id": "a", "disabled": True}])
        self.assertEqual([r["id"] for r in merged], ["b"])


class ServiceNowKbDeliveryTests(unittest.TestCase):
    """Approved KB articles mirror into ServiceNow Knowledge. The local
    approved KB stays the source of truth for incident matching."""

    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        root = Path(self._temp.name)
        self.kb_dir = root / "approved"
        self.kb_dir.mkdir()
        self.refs_path = root / "refs.json"
        from integrations import servicenow
        self.servicenow = servicenow
        patcher = patch.multiple(servicenow, KB_DIR=self.kb_dir, KB_REFS_PATH=self.refs_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _article(self, slug: str, body: str) -> Path:
        path = self.kb_dir / f"{slug}.md"
        path.write_text(body, encoding="utf-8")
        return path

    def _sweep(self):
        self.servicenow._sweep_kb("dry_run")
        return self.servicenow.load_kb_refs()

    def test_each_article_is_delivered_exactly_once(self):
        self._article("queue-depth-runbook", "# Queue depth runbook\n\nSteps.\n")
        refs = self._sweep()
        self.assertIn("queue-depth-runbook", refs)
        first_hash = refs["queue-depth-runbook"]["hash"]
        # A second sweep with nothing changed must not re-deliver.
        with patch.object(self.servicenow, "_save_kb_refs") as save:
            self.servicenow._sweep_kb("dry_run")
            save.assert_not_called()
        self.assertEqual(self._sweep()["queue-depth-runbook"]["hash"], first_hash)

    def test_edited_article_triggers_one_update(self):
        path = self._article("channel-runbook", "# Channel runbook\n\nOriginal.\n")
        original_hash = self._sweep()["channel-runbook"]["hash"]
        path.write_text("# Channel runbook\n\nRevised after review.\n", encoding="utf-8")
        updated_hash = self._sweep()["channel-runbook"]["hash"]
        self.assertNotEqual(original_hash, updated_hash)
        # And settles: no further delivery once the hash is recorded.
        with patch.object(self.servicenow, "_save_kb_refs") as save:
            self.servicenow._sweep_kb("dry_run")
            save.assert_not_called()

    def test_deleted_article_is_retired_and_ref_dropped(self):
        path = self._article("temporary-note", "# Temporary note\n\nBody.\n")
        self.assertIn("temporary-note", self._sweep())
        path.unlink()
        self.assertNotIn("temporary-note", self._sweep())

    def test_payload_uses_heading_as_title(self):
        payload = self.servicenow.kb_payload("some-slug", "# Real Title\n\nBody.\n")
        self.assertEqual(payload["short_description"], "Real Title")
        self.assertEqual(payload["workflow_state"], "published")
        # No heading -> fall back to the slug rather than sending an empty title.
        self.assertEqual(self.servicenow.kb_payload("some-slug", "no heading")["short_description"],
                         "some-slug")


if __name__ == "__main__": unittest.main()
