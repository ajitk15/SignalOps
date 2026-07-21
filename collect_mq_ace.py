"""One deterministic collection cycle: MCP -> rules -> incidents."""
from __future__ import annotations
import asyncio
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from agents.common import load_watchlist
from enterprise_pipeline import EnterprisePipeline
from integrations.mq_ace_mcp import MqAceMcpCollector

async def collect_once(use_ai: bool = False, pipeline: EnterprisePipeline | None = None):
    watchlist = load_watchlist(); collector = MqAceMcpCollector(); pipeline = pipeline or EnterprisePipeline(use_ai=use_ai)
    observations = await collector.collect_queues(watchlist.queues)
    observations += await collector.collect_channels(watchlist.channels)
    return [await pipeline.ingest(observation) for observation in observations]

async def collect_forever(pipeline: EnterprisePipeline):
    """Reuse one pipeline so trend and deduplication state survives cycles."""
    watchlist = load_watchlist()
    while True:
        await collect_once(pipeline=pipeline)
        await asyncio.sleep(watchlist.poll_interval_seconds)

if __name__ == "__main__":
    print(asyncio.run(collect_once(use_ai=False)))
