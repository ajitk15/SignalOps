"""Workflow B — ticket to pull request.

    fetch → locate → analyse → implement → qa →(tests pass)→ gate →(approved)→ pr → write back
                                            ↓                    ↓
                                    blocked (end)         rejected (end)

Three decisions shape this graph.

**The patch is the durable artifact, not the checkout.** A human gate can pause
a run for days, and a temp clone will not survive that — nor a restart, nor the
machine. So `implement` captures the diff into checkpointed state, and `pr`
re-clones and applies it. That also sharpens the approval: what gets hashed and
approved is the actual patch text, so approving a diff cannot authorise a
different one.

**The test suite outranks the agent.** `qa` runs the repository's own command
from validated configuration and routes on its exit code. The QA reviewer's
opinion is recorded next to it and changes nothing. An agent that could talk
its way past a red build would make the whole gate ornamental.

**Analysis comes before implementation.** The impact assessment is produced and
recorded before a line is written, so a run that is going to be large and risky
is visible while it is still only a proposal.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from engine.approvals import canonical_hash
from engine.llm import as_text
from engine.state import RunContext, RunState

logger = logging.getLogger("engine.ticket_to_pr")

TEMPLATE = "ticket_to_pr"

REQUIRED_AGENTS = ("code_locator", "implementer")

TEST_TIMEOUT_SECONDS = 900


def build(ctx: RunContext):
    """Compile the graph with this run's context bound in."""

    def fetch(state: RunState) -> dict:
        """Clone the repository into a throwaway checkout for this run."""
        with ctx.step("fetch") as record:
            workspace = ctx.workspace()
            tree = workspace.tree()
            record["output"] = {"branch": workspace.branch, "files": len(tree),
                                "base": workspace.base_branch}
            return {"context": {"tree": tree, "branch": workspace.branch}}

    def locate(state: RunState) -> dict:
        with ctx.step("locate", agent_id="code_locator") as record:
            sections = {"ticket": as_text(state.get("ticket", {})),
                        "repository_tree": "\n".join(state["context"].get("tree", []))}
            result = ctx.call("code_locator", sections, record)
            return {"outputs": {"code_locator": result.output}}

    def route_after_locate(state: RunState) -> str:
        files = state["outputs"].get("code_locator", {}).get("files") or []
        return "analyse" if files else "nothing_to_do"

    def nothing_to_do(state: RunState) -> dict:
        return {"outcome": "skipped",
                "outcome_reason": "no files in this repository looked relevant to the ticket"}

    def analyse(state: RunState) -> dict:
        """Complexity and risk, before any code exists.

        Optional agent: without it the change still proceeds, but a human
        reviews the diff with less context and no early warning — which the
        catalogue says out loud.
        """
        if not ctx.enabled("impact_analyst"):
            return {"outputs": {"impact_analyst": {"skipped": True}},
                    "decisions": [{"node": "analyse", "decision": "skipped",
                                   "why": "the impact analyst is disabled"}]}
        with ctx.step("analyse", agent_id="impact_analyst") as record:
            files = state["outputs"]["code_locator"].get("files", [])
            sections = {"ticket": as_text(state.get("ticket", {})),
                        "relevant_files": _read_files(ctx, files)}
            result = ctx.call("impact_analyst", sections, record)
            return {"outputs": {"impact_analyst": result.output}}

    def implement(state: RunState) -> dict:
        """The only node that changes anything, and it changes a clone.

        The diff is captured into state because the checkout will not outlive
        the approval that follows.
        """
        import asyncio

        from engine import coder

        with ctx.step("implement", agent_id="implementer") as record:
            workspace = ctx.workspace()
            agent = ctx.agents["implementer"]
            analysis = state["outputs"].get("impact_analyst", {})
            files = state["outputs"]["code_locator"].get("files", [])
            runner = (coder.implement_simulated if ctx.client.simulated
                      else coder.implement)
            result = asyncio.run(runner(
                agent=agent, workspace=workspace, ticket=state.get("ticket", {}),
                analysis=analysis, files=files,
                budget_usd=ctx.run_budget_usd))
            if result.simulated:
                ctx.simulated = True

            # After the fact as well as during: the tool-call veto refuses a
            # protected write as it happens, this catches anything that arrived
            # by another route, and it runs before a commit exists.
            workspace.assert_changes_allowed()

            patch = workspace.diff()
            record["output"] = {"summary": result.summary[:600],
                                "files_changed": result.files_changed,
                                "turns": result.turns, "refusals": result.refusals,
                                "simulated": result.simulated, "error": result.error}
            record["cost_usd"] = result.cost_usd
            return {"outputs": {"implementer": result.as_output()},
                    "context": {"patch": patch, "refusals": result.refusals,
                                "code_error": result.error}}

    def route_after_implement(state: RunState) -> str:
        return "qa" if (state["context"].get("patch") or "").strip() else "no_change"

    def no_change(state: RunState) -> dict:
        reason = state["context"].get("code_error") or (
            "the implementer made no change — see its summary")
        return {"outcome": "no_change", "outcome_reason": reason}

    def qa(state: RunState) -> dict:
        """The repository's own tests, then an advisory review of the diff.

        Order matters for honesty: the deterministic result is produced first
        and the agent sees the diff, not the verdict it is expected to reach.
        """
        with ctx.step("qa") as record:
            workspace = ctx.workspace()
            command = ctx.config.get("test_command")
            tests = _run_tests(workspace, command)
            record["output"] = tests
            outputs = {}
            if ctx.enabled("qa_reviewer"):
                with ctx.step("review", agent_id="qa_reviewer") as review_record:
                    result = ctx.call("qa_reviewer",
                                      {"diff": state["context"].get("patch", ""),
                                       "test_result": as_text(tests)}, review_record)
                    outputs["qa_reviewer"] = result.output
            return {"outputs": outputs, "context": {"tests": tests}}

    def route_after_qa(state: RunState) -> str:
        tests = state["context"].get("tests", {})
        # No configured command is not a pass. It is an unknown, and an unknown
        # must not be allowed to look like a green build to whoever approves.
        return "gate" if tests.get("status") in ("passed", "not_configured") else "blocked"

    def blocked(state: RunState) -> dict:
        tests = state["context"].get("tests", {})
        return {"outcome": "blocked",
                "outcome_reason": f"the repository's tests failed ({tests.get('summary', '')}). "
                                  "No pull request was opened.",
                "decisions": [{"node": "qa", "decision": "blocked",
                               "why": "a failing suite blocks the PR regardless of "
                                      "the reviewer's opinion"}]}

    def gate(state: RunState) -> dict:
        """A human reads the diff before anything leaves the machine."""
        ctx.check("gate")
        patch = state["context"].get("patch", "")
        payload = {"ticket": state.get("ticket", {}).get("number"),
                   "summary": state["outputs"]["implementer"].get("summary"),
                   "files_changed": state["outputs"]["implementer"].get("files_changed"),
                   "impact": state["outputs"].get("impact_analyst", {}),
                   "tests": state["context"].get("tests", {}),
                   "review": state["outputs"].get("qa_reviewer", {}),
                   # The patch itself is in the hash: approving a diff must not
                   # authorise a different diff.
                   "patch": patch,
                   "simulated": ctx.simulated}
        decision = interrupt({
            "kind": "code_review",
            "summary": _gate_summary(state),
            "payload": payload,
            "payload_hash": canonical_hash(payload),
            "why": "no branch is pushed and no pull request is opened until a human "
                   "has read this diff",
        })
        return {"approval": {"mode": "human", **(decision or {})},
                "decisions": [{"node": "gate",
                               "decision": (decision or {}).get("status", "unknown"),
                               "why": (decision or {}).get("note") or ""}]}

    def route_after_gate(state: RunState) -> str:
        return "pull_request" if (state.get("approval") or {}).get("status") == "approved" \
            else "rejected"

    def rejected(state: RunState) -> dict:
        return {"outcome": "rejected",
                "outcome_reason": (state.get("approval") or {}).get("note")
                                  or "a reviewer rejected the change"}

    def pull_request(state: RunState) -> dict:
        """Push the branch and open a draft pull request. Never merges.

        Re-clones and re-applies the patch rather than trusting a checkout to
        have survived the approval, which may have taken days.
        """
        with ctx.step("pull_request") as record:
            workspace = ctx.workspace(reapply=state["context"].get("patch"))
            ticket = state.get("ticket", {})
            title = f"{ticket.get('number', 'SignalOps')}: " \
                    f"{ticket.get('short_description', 'automated change')}"[:120]
            body = _pr_body(state, ctx.simulated)
            commit = workspace.commit(title)
            if ctx.dry_run or not ctx.pr_sink.live:
                record["output"] = {"pushed": False, "dry_run": ctx.dry_run,
                                    "commit": commit,
                                    "would_open": {"branch": workspace.branch,
                                                   "title": title}}
                return {"outcome": "prepared",
                        "outcome_reason": "the branch and pull request were composed and "
                                          "not sent — this run is a dry run",
                        "external_writes": [{"target": "git.pull_request", "sent": False,
                                             "ref": None, "payload": {"title": title},
                                             "at": time.time()}]}
            workspace.push()
            pr = ctx.pr_sink.open(repo=ctx.config["repo_full_name"],
                                  branch=workspace.branch,
                                  base=workspace.base_branch, title=title, body=body)
            record["output"] = {"pushed": True, "url": pr.url, "commit": commit}
            return {"outcome": "pull_request_opened",
                    "outcome_reason": f"opened {pr.url}",
                    "context": {"pull_request_url": pr.url},
                    "external_writes": [{**pr.as_record(), "at": time.time()}]}

    def write_back(state: RunState) -> dict:
        """Tell the ticket where the change went."""
        with ctx.step("write_back") as record:
            ticket = state.get("ticket", {})
            url = state["context"].get("pull_request_url")
            note = (f"SignalOps opened a draft pull request: {url}" if url else
                    "SignalOps prepared a change but did not open a pull request "
                    "(dry run).")
            result = ctx.sink.work_note(sys_id=ticket.get("sys_id"),
                                        number=ticket.get("number"), note=note)
            record["output"] = {"sent": result.sent}
            return {"external_writes": [{**result.as_record(), "at": time.time()}]}

    graph = StateGraph(RunState)
    for name, fn in (("fetch", fetch), ("locate", locate), ("nothing_to_do", nothing_to_do),
                     ("analyse", analyse), ("implement", implement), ("no_change", no_change),
                     ("qa", qa), ("blocked", blocked), ("gate", gate),
                     ("rejected", rejected), ("pull_request", pull_request),
                     ("write_back", write_back)):
        graph.add_node(name, fn)

    graph.add_edge(START, "fetch")
    graph.add_edge("fetch", "locate")
    graph.add_conditional_edges("locate", route_after_locate,
                                {"analyse": "analyse", "nothing_to_do": "nothing_to_do"})
    graph.add_edge("nothing_to_do", END)
    graph.add_edge("analyse", "implement")
    graph.add_conditional_edges("implement", route_after_implement,
                                {"qa": "qa", "no_change": "no_change"})
    graph.add_edge("no_change", END)
    graph.add_conditional_edges("qa", route_after_qa, {"gate": "gate", "blocked": "blocked"})
    graph.add_edge("blocked", END)
    graph.add_conditional_edges("gate", route_after_gate,
                                {"pull_request": "pull_request", "rejected": "rejected"})
    graph.add_edge("rejected", END)
    graph.add_edge("pull_request", "write_back")
    graph.add_edge("write_back", END)
    return graph


def _read_files(ctx: RunContext, files: list[str], limit: int = 6) -> str:
    """Contents of the located files, for the analyst to reason over."""
    workspace = ctx.workspace()
    parts = []
    for relative in files[:limit]:
        try:
            parts.append(f"--- {relative} ---\n{workspace.read(relative)}")
        except Exception as error:                     # noqa: BLE001
            parts.append(f"--- {relative} ---\n(could not read: {error})")
    return "\n\n".join(parts)


def _run_tests(workspace, command: str | None) -> dict:
    """Run the repository's own suite. Authoritative.

    The command comes from workflow configuration and never from ticket text —
    a ticket that could choose the command would be a ticket that could run
    anything.
    """
    if not command:
        return {"status": "not_configured",
                "summary": "no test command is configured for this workflow, so nothing "
                           "verified this change",
                "exit_code": None}
    try:
        completed = subprocess.run(
            command, cwd=workspace.path, shell=True, capture_output=True, text=True,
            timeout=TEST_TIMEOUT_SECONDS,
            env={**os.environ, "CI": "1"})
    except subprocess.TimeoutExpired:
        return {"status": "failed", "exit_code": None,
                "summary": f"the test command timed out after {TEST_TIMEOUT_SECONDS}s",
                "output": ""}
    tail = (completed.stdout or "")[-4000:] + (completed.stderr or "")[-4000:]
    return {"status": "passed" if completed.returncode == 0 else "failed",
            "exit_code": completed.returncode,
            "summary": f"`{command}` exited {completed.returncode}",
            "output": tail}


def _gate_summary(state: RunState) -> str:
    ticket = state.get("ticket", {})
    implementer = state["outputs"].get("implementer", {})
    impact = state["outputs"].get("impact_analyst", {})
    tests = state["context"].get("tests", {})
    files = implementer.get("files_changed", [])
    bits = [f"{ticket.get('number', 'ticket')} — {len(files)} file(s) changed"]
    if impact.get("complexity"):
        bits.append(f"{impact['complexity']} change, {impact.get('risk', 'unknown')} risk")
    bits.append(f"tests {tests.get('status', 'unknown')}")
    return ", ".join(bits) + ". Review the diff before it leaves the machine."


def _pr_body(state: RunState, simulated: bool) -> str:
    ticket = state.get("ticket", {})
    implementer = state["outputs"].get("implementer", {})
    impact = state["outputs"].get("impact_analyst", {})
    tests = state["context"].get("tests", {})
    approval = state.get("approval") or {}
    lines = []
    if simulated:
        lines += ["> **Simulated run** — no model was called. Do not merge.", ""]
    lines += [
        f"Opened by SignalOps for **{ticket.get('number', 'a ticket')}**.",
        "",
        "### What changed", "", implementer.get("summary", "(no summary)"), "",
        "### Checks", "",
        f"- Repository tests: **{tests.get('status', 'unknown')}** — {tests.get('summary', '')}",
    ]
    if impact and not impact.get("skipped"):
        lines.append(f"- Assessed complexity: **{impact.get('complexity', 'unknown')}**, "
                     f"risk **{impact.get('risk', 'unknown')}**")
    lines += [
        f"- Reviewed and approved by: **{approval.get('by', 'a reviewer')}**"
        + (f" — {approval['note']}" if approval.get("note") else ""),
        "",
        "This pull request was opened by an automated workflow and is a draft. It was "
        "not merged and cannot be merged by the account that opened it.",
    ]
    return "\n".join(lines)
