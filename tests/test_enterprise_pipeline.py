import asyncio
import logging
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import collect_mq_ace
from collect_mq_ace import collect_forever, collector_health
from detection import Correlator, Observation, RuleEngine
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


class CollectorResilienceTests(unittest.TestCase):
    def setUp(self):
        collect_mq_ace._health.update(status="starting", consecutive_failures=0,
                                      last_error=None, last_success_ts=None, next_attempt_in=None)
        # These tests fail collection on purpose; the resulting tracebacks are
        # correct behaviour but would bury the actual test results.
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def _run_cycles(self, failures: int, cycles: int) -> list[float]:
        """Drive collect_forever, failing the first N cycles, capturing each sleep."""
        delays: list[float] = []
        calls = {"n": 0}

        async def flaky(pipeline=None):
            calls["n"] += 1
            if calls["n"] <= failures:
                raise RuntimeError("mcp unreachable")
            return []

        async def capture_sleep(seconds):
            delays.append(seconds)
            if len(delays) >= cycles:
                raise _StopLoop

        with patch("collect_mq_ace.collect_once", flaky), patch("asyncio.sleep", capture_sleep):
            with self.assertRaises(_StopLoop):
                asyncio.run(collect_forever(None))
        return delays

    def test_failures_back_off_after_threshold_then_reset_on_recovery(self):
        # watchlist.yaml: poll 60s, threshold 3 failures, multiplier 2, cap 600s.
        delays = self._run_cycles(failures=4, cycles=6)
        self.assertEqual(delays, [60, 60, 120, 240, 60, 60])

    def test_loop_survives_failure_and_reports_health(self):
        self._run_cycles(failures=2, cycles=2)
        health = collector_health()
        self.assertEqual(health["status"], "failing")
        self.assertEqual(health["consecutive_failures"], 2)
        self.assertIn("mcp unreachable", health["last_error"])

    def test_backoff_is_capped(self):
        delays = self._run_cycles(failures=20, cycles=12)
        self.assertEqual(max(delays), 600)


if __name__ == "__main__": unittest.main()
