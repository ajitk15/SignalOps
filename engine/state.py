"""The state a run carries, and the context a node executes inside.

The split matters for durability. **State** is a plain dict that LangGraph
checkpoints after every node — it must stay JSON-serialisable, because it is
what a run is rebuilt from when the process restarts. **Context** is the live
machinery: the database session factory, the model client, the resolved agents.
It is bound by closure when the graph is built and deliberately never enters the
state, since a database handle cannot be checkpointed and should not survive a
restart anyway.

`RunContext.step()` is where the non-negotiables live. Every node runs inside
it, so the kill switch and the budget are checked at every node boundary rather
than only at the start — a run already in flight has to be stoppable, otherwise
the kill switch is a "do not start more" switch wearing a bigger name.
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from agents.catalogue import AgentSpec
from agents.guard import ResolvedAgent
from agents.schemas import schema_for
from engine import budget as budget_module
from engine.llm import AgentResult, LLMClient, ModelCallFailed
from models import RunStep, Workspace

logger = logging.getLogger("engine")


class Halted(Exception):
    """A run stopped by a control rather than by an error: the kill switch or a
    budget ceiling. Distinguished from a failure because it is not a bug."""

    def __init__(self, reason: str, kind: str) -> None:
        super().__init__(reason)
        self.kind = kind          # killswitch | budget


def _merge(existing: dict, incoming: dict) -> dict:
    return {**(existing or {}), **(incoming or {})}


def _append(existing: list, incoming: list) -> list:
    return [*(existing or []), *(incoming or [])]


class RunState(TypedDict, total=False):
    """Checkpointed after every node. Keep it JSON-serialisable."""
    run_id: str
    workflow_id: str
    dry_run: bool
    # The trigger payload — untrusted, never treated as instructions.
    ticket: dict[str, Any]
    context: Annotated[dict[str, Any], _merge]
    outputs: Annotated[dict[str, Any], _merge]
    decisions: Annotated[list[dict], _append]
    external_writes: Annotated[list[dict], _append]
    approval: dict[str, Any] | None
    outcome: str | None
    outcome_reason: str | None


@dataclass
class RunContext:
    """Bound to one run. Not part of the checkpointed state."""
    run_id: str
    workspace_id: str
    workflow_id: str
    dry_run: bool
    actor: str
    actor_verified: bool
    client: LLMClient
    agents: dict[str, ResolvedAgent]
    specs: dict[str, AgentSpec]
    session_scope: Any
    publish: Any                       # callable(event_type, payload)
    run_budget_usd: float | None = budget_module.DEFAULT_RUN_BUDGET_USD
    spent_usd: float = 0.0
    simulated: bool = field(default=False)

    # --- controls ------------------------------------------------------------

    def _assert_permitted(self, node: str) -> None:
        with self.session_scope() as session:
            workspace = session.get(Workspace, self.workspace_id)
            if workspace is not None and workspace.killswitch:
                raise Halted(
                    f"workspace kill switch is on; stopped before {node!r}", "killswitch")
            workspace_budget = workspace.budget_usd if workspace else None
            spent_workspace = _workspace_spend(session, self.workspace_id) \
                if workspace_budget is not None else 0.0
        budget_module.check(spent_run=self.spent_usd, run_budget=self.run_budget_usd,
                            spent_workspace=spent_workspace,
                            workspace_budget=workspace_budget)

    # --- step recording ------------------------------------------------------

    @contextmanager
    def step(self, node: str, agent_id: str | None = None):
        """Run one node: checked, timed, persisted and broadcast.

        Yields a mutable record the node fills in. A node that raises still
        leaves a persisted step with its error, so a failed run is legible
        afterwards rather than being a gap in the timeline.
        """
        self._assert_permitted(node)
        record: dict[str, Any] = {"output": None, "input_tokens": 0,
                                  "output_tokens": 0, "cost_usd": 0.0}
        with self.session_scope() as session:
            step = RunStep(run_id=self.run_id, node=node, agent_id=agent_id,
                           status="running", started_at=time.time())
            session.add(step)
            session.flush()
            step_id = step.id
        self.publish("run_step_started",
                     {"run_id": self.run_id, "step_id": step_id, "node": node,
                      "agent_id": agent_id})
        started = time.time()
        try:
            yield record
        except Exception as error:
            self._finish(step_id, node, agent_id, record, started,
                         status="failed", error=f"{type(error).__name__}: {error}")
            raise
        self._finish(step_id, node, agent_id, record, started, status="succeeded")

    def _finish(self, step_id, node, agent_id, record, started, *, status, error=None):
        self.spent_usd += record["cost_usd"]
        with self.session_scope() as session:
            step = session.get(RunStep, step_id)
            step.status = status
            step.finished_at = time.time()
            step.output = record["output"]
            step.input_tokens = record["input_tokens"]
            step.output_tokens = record["output_tokens"]
            step.cost_usd = record["cost_usd"]
            step.error = error
        self.publish("run_step_finished",
                     {"run_id": self.run_id, "step_id": step_id, "node": node,
                      "agent_id": agent_id, "status": status, "error": error,
                      "duration_s": round(time.time() - started, 3),
                      "cost_usd": record["cost_usd"], "output": record["output"]})

    # --- agent invocation ----------------------------------------------------

    def call(self, agent_id: str, sections: dict[str, str], record: dict) -> AgentResult:
        """Invoke an agent and fold its usage into the step record."""
        from engine.llm import render_task
        agent = self.agents[agent_id]
        result = self.client.complete(agent, render_task(sections), schema_for(agent_id))
        record["output"] = result.output
        record["input_tokens"] = result.input_tokens
        record["output_tokens"] = result.output_tokens
        record["cost_usd"] = result.cost_usd
        if result.simulated:
            self.simulated = True
        return result

    def enabled(self, agent_id: str) -> bool:
        agent = self.agents.get(agent_id)
        return bool(agent and agent.enabled)

    def threshold(self, agent_id: str) -> float:
        agent = self.agents[agent_id]
        # No configured threshold means nothing to clear, so the gate is the
        # requires_approval flag alone rather than an accidental free pass.
        return agent.confidence_threshold if agent.confidence_threshold is not None else 0.0


def _workspace_spend(session, workspace_id: str) -> float:
    from sqlalchemy import func

    from models import Run
    total = (session.query(func.sum(Run.cost_usd))
             .filter(Run.workspace_id == workspace_id).scalar())
    return float(total or 0.0)


__all__ = ["RunState", "RunContext", "Halted", "ModelCallFailed"]
