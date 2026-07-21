"""Runs every configured collector on one poll loop with per-collector health.

One failing collector must never stop the others: each source tracks its own
ok/degraded/failing state and backs off independently using the watchlist's
configured limits, while healthy sources keep their normal cadence.
"""
from __future__ import annotations
import asyncio
import logging
import time
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from agents.common import Watchlist, load_watchlist
from collectors import Collector, build_collectors
from enterprise_pipeline import EnterprisePipeline
from events import Event, bus

logger = logging.getLogger("collector")

# Health per collector name. Underscore-prefixed keys are loop internals and
# are stripped from the published payload.
_health: dict[str, dict] = {}


def _fresh(interval: float) -> dict:
    return {"status": "starting", "consecutive_failures": 0, "last_error": None,
            "last_success_ts": None, "next_attempt_in": interval,
            "_delay": interval, "_skip_until": 0.0}


_STATUS_RANK = {"failing": 0, "degraded": 1, "starting": 2, "ok": 3}


def collector_health() -> dict:
    """Aggregate health: worst collector's fields at the top level (the shape
    the dashboard has always consumed), plus per-collector detail."""
    per = {name: {k: v for k, v in h.items() if not k.startswith("_")}
           for name, h in _health.items()}
    if not per:
        return {"status": "starting", "consecutive_failures": 0, "last_error": None,
                "last_success_ts": None, "next_attempt_in": None, "collectors": {}}
    worst = min(per.values(), key=lambda h: _STATUS_RANK.get(h["status"], 2))
    return {**worst, "collectors": per}


async def _run_collector(collector: Collector, pipeline: EnterprisePipeline,
                         watchlist: Watchlist, now: float) -> None:
    health = _health.setdefault(collector.name, _fresh(watchlist.poll_interval_seconds))
    # Backoff skip. The half-interval tolerance stops tick quantisation from
    # stretching a 2-tick backoff into 3 (collection time shifts _skip_until
    # slightly past the tick boundary).
    if health["_skip_until"] - now > watchlist.poll_interval_seconds / 2:
        return
    try:
        observations = await collector.collect()
        for observation in observations:
            await pipeline.ingest(observation)
    except Exception as exc:
        health["consecutive_failures"] += 1
        health["last_error"] = f"{type(exc).__name__}: {exc}"
        health["status"] = "failing"
        logger.exception("collector %s failed (%d consecutive)",
                         collector.name, health["consecutive_failures"])
        if health["consecutive_failures"] >= watchlist.max_consecutive_failures_before_backoff:
            health["_delay"] = min(health["_delay"] * watchlist.backoff_multiplier,
                                   watchlist.max_backoff_seconds)
            health["_skip_until"] = now + health["_delay"]
    else:
        if health["consecutive_failures"]:
            logger.info("collector %s recovered after %d failure(s)",
                        collector.name, health["consecutive_failures"])
        if observations:
            health.update(status="ok", consecutive_failures=0, last_error=None,
                          last_success_ts=time.time())
        else:
            # The endpoint answered but there was nothing behind it (e.g. MCP up,
            # queue managers down). Calling that "ok" would be the silent failure
            # this loop exists to prevent. No backoff: the endpoint is healthy.
            if health["status"] != "degraded":
                logger.warning("collector %s returned no readings — check upstream connectivity",
                               collector.name)
            health.update(status="degraded", consecutive_failures=0,
                          last_error="collection returned no readings", last_success_ts=time.time())
        health["_delay"] = watchlist.poll_interval_seconds
        health["_skip_until"] = 0.0
    health["next_attempt_in"] = health["_delay"]


async def collect_once(pipeline: EnterprisePipeline | None = None,
                       watchlist: Watchlist | None = None) -> dict:
    """One cycle over every collector; returns observations ingested per source."""
    watchlist = watchlist or load_watchlist()
    pipeline = pipeline or EnterprisePipeline(use_ai=False)
    results = {}
    for collector in build_collectors(watchlist):
        observations = await collector.collect()
        for observation in observations:
            await pipeline.ingest(observation)
        results[collector.name] = len(observations)
    return results


async def collect_forever(pipeline: EnterprisePipeline,
                          watchlist: Watchlist | None = None,
                          collectors: list[Collector] | None = None) -> None:
    """Reuse one pipeline so trend and deduplication state survives cycles.

    CancelledError derives from BaseException, so shutdown cancellation passes
    through the per-collector `except Exception` untouched.
    """
    watchlist = watchlist or load_watchlist()
    collectors = collectors if collectors is not None else build_collectors(watchlist)
    while True:
        now = time.time()
        for collector in collectors:
            await _run_collector(collector, pipeline, watchlist, now)
        bus.publish(Event("collector_status", collector_health()))
        await asyncio.sleep(watchlist.poll_interval_seconds)


if __name__ == "__main__":
    print(asyncio.run(collect_once()))
