"""The engine: starts runs, pauses them at human gates, and picks them back up.

The durability story is the point of this file. LangGraph checkpoints the state
after every node into its own SQLite file, so a run is not a Python object that
dies with the process — it is a row that a fresh process can resume. Three
consequences the platform depends on:

- A run paused at an approval survives a restart. Approvals are answered in
  human time, and "the server was redeployed" cannot be a reason a half-finished
  remediation vanishes.
- A run interrupted mid-flight is resumed on the next start, from the last node
  that completed rather than from the beginning. `reconcile()` does this.
- Cost, steps and decisions are persisted as they happen, so a run that dies
  ungracefully still leaves a legible trail.

Graphs execute on a bounded thread pool. LangGraph's sync API is a natural fit
for nodes that call a blocking SDK, and a bounded pool means one heavy run
cannot starve the event loop serving the UI.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from sqlalchemy.exc import IntegrityError

from agents.catalogue import for_workflow
from agents.catalogue import get as catalogue_get
from agents.guard import GuardrailViolation, resolve
from db import audit, session_scope
from engine import incident
from engine.approvals import StaleApproval, canonical_hash
from engine.budget import DEFAULT_RUN_BUDGET_USD, BudgetExceeded
from engine.llm import ModelCallFailed, build_client
from engine.state import Halted, RunContext
from integrations.servicenow import ContextSource, TicketSink
from events import Event, bus
from models import AgentConfig, Approval, ApprovalStatus, Run, RunStatus, Workflow, Workspace

logger = logging.getLogger("engine.runtime")

CHECKPOINT_PATH = Path(__file__).resolve().parent.parent / "data" / "checkpoints.db"

# One template so far. Phase 4 registers ticket_to_pr alongside it; the registry
# exists now so adding it is a line here rather than a change to the engine.
TEMPLATES = {incident.TEMPLATE: incident}

MAX_CONCURRENT_RUNS = int(os.getenv("SIGNALOPS_MAX_CONCURRENT_RUNS", "4"))


class EngineError(Exception):
    pass


class DuplicateRun(EngineError):
    """This ticket already has a run on this workflow.

    Not a failure. The unique index on (workflow_id, trigger_ref) is the
    idempotency guarantee — a poller that sees the same ticket on two
    consecutive sweeps is the normal case, and it must not start a second run
    or spend a second time. The caller gets the existing run instead.
    """

    def __init__(self, message: str, run_id: str | None = None) -> None:
        super().__init__(message)
        self.run_id = run_id


class Engine:
    def __init__(self, *, client=None, checkpoint_path: Path | None = None,
                 sink_factory=None, source_factory=None) -> None:
        path = Path(checkpoint_path or CHECKPOINT_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False because runs execute on the pool; SqliteSaver
        # holds its own lock around every access.
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self.checkpointer = SqliteSaver(self._connection)
        self.checkpointer.setup()
        self._client = client
        # Injectable so tests can exercise the write path without a ServiceNow
        # instance, and so phase 4 can register a different sink.
        self._sink_factory = sink_factory or (lambda *, dry_run: TicketSink(dry_run=dry_run))
        self._source_factory = source_factory or ContextSource
        self._pool = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_RUNS,
                                        thread_name_prefix="signalops-run")
        self._lock = threading.Lock()

    @property
    def client(self):
        # Built lazily so importing the engine never requires an API key.
        if self._client is None:
            self._client = build_client()
        return self._client

    # --- starting ------------------------------------------------------------

    def start(self, *, workflow_id: str, ticket: dict, actor: str,
              actor_verified: bool = False, dry_run: bool | None = None,
              trigger_ref: str | None = None) -> str:
        """Create a run and hand it to the pool. Returns the run id."""
        with session_scope() as session:
            workflow = session.get(Workflow, workflow_id)
            if workflow is None:
                raise EngineError("unknown workflow")
            if workflow.template not in TEMPLATES:
                raise EngineError(f"no graph registered for template {workflow.template!r}")
            workspace = session.get(Workspace, workflow.workspace_id)
            if workspace is not None and workspace.killswitch:
                raise Halted("workspace kill switch is on; no run was started", "killswitch")

            module = TEMPLATES[workflow.template]
            self._assert_required_agents(session, workflow.workspace_id, module)

            effective_dry_run = workflow.config.get("dry_run", True) if dry_run is None else dry_run
            reference = trigger_ref or ticket.get("number")
            existing = (session.query(Run)
                        .filter(Run.workflow_id == workflow.id,
                                Run.trigger_ref == reference).first()
                        if reference else None)
            if existing is not None:
                raise DuplicateRun(
                    f"{reference} already has a run on this workflow "
                    f"({existing.status.value}). Re-running the same ticket would "
                    f"duplicate the work and the spend.", existing.id)
            run = Run(workspace_id=workflow.workspace_id, workflow_id=workflow.id,
                      trigger_ref=reference,
                      status=RunStatus.pending, dry_run=bool(effective_dry_run))
            session.add(run)
            try:
                session.flush()
            except IntegrityError as clash:
                # Lost a race with a concurrent poller; the index is the real
                # guarantee and the pre-check above is only the friendly path.
                raise DuplicateRun(
                    f"{reference} already has a run on this workflow.") from clash
            run_id = run.id
            workspace_id = workflow.workspace_id
            template = workflow.template
            budget = workflow.config.get("run_budget_usd", DEFAULT_RUN_BUDGET_USD)
            audit(session, actor=actor, action="run_started", entity_type="run",
                  entity_id=run_id, workspace_id=workspace_id,
                  actor_verified=actor_verified,
                  detail={"workflow": workflow.name, "template": template,
                          "dry_run": bool(effective_dry_run), "trigger": run.trigger_ref})

        self._publish("run_started", {"run_id": run_id, "workflow_id": workflow_id,
                                      "template": template, "dry_run": bool(effective_dry_run),
                                      "trigger_ref": trigger_ref or ticket.get("number")})
        context = self._context(run_id=run_id, workspace_id=workspace_id,
                                workflow_id=workflow_id, dry_run=bool(effective_dry_run),
                                actor=actor, actor_verified=actor_verified,
                                template=template, budget=budget)
        initial = {"run_id": run_id, "workflow_id": workflow_id,
                   "dry_run": bool(effective_dry_run), "ticket": ticket,
                   "context": {}, "outputs": {}, "decisions": [], "external_writes": []}
        self._pool.submit(self._drive, context, template, initial)
        return run_id

    def _assert_required_agents(self, session, workspace_id: str, module) -> None:
        """A disabled required agent stops the run before it starts.

        Failing at submission is the honest failure: the alternative spends
        money on three nodes and then discovers the fourth cannot run.
        """
        configs = _configs(session, workspace_id)
        for agent_id in module.REQUIRED_AGENTS:
            spec = catalogue_get(agent_id)
            if not resolve(spec, configs.get(agent_id)).enabled:
                raise EngineError(
                    f"agent {agent_id!r} is disabled and this workflow cannot run "
                    f"without it: {spec.disabled_effect}")

    def _context(self, *, run_id, workspace_id, workflow_id, dry_run, actor,
                 actor_verified, template, budget) -> RunContext:
        with session_scope() as session:
            configs = _configs(session, workspace_id)
        agents, specs = {}, {}
        for spec in for_workflow(template):
            try:
                agents[spec.id] = resolve(spec, configs.get(spec.id))
            except GuardrailViolation:
                # A stored customisation that no longer passes the guard must not
                # silently run under the shipped defaults — drop the agent so the
                # run fails on a required one and skips an optional one.
                logger.exception("agent %s failed to resolve; excluded from this run", spec.id)
                continue
            specs[spec.id] = spec
        return RunContext(run_id=run_id, workspace_id=workspace_id, workflow_id=workflow_id,
                          dry_run=dry_run, actor=actor, actor_verified=actor_verified,
                          client=self.client, agents=agents, specs=specs,
                          session_scope=session_scope, publish=self._publish,
                          run_budget_usd=budget,
                          sink=self._sink_factory(dry_run=dry_run),
                          source=self._source_factory())

    # --- driving -------------------------------------------------------------

    def _drive(self, context: RunContext, template: str, payload) -> None:
        """Run the graph until it finishes, pauses or stops. Never raises."""
        graph = TEMPLATES[template].build(context).compile(checkpointer=self.checkpointer)
        config = {"configurable": {"thread_id": context.run_id}}
        self._set_status(context.run_id, RunStatus.running)
        try:
            state = graph.invoke(payload, config)
        except Halted as halt:
            self._stop(context, RunStatus.cancelled, str(halt), kind=halt.kind)
            return
        except (BudgetExceeded,) as over:
            self._stop(context, RunStatus.cancelled, str(over), kind="budget")
            return
        except (ModelCallFailed, Exception) as error:      # noqa: BLE001 — see below
            # Broad on purpose: a node raising anything at all must leave the run
            # recorded as failed rather than leaving a "running" row behind and a
            # traceback in a log nobody reads.
            logger.exception("run %s failed", context.run_id)
            self._stop(context, RunStatus.failed, f"{type(error).__name__}: {error}")
            return

        pending = state.get("__interrupt__")
        if pending:
            self._pause(context, pending)
            return
        self._finish(context, state)

    def _pause(self, context: RunContext, pending) -> None:
        """Record the approval the graph is waiting on."""
        request = pending[0].value if hasattr(pending[0], "value") else pending[0]
        node = "hand_off" if request.get("kind") == "execution_outcome" else "gate"
        with session_scope() as session:
            approval = Approval(run_id=context.run_id, node=node,
                                summary=request.get("summary", "approval required"),
                                payload=request.get("payload", {}),
                                payload_hash=request.get("payload_hash", ""),
                                status=ApprovalStatus.pending)
            session.add(approval)
            session.flush()
            approval_id = approval.id
            run = session.get(Run, context.run_id)
            run.status = RunStatus.awaiting_approval
            run.cost_usd = context.spent_usd
            passed = _mark_dry_run_passed(session, run)
            if passed:
                audit(session, actor="engine", action="dry_run_passed",
                      entity_type="workflow", entity_id=run.workflow_id,
                      workspace_id=context.workspace_id, detail={"run_id": run.id})
            audit(session, actor="engine", action="approval_requested", entity_type="run",
                  entity_id=context.run_id, workspace_id=context.workspace_id,
                  detail={"approval_id": approval_id, "node": node,
                          "why": request.get("why")})
        self._publish("approval_requested",
                      {"run_id": context.run_id, "approval_id": approval_id, "node": node,
                       "summary": request.get("summary"), "why": request.get("why"),
                       "confidence": request.get("confidence"),
                       "threshold": request.get("threshold")})

    def _finish(self, context: RunContext, state) -> None:
        outcome = state.get("outcome") or "completed"
        with session_scope() as session:
            run = session.get(Run, context.run_id)
            run.status = RunStatus.succeeded
            run.finished_at = time.time()
            run.cost_usd = context.spent_usd
            if _mark_dry_run_passed(session, run):
                audit(session, actor="engine", action="dry_run_passed",
                      entity_type="workflow", entity_id=run.workflow_id,
                      workspace_id=context.workspace_id, detail={"run_id": run.id})
            audit(session, actor="engine", action="run_finished", entity_type="run",
                  entity_id=context.run_id, workspace_id=context.workspace_id,
                  detail={"outcome": outcome, "cost_usd": round(context.spent_usd, 6),
                          "simulated": context.simulated,
                          "external_writes": len(state.get("external_writes", []))})
        self._publish("run_finished",
                      {"run_id": context.run_id, "status": "succeeded", "outcome": outcome,
                       "reason": state.get("outcome_reason"),
                       "cost_usd": round(context.spent_usd, 6),
                       "simulated": context.simulated})

    def _stop(self, context: RunContext, status: RunStatus, reason: str,
              kind: str = "error") -> None:
        with session_scope() as session:
            run = session.get(Run, context.run_id)
            run.status = status
            run.finished_at = time.time()
            run.error = reason
            run.cost_usd = context.spent_usd
            audit(session, actor="engine", action=f"run_{status.value}", entity_type="run",
                  entity_id=context.run_id, workspace_id=context.workspace_id,
                  detail={"reason": reason, "kind": kind})
        self._publish("run_finished", {"run_id": context.run_id, "status": status.value,
                                       "reason": reason, "kind": kind,
                                       "cost_usd": round(context.spent_usd, 6)})

    def _set_status(self, run_id: str, status: RunStatus) -> None:
        with session_scope() as session:
            run = session.get(Run, run_id)
            if run is not None:
                run.status = status

    # --- approvals -----------------------------------------------------------

    def decide(self, *, approval_id: str, approved: bool, actor: str, actor_id: str | None,
               actor_verified: bool = False, note: str | None = None) -> str:
        """Answer a pending approval and resume the run.

        The payload is re-hashed before the decision is accepted. If the plan
        changed since it was shown, the approval does not carry over — the whole
        point of pinning the hash is that "approved" refers to something
        specific.
        """
        from langgraph.types import Command

        with session_scope() as session:
            approval = session.get(Approval, approval_id)
            if approval is None:
                raise EngineError("unknown approval")
            if approval.status is not ApprovalStatus.pending:
                raise EngineError(f"approval is already {approval.status.value}")
            if canonical_hash(approval.payload) != approval.payload_hash:
                approval.status = ApprovalStatus.expired
                raise StaleApproval(
                    "the plan changed after this approval was requested; it must be "
                    "reviewed again")
            run = session.get(Run, approval.run_id)
            if run is None:
                raise EngineError("approval has no run")
            if run.status is not RunStatus.awaiting_approval:
                raise EngineError(f"run is {run.status.value}, not awaiting approval")
            approval.status = ApprovalStatus.approved if approved else ApprovalStatus.rejected
            approval.decided_at = time.time()
            approval.decided_by = actor_id
            approval.note = note
            run_id, workspace_id, workflow_id = run.id, run.workspace_id, run.workflow_id
            dry_run = run.dry_run
            spent = run.cost_usd
            workflow = session.get(Workflow, workflow_id)
            template, budget = workflow.template, workflow.config.get(
                "run_budget_usd", DEFAULT_RUN_BUDGET_USD)
            audit(session, actor=actor,
                  action="approval_granted" if approved else "approval_rejected",
                  entity_type="run", entity_id=run_id, workspace_id=workspace_id,
                  actor_verified=actor_verified,
                  detail={"approval_id": approval_id, "note": note,
                          # The hash goes in the audit line so the trail records
                          # what was approved, not merely that something was.
                          "payload_hash": approval.payload_hash})

        self._publish("approval_decided", {"run_id": run_id, "approval_id": approval_id,
                                           "approved": approved, "actor": actor})
        context = self._context(run_id=run_id, workspace_id=workspace_id,
                                workflow_id=workflow_id, dry_run=dry_run, actor=actor,
                                actor_verified=actor_verified, template=template,
                                budget=budget)
        context.spent_usd = spent or 0.0
        resume = {"status": "approved" if approved else "rejected", "by": actor, "note": note}
        self._pool.submit(self._drive, context, template, Command(resume=resume))
        return run_id

    # --- lifecycle -----------------------------------------------------------

    def cancel(self, *, run_id: str, actor: str, actor_verified: bool = False) -> None:
        with session_scope() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise EngineError("unknown run")
            if run.status in (RunStatus.succeeded, RunStatus.failed, RunStatus.cancelled):
                raise EngineError(f"run is already {run.status.value}")
            run.status = RunStatus.cancelled
            run.finished_at = time.time()
            run.error = f"cancelled by {actor}"
            audit(session, actor=actor, action="run_cancelled", entity_type="run",
                  entity_id=run_id, workspace_id=run.workspace_id,
                  actor_verified=actor_verified)
        self._publish("run_finished", {"run_id": run_id, "status": "cancelled",
                                       "reason": f"cancelled by {actor}"})

    def reconcile(self) -> int:
        """Resume runs the previous process left in flight.

        A run marked `running` with no process behind it is the signature of a
        crash or a redeploy. The checkpoint holds everything needed to continue,
        so the honest recovery is to continue rather than to fail it and make a
        human restart work that was half done.

        Runs awaiting approval need nothing here — they resume when someone
        answers, which is exactly what the checkpointer makes possible.
        """
        resumed = 0
        with session_scope() as session:
            stale = session.query(Run).filter(Run.status == RunStatus.running).all()
            targets = [(r.id, r.workspace_id, r.workflow_id, r.dry_run, r.cost_usd)
                       for r in stale]
        for run_id, workspace_id, workflow_id, dry_run, spent in targets:
            with session_scope() as session:
                workflow = session.get(Workflow, workflow_id)
                if workflow is None or workflow.template not in TEMPLATES:
                    continue
                template = workflow.template
                budget = workflow.config.get("run_budget_usd", DEFAULT_RUN_BUDGET_USD)
            context = self._context(run_id=run_id, workspace_id=workspace_id,
                                    workflow_id=workflow_id, dry_run=dry_run,
                                    actor="engine (resumed)", actor_verified=False,
                                    template=template, budget=budget)
            context.spent_usd = spent or 0.0
            logger.info("resuming run %s from its last checkpoint", run_id)
            self._publish("run_resumed", {"run_id": run_id})
            # None as input means "continue from the checkpoint".
            self._pool.submit(self._drive, context, template, None)
            resumed += 1
        return resumed

    def shutdown(self, *, drain: bool = False) -> None:
        """Stop accepting work.

        By default this does not wait: a run killed mid-node is checkpointed up
        to its last completed node and `reconcile()` resumes it on the next
        start, so a slow shutdown buys nothing a restart does not already fix.
        `drain=True` waits instead, which tests need so a worker is not still
        holding the database when the temporary directory is removed.
        """
        self._pool.shutdown(wait=drain, cancel_futures=not drain)
        self._connection.close()

    # --- events --------------------------------------------------------------

    def _publish(self, event_type: str, payload: dict) -> None:
        bus.publish_threadsafe(Event(event_type, payload))


def _mark_dry_run_passed(session, run) -> bool:
    """Stamp the workflow once a dry run has proven the wiring.

    "Proven" means the run reached a completed work note: the connection
    resolved, every required agent produced a valid answer, and the external
    write was composed. That is the whole path onboarding needs to trust, and
    stopping there matters — a dry run must not require someone to also approve
    a plan and report an outcome for a ticket nobody intends to act on.
    """
    from models import RunStep, Workflow
    if not run.dry_run:
        return False
    workflow = session.get(Workflow, run.workflow_id)
    if workflow is None or workflow.dry_run_passed_at:
        return False
    reached = (session.query(RunStep)
               .filter(RunStep.run_id == run.id, RunStep.node == "work_note",
                       RunStep.status == "succeeded").first())
    if reached is None:
        return False
    workflow.dry_run_passed_at = time.time()
    return True


def _configs(session, workspace_id: str) -> dict:
    return {c.agent_id: c for c in session.query(AgentConfig)
            .filter(AgentConfig.workspace_id == workspace_id).all()}


_engine: Engine | None = None


def engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = Engine()
    return _engine
