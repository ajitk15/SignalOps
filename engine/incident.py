"""Workflow A — incident remediation, as a LangGraph state machine.

    enrich → triage →(in scope)→ diagnose → plan → work note → gate
                  ↓                                              ↓
                 end                                    (approved) → hand off → close
                                                        (rejected) → end

Two properties are structural rather than enforced by convention.

**Nothing here executes a remediation.** The `hand_off` node renders the plan
for a person to run and records that it is waiting on them. The plan calls this
propose-only, and the way to make that true is for the graph to contain no node
that could act — not a node with an `if dry_run` branch that someone later
turns off.

**The gate is on the path, not beside it.** Every route from planning to
hand-off goes through `gate`, so the only way to skip the human is to clear the
confidence threshold — a decision the gate records either way.
"""
from __future__ import annotations

import time

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from engine.approvals import canonical_hash
from engine.llm import as_text
from engine.state import RunContext, RunState

TEMPLATE = "incident_remediation"

# Which agents this graph cannot run without. Checked before a run starts so a
# disabled required agent is a clear refusal rather than a mid-run failure.
REQUIRED_AGENTS = ("diagnostician", "remediation_planner")


def _ticket_sections(state: RunState) -> dict[str, str]:
    ticket = state.get("ticket", {})
    return {"incident": as_text(ticket)}


def build(ctx: RunContext):
    """Compile the graph with this run's context bound in."""

    def enrich(state: RunState) -> dict:
        """Gather what is known around the incident.

        Deterministic on purpose: the agents reason about evidence, they do not
        go and fetch it. Context supplied on the trigger payload wins over a
        lookup — that is what makes a run reproducible from a recorded ticket.
        Anything neither supplied nor fetched is recorded as unavailable rather
        than quietly absent, so a thin diagnosis can be explained by a thin
        context instead of looking like a weak agent.
        """
        with ctx.step("enrich") as record:
            ticket = state.get("ticket", {})
            gathered, missing = {}, []
            for name in ("recent_changes", "past_incidents", "kb_articles"):
                if ticket.get(name):
                    gathered[name] = ticket[name]
            if ctx.source is not None:
                fetched, unavailable = ctx.source.gather(ticket)
                for name, value in fetched.items():
                    gathered.setdefault(name, value)
                missing = [name for name in unavailable if name not in gathered]
            else:
                missing = [name for name in ("recent_changes", "past_incidents",
                                             "kb_articles") if name not in gathered]
            record["output"] = {"gathered": sorted(gathered), "unavailable": missing,
                                "source": "servicenow" if (
                                    ctx.source is not None and ctx.source.available)
                                    else "trigger payload only"}
            return {"context": {**gathered, "unavailable_sources": missing}}

    def triage(state: RunState) -> dict:
        if not ctx.enabled("triage"):
            # Optional agent, switched off: the catalogue says every ticket is
            # then treated as in scope, and the run says so out loud.
            return {"outputs": {"triage": {"in_scope": True, "skipped": True}},
                    "decisions": [{"node": "triage", "decision": "skipped",
                                   "why": "the triage agent is disabled"}]}
        with ctx.step("triage", agent_id="triage") as record:
            result = ctx.call("triage", _ticket_sections(state), record)
            return {"outputs": {"triage": result.output}}

    def route_after_triage(state: RunState) -> str:
        triage_output = state.get("outputs", {}).get("triage", {})
        return "diagnose" if triage_output.get("in_scope", True) else "out_of_scope"

    def out_of_scope(state: RunState) -> dict:
        triage_output = state.get("outputs", {}).get("triage", {})
        return {"outcome": "skipped",
                "outcome_reason": triage_output.get("reason", "triage ruled it out of scope"),
                "decisions": [{"node": "triage", "decision": "out_of_scope",
                               "why": triage_output.get("reason", "")}]}

    def diagnose(state: RunState) -> dict:
        with ctx.step("diagnose", agent_id="diagnostician") as record:
            sections = _ticket_sections(state)
            sections["context"] = as_text(state.get("context", {}))
            result = ctx.call("diagnostician", sections, record)
            return {"outputs": {"diagnostician": result.output}}

    def plan(state: RunState) -> dict:
        with ctx.step("plan", agent_id="remediation_planner") as record:
            sections = _ticket_sections(state)
            sections["diagnosis"] = as_text(state["outputs"]["diagnostician"])
            result = ctx.call("remediation_planner", sections, record)
            return {"outputs": {"remediation_planner": result.output}}

    def work_note(state: RunState) -> dict:
        """Write the diagnosis and plan back to the ticket.

        Dry-run is the default and records the exact payload it would have
        sent, so what the workflow *would* do is reviewable before it is
        allowed to do it.
        """
        with ctx.step("work_note") as record:
            ticket = state.get("ticket", {})
            diagnosis = state["outputs"]["diagnostician"]
            proposal = state["outputs"]["remediation_planner"]
            body = _render_work_note(diagnosis, proposal, simulated=ctx.simulated)
            result = ctx.sink.work_note(sys_id=ticket.get("sys_id"),
                                        number=ticket.get("number"), note=body)
            record["output"] = {"sent": result.sent, "dry_run": ctx.dry_run,
                                "characters": len(body)}
            return {"external_writes": [{**result.as_record(), "at": time.time(),
                                         "body": body}]}

    def gate(state: RunState) -> dict:
        """The human gate.

        Straight through only when the planner is both confident enough and not
        marked as needing approval. Everything else pauses the run — including,
        deliberately, the case where no threshold is configured.
        """
        ctx.check("gate")
        proposal = state["outputs"]["remediation_planner"]
        confidence = float(proposal.get("confidence", 0.0))
        threshold = ctx.threshold("remediation_planner")
        needs_human = ctx.agents["remediation_planner"].requires_approval
        payload = {"incident": state.get("ticket", {}).get("number"),
                   "diagnosis": state["outputs"]["diagnostician"],
                   "plan": proposal,
                   "simulated": ctx.simulated}

        if not needs_human and confidence >= threshold:
            return {"approval": {"mode": "auto", "confidence": confidence,
                                 "threshold": threshold,
                                 "payload_hash": canonical_hash(payload)},
                    "decisions": [{"node": "gate", "decision": "auto_approved",
                                   "why": f"confidence {confidence:.2f} cleared the "
                                          f"{threshold:.2f} threshold"}]}

        # Pauses here. The run is checkpointed mid-node; resuming re-enters this
        # function and interrupt() returns the decision instead of raising.
        why = ("this agent is configured to always ask" if needs_human else
               f"confidence {confidence:.2f} is below the {threshold:.2f} threshold")
        decision = interrupt({"summary": _approval_summary(state, confidence, threshold, why),
                              "payload": payload,
                              "payload_hash": canonical_hash(payload),
                              "confidence": confidence, "threshold": threshold,
                              "why": why})
        return {"approval": {"mode": "human", **(decision or {})},
                "decisions": [{"node": "gate",
                               "decision": (decision or {}).get("status", "unknown"),
                               "why": (decision or {}).get("note") or why}]}

    def route_after_gate(state: RunState) -> str:
        approval = state.get("approval") or {}
        if approval.get("mode") == "auto":
            return "hand_off"
        return "hand_off" if approval.get("status") == "approved" else "rejected"

    def rejected(state: RunState) -> dict:
        approval = state.get("approval") or {}
        return {"outcome": "rejected",
                "outcome_reason": approval.get("note") or "a reviewer rejected the plan"}

    def hand_off(state: RunState) -> dict:
        """Propose-only execution.

        There is no automation adapter behind this node, and that is the whole
        design. The plan goes to a person, and the run pauses a second time
        until they say what happened. Two pauses, not one, because approving a
        plan and having run it are different facts and only the second one can
        justify resolving a ticket.

        When a real executor arrives it plugs in here — behind an approval that
        already exists and reporting into a field the workflow already reads.
        """
        ctx.check("hand_off")
        proposal = state["outputs"]["remediation_planner"]
        ticket = state.get("ticket", {})
        if not ctx.config.get("require_outcome_report", True):
            # Configured off: the run ends here and the ticket stays open for a
            # person to close in ServiceNow. Nothing is resolved on the strength
            # of an approval alone either way — the difference is only whether
            # the platform waits to be told.
            with ctx.step("hand_off") as record:
                record["output"] = {"steps": len(proposal.get("steps", [])),
                                    "awaiting": "an operator; no report was requested"}
            return {"outcome": "proposed",
                    "outcome_reason": "plan approved and handed to an operator; this "
                                      "workflow does not wait for an outcome report",
                    "decisions": [{"node": "hand_off", "decision": "proposed",
                                   "why": "outcome reporting is off for this workflow"}]}
        payload = {"plan": proposal, "incident": ticket.get("number")}
        report = interrupt({
            "kind": "execution_outcome",
            "summary": f"{ticket.get('number', 'incident')} — plan approved. Run the "
                       f"{len(proposal.get('steps', []))} steps, then report what happened.",
            "payload": payload,
            "payload_hash": canonical_hash(payload),
            "why": "SignalOps proposes; a person executes and reports back",
        })
        succeeded = (report or {}).get("status") == "approved"
        with ctx.step("hand_off") as record:
            record["output"] = {"steps": len(proposal.get("steps", [])),
                                "reported": "succeeded" if succeeded else "did not resolve it",
                                "by": (report or {}).get("by")}
        return {"approval": {**(state.get("approval") or {}),
                             "execution_outcome": "succeeded" if succeeded else "failed",
                             "execution_note": (report or {}).get("note")},
                "decisions": [{"node": "hand_off",
                               "decision": "executed" if succeeded else "did_not_resolve",
                               "why": (report or {}).get("note")
                                      or "reported by the operator who ran it"}]}

    def close(state: RunState) -> dict:
        """Resolve the ticket — only when a human said the plan worked.

        The workflow proposes and a person executes, so nothing here can know
        the incident is fixed. Closing on approval alone would resolve tickets
        whose remediation nobody ran. The run therefore ends `awaiting_outcome`
        unless an operator has reported one.
        """
        with ctx.step("close") as record:
            ticket = state.get("ticket", {})
            outcome = (state.get("approval") or {}).get("execution_outcome")
            if outcome != "succeeded":
                record["output"] = {"resolved": False,
                                    "waiting_on": "an operator to run the plan and report back"}
                return {"outcome": "awaiting_outcome",
                        "outcome_reason": "plan approved and handed over; the ticket stays "
                                          "open until someone reports the result"}
            result = ctx.sink.resolve(
                sys_id=ticket.get("sys_id"), number=ticket.get("number"),
                close_notes=_render_close_note(state, simulated=ctx.simulated))
            record["output"] = {"resolved": True, "sent": result.sent, "dry_run": ctx.dry_run}
            return {"outcome": "resolved",
                    "outcome_reason": "an operator ran the plan and reported success",
                    "external_writes": [{**result.as_record(), "at": time.time()}]}

    graph = StateGraph(RunState)
    for name, fn in (("enrich", enrich), ("triage", triage), ("out_of_scope", out_of_scope),
                     ("diagnose", diagnose), ("plan", plan), ("work_note", work_note),
                     ("gate", gate), ("rejected", rejected), ("hand_off", hand_off),
                     ("close", close)):
        graph.add_node(name, fn)

    graph.add_edge(START, "enrich")
    graph.add_edge("enrich", "triage")
    graph.add_conditional_edges("triage", route_after_triage,
                                {"diagnose": "diagnose", "out_of_scope": "out_of_scope"})
    graph.add_edge("out_of_scope", END)
    graph.add_edge("diagnose", "plan")
    graph.add_edge("plan", "work_note")
    graph.add_edge("work_note", "gate")
    graph.add_conditional_edges("gate", route_after_gate,
                                {"hand_off": "hand_off", "rejected": "rejected"})
    graph.add_edge("rejected", END)
    graph.add_edge("hand_off", "close")
    graph.add_edge("close", END)
    return graph


def _render_work_note(diagnosis: dict, proposal: dict, *, simulated: bool) -> str:
    lines = []
    if simulated:
        lines += ["[SIMULATED — no model was called. Do not act on this note.]", ""]
    lines += [
        "SignalOps analysis",
        "",
        f"Likely cause: {diagnosis.get('root_cause', 'not established')}",
        f"Confidence: {diagnosis.get('confidence', 0):.0%}",
        "",
        "Evidence:",
    ]
    lines += [f"  - {item}" for item in diagnosis.get("evidence", [])] or ["  - none recorded"]
    lines += ["", f"Proposed remediation (risk: {proposal.get('risk', 'unknown')}, "
                  f"downtime: {'yes' if proposal.get('requires_downtime') else 'no'}):"]
    for index, step in enumerate(proposal.get("steps", []), start=1):
        lines += [f"  {index}. {step.get('action', '')}",
                  f"     verify: {step.get('verify', '')}",
                  f"     rollback: {step.get('rollback', '')}"]
    lines += ["", "No change has been made. This is a proposal for an operator to execute."]
    return "\n".join(lines)


def _render_close_note(state: RunState, *, simulated: bool) -> str:
    diagnosis = state["outputs"]["diagnostician"]
    approval = state.get("approval") or {}
    lines = []
    if simulated:
        lines += ["[SIMULATED — no model was called.]", ""]
    lines += [
        "Resolved via SignalOps.",
        "",
        f"Cause: {diagnosis.get('root_cause', 'not established')}",
        f"Plan approved by: {approval.get('by') or 'auto (confidence above threshold)'}",
        f"Executed and confirmed by: {approval.get('by') or 'operator'}",
    ]
    if approval.get("execution_note"):
        lines += ["", f"Operator note: {approval['execution_note']}"]
    return "\n".join(lines)


def _approval_summary(state: RunState, confidence: float, threshold: float, why: str) -> str:
    ticket = state.get("ticket", {})
    proposal = state["outputs"]["remediation_planner"]
    return (f"{ticket.get('number', 'incident')} — {len(proposal.get('steps', []))} step "
            f"plan, risk {proposal.get('risk', 'unknown')}. Paused because {why}.")
