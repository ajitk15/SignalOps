"""Triggers: turn tickets matching a saved filter into runs.

One loop per enabled workflow, and the isolation is the point. A connection
that starts returning 500s must take down its own workflow's polling and
nothing else — the v1 lesson was a single shared loop where one bad source
silenced every other one, and the failure looked like "monitoring stopped"
rather than "one source broke".

The poller does not deduplicate. It does not need to: starting a run for a
ticket that already has one raises DuplicateRun against the unique index, which
means re-seeing a ticket on the next sweep is free and correct rather than
something the poller has to remember. That also survives a restart, which an
in-memory seen-set would not.
"""
from __future__ import annotations

import asyncio
import logging

from db import session_scope
from engine.runtime import DuplicateRun, EngineError, engine
from engine.state import Halted
from events import Event, bus
from integrations import servicenow
from models import Workflow

logger = logging.getLogger("engine.poller")

DEFAULT_INTERVAL_SECONDS = 120
MIN_INTERVAL_SECONDS = 30
# Per sweep. A filter that suddenly matches a thousand tickets is a misconfigured
# filter, and starting a thousand runs is the expensive way to find that out.
MAX_TICKETS_PER_SWEEP = 5


def _connection_of(workflow_config: dict):
    """The connection a workflow polls through, as a detached settings carrier."""
    connection_id = (workflow_config or {}).get("connection_id")
    if not connection_id:
        return None
    from models import Connection
    with session_scope() as session:
        connection = session.get(Connection, connection_id)
        if connection is None:
            return None
        return type("C", (), {"name": connection.name,
                              "config": dict(connection.config or {}),
                              "secrets": dict(connection.secrets or {})})()


def _trigger_query(workflow_config: dict) -> str:
    """What the poller asks ServiceNow for.

    Built from the connection's monitored queue — the assignment group an
    operations team already thinks in — plus any extra filter on the workflow.
    Never from ticket text.
    """
    connection = _connection_of(workflow_config)
    extra = (workflow_config or {}).get("filter_query", "")
    if connection is None:
        return extra or "active=true"
    return servicenow.queue_query(connection, extra)


def _poll_client(workflow_id: str):
    """The client for this workflow's connection, falling back to the
    environment so existing setups keep polling."""
    from models import Workflow
    with session_scope() as session:
        workflow = session.get(Workflow, workflow_id)
        config = dict(workflow.config or {}) if workflow else {}
    connection = _connection_of(config)
    if connection is None:
        return servicenow.reader()
    try:
        return servicenow.client_from(connection)
    except servicenow.ServiceNowError:
        logger.exception("could not build a client for workflow %s", workflow_id)
        return None



class Poller:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}

    async def sync(self) -> None:
        """Match running loops to the enabled workflows. Idempotent."""
        with session_scope() as session:
            wanted = {w.id: (w.name, dict(w.config))
                      for w in session.query(Workflow)
                      .filter(Workflow.enabled.is_(True)).all()
                      if w.config.get("poll_enabled")}
        for workflow_id in list(self._tasks):
            if workflow_id not in wanted:
                self.stop(workflow_id)
        for workflow_id, (name, config) in wanted.items():
            if workflow_id not in self._tasks:
                logger.info("polling started for %s", name)
                self._tasks[workflow_id] = asyncio.create_task(
                    self._loop(workflow_id, name, config))

    def stop(self, workflow_id: str) -> None:
        task = self._tasks.pop(workflow_id, None)
        if task is not None:
            task.cancel()
            logger.info("polling stopped for workflow %s", workflow_id)

    def stop_all(self) -> None:
        for workflow_id in list(self._tasks):
            self.stop(workflow_id)

    def status(self) -> dict[str, bool]:
        return {wid: not task.done() for wid, task in self._tasks.items()}

    async def _loop(self, workflow_id: str, name: str, config: dict) -> None:
        interval = max(MIN_INTERVAL_SECONDS,
                       int(config.get("poll_interval_seconds", DEFAULT_INTERVAL_SECONDS)))
        while True:
            try:
                await self._sweep(workflow_id, name, _trigger_query(config))
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never let one bad sweep end the loop; the next one may succeed
                # and a dead poller is worse than a noisy one.
                logger.exception("poll sweep failed for %s", name)
                bus.publish(Event("poll_failed", {"workflow_id": workflow_id, "name": name}))
            await asyncio.sleep(interval)

    async def _sweep(self, workflow_id: str, name: str, query: str) -> None:
        client = _poll_client(workflow_id)
        if client is None:
            logger.warning("polling %s: no ServiceNow read credentials; skipping sweep", name)
            return
        # Blocking HTTP off the event loop.
        records = await asyncio.to_thread(client.search_incidents, query, MAX_TICKETS_PER_SWEEP)
        started, skipped = 0, 0
        for record in records:
            ticket = servicenow.normalise(record)
            try:
                await asyncio.to_thread(
                    engine().start, workflow_id=workflow_id, ticket=ticket,
                    actor="poller", actor_verified=False, trigger_ref=ticket["number"])
                started += 1
            except DuplicateRun:
                skipped += 1          # already has a run; the normal case
            except Halted as halt:
                logger.info("polling %s: %s", name, halt)
                return
            except EngineError:
                logger.exception("polling %s: could not start a run for %s",
                                 name, ticket.get("number"))
        bus.publish(Event("poll_completed",
                          {"workflow_id": workflow_id, "name": name,
                           "matched": len(records), "started": started,
                           "already_running": skipped}))


poller = Poller()
