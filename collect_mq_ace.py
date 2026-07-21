"""One deterministic collection cycle: MCP -> rules -> incidents."""
from __future__ import annotations
import asyncio
import logging
import time
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from agents.common import load_watchlist
from enterprise_pipeline import EnterprisePipeline
from events import Event, bus
from integrations.mq_ace_mcp import MqAceMcpCollector

logger = logging.getLogger("collector")

# Collection health, so a dead poll loop is visible on the dashboard instead of
# leaving stale tiles under a green "live" indicator. Read via collector_health().
_health: dict = {"status": "starting", "consecutive_failures": 0,
                 "last_error": None, "last_success_ts": None, "next_attempt_in": None}


def collector_health() -> dict:
    return dict(_health)


async def collect_once(use_ai: bool = False, pipeline: EnterprisePipeline | None = None):
    watchlist = load_watchlist(); collector = MqAceMcpCollector(); pipeline = pipeline or EnterprisePipeline(use_ai=use_ai)
    observations = await collector.collect_queues(watchlist.queues)
    observations += await collector.collect_channels(watchlist.channels)
    return [await pipeline.ingest(observation) for observation in observations]

async def collect_forever(pipeline: EnterprisePipeline):
    """Reuse one pipeline so trend and deduplication state survives cycles.

    A failing MCP endpoint must never end the loop — that used to leave the
    dashboard showing stale readings forever with nothing logged. Failures back
    off using the watchlist's configured limits and are published to the bus.
    CancelledError derives from BaseException, so shutdown still cancels cleanly.
    """
    watchlist = load_watchlist()
    delay = watchlist.poll_interval_seconds
    while True:
        try:
            results = await collect_once(pipeline=pipeline)
        except Exception as exc:
            _health["consecutive_failures"] += 1
            _health["last_error"] = f"{type(exc).__name__}: {exc}"
            _health["status"] = "failing"
            logger.exception("collection cycle failed (%d consecutive)", _health["consecutive_failures"])
            if _health["consecutive_failures"] >= watchlist.max_consecutive_failures_before_backoff:
                delay = min(delay * watchlist.backoff_multiplier, watchlist.max_backoff_seconds)
        else:
            # Only a run of failures is something to recover from; the first
            # cycle after startup would otherwise log "recovered after 0".
            if _health["consecutive_failures"]:
                logger.info("collection recovered after %d failure(s)", _health["consecutive_failures"])
            if results:
                _health.update(status="ok", consecutive_failures=0, last_error=None, last_success_ts=time.time())
            else:
                # The MCP call succeeded but yielded nothing to report — e.g. the
                # server is up while the queue managers behind it are not. Calling
                # this "ok" would recreate the silent failure this loop exists to
                # prevent, so it is surfaced as its own state. No backoff: the
                # endpoint is healthy, so the normal cadence is still correct.
                if _health["status"] != "degraded":
                    logger.warning("collection returned no readings — check queue manager connectivity")
                _health.update(status="degraded", consecutive_failures=0,
                               last_error="collection returned no readings", last_success_ts=time.time())
            delay = watchlist.poll_interval_seconds
        _health["next_attempt_in"] = delay
        bus.publish(Event("collector_status", collector_health()))
        await asyncio.sleep(delay)

if __name__ == "__main__":
    print(asyncio.run(collect_once(use_ai=False)))
