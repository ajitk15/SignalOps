import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from detection import Correlator, Observation, RuleEngine
from enterprise_pipeline import EnterprisePipeline
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

if __name__ == "__main__": unittest.main()
