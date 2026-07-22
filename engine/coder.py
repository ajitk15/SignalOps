"""The implementer, on the Claude Agent SDK.

Workflow A's agents answer a question once and return JSON; the Client SDK with
a schema is exactly right for that. This agent is different in kind — it has to
read around a repository, decide what to change, and make the edits. That is an
agent loop over file tools, which is what the Agent SDK is. Building it on the
raw API would mean reimplementing Read, Edit, Glob and Grep plus the loop, and
getting all of it slightly wrong.

Adopting it does not loosen the envelope. Three constraints:

**No Bash.** The Agent SDK ships one and it is genuinely useful, but a shell is
a way to reach everything the tool allowlist just finished restricting. The
repository's test suite still runs — from a command in validated configuration,
executed by a deterministic node. The agent's opinion about the tests is
advisory; the exit code is not.

**`can_use_tool` vetoes every individual write.** The path allowlist is checked
at the moment of the call, not only afterwards on the diff. Refusing after the
fact still leaves the run having produced an edit somebody has to review and
throw away, and a refusal the agent can see is one it can work around by
choosing a different file.

**No inherited configuration.** `setting_sources=None` so the operator's
`~/.claude` — their CLAUDE.md, their skills, their MCP servers — never leaks
into a run acting on someone else's repository.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from agents.guard import (SDK_TOOLS_NEVER_GRANTED, GuardrailViolation,
                          ResolvedAgent, assert_sdk_tool_allowed, sdk_tools_for)
from engine.budget import cost_of
from integrations.repo import RepoWorkspace, is_protected

logger = logging.getLogger("engine.coder")

# The tool list is derived from the agent's declared tier (see
# agents.guard.sdk_tools_for), not written out here. A hardcoded list at the
# call site would mean retiering an agent in the catalogue changed a label and
# nothing else.
FORBIDDEN_TOOLS = list(SDK_TOOLS_NEVER_GRANTED)

MAX_TURNS = 40


@dataclass
class CodeResult:
    summary: str
    files_changed: list[str]
    cost_usd: float
    turns: int
    simulated: bool
    refusals: list[str] = field(default_factory=list)
    error: str | None = None

    def as_output(self, confidence: float = 0.0) -> dict:
        return {"summary": self.summary, "files_changed": self.files_changed,
                "confidence": confidence}


class CodeAgentUnavailable(Exception):
    """No credential, so no code was written. Not a failure of the change."""


def _permission_callback(agent: ResolvedAgent, workspace: RepoWorkspace,
                         refusals: list[str]):
    """Veto writes outside the allowlist, at the moment they are attempted."""

    async def can_use_tool(tool_name: str, tool_input: dict, context):
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        # The tier decides, not a list written out here. An agent below
        # write_code is refused Edit and Write by this call alone.
        try:
            assert_sdk_tool_allowed(agent, tool_name)
        except GuardrailViolation as violation:
            refusals.append(str(violation))
            return PermissionResultDeny(message=str(violation))

        path = tool_input.get("file_path") or tool_input.get("path")
        if path and tool_name in ("Edit", "Write"):
            try:
                relative = Path(path).resolve().relative_to(workspace.path.resolve())
            except (ValueError, OSError):
                reason = f"{path} is outside the repository checkout"
                refusals.append(reason)
                return PermissionResultDeny(message=reason)
            blocked = is_protected(str(relative),
                                   allow_dependencies=workspace.allow_dependencies)
            if blocked:
                refusals.append(blocked)
                # The message reaches the agent, so it says what the rule is
                # rather than inviting a hunt for a path that slips through.
                return PermissionResultDeny(
                    message=f"Refused: {blocked}. This restriction is enforced by the "
                            "platform and cannot be worked around; if the change truly "
                            "requires it, say so in your summary and stop.")
        return PermissionResultAllow()

    return can_use_tool


def build_prompt(agent: ResolvedAgent, ticket: dict, analysis: dict,
                 files: list[str]) -> str:
    """The task, with the ticket fenced as data exactly as elsewhere."""
    from engine.llm import as_text, render_task

    sections = {"ticket": as_text(ticket)}
    if analysis:
        sections["impact_analysis"] = as_text(analysis)
    if files:
        sections["files_identified_as_relevant"] = "\n".join(files)
    return (
        f"{render_task(sections)}\n\n"
        "Make the change in the repository you are working in. Edit files directly. "
        "Do not run commands — the test suite is run for you afterwards and its "
        "result, not your assessment, decides whether this becomes a pull request.\n\n"
        "When you are done, reply with a one-paragraph summary of what you changed "
        "and why. If you concluded no change should be made, say that instead."
    )


async def implement(*, agent: ResolvedAgent, workspace: RepoWorkspace, ticket: dict,
                    analysis: dict, files: list[str],
                    budget_usd: float | None = None) -> CodeResult:
    """Run the implementer against the checkout. Returns what it changed."""
    try:
        from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions,
                                      ResultMessage, TextBlock, query)
    except ImportError as error:                       # pragma: no cover
        raise CodeAgentUnavailable("claude-agent-sdk is not installed") from error

    refusals: list[str] = []
    options = ClaudeAgentOptions(
        # The system prompt is the resolved one — same safety preamble, same
        # operator guidance, same customisation as every other agent.
        system_prompt=agent.system_prompt,
        allowed_tools=sdk_tools_for(agent),
        disallowed_tools=FORBIDDEN_TOOLS,
        can_use_tool=_permission_callback(agent, workspace, refusals),
        cwd=str(workspace.path),
        model=agent.model,
        max_turns=MAX_TURNS,
        max_budget_usd=budget_usd,
        # acceptEdits: the platform's own permission callback is the gate, and
        # there is no human at a terminal to answer a prompt.
        permission_mode="acceptEdits",
        # Do not inherit the operator's CLAUDE.md, skills or MCP servers.
        setting_sources=None,
    )

    text_parts: list[str] = []
    cost = 0.0
    turns = 0
    error = None
    try:
        async for message in query(prompt=build_prompt(agent, ticket, analysis, files),
                                   options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        text_parts.append(block.text)
            elif isinstance(message, ResultMessage):
                cost = message.total_cost_usd or 0.0
                turns = message.num_turns or 0
                if message.is_error:
                    error = message.result or "the code agent reported an error"
    except Exception as failure:                       # noqa: BLE001
        # A failure here must leave the run legible rather than raising through
        # the graph with an SDK traceback.
        logger.exception("code agent failed")
        error = f"{type(failure).__name__}: {failure}"

    changed = workspace.changed_files()
    summary = "\n".join(text_parts).strip() or "The agent produced no summary."
    return CodeResult(summary=summary[:4000], files_changed=changed, cost_usd=cost,
                      turns=turns, simulated=False, refusals=refusals, error=error)


async def implement_simulated(*, workspace: RepoWorkspace, ticket: dict,
                              **_) -> CodeResult:
    """No API key: touch nothing, and say plainly that nothing was written.

    Deliberately does not fabricate a diff. A simulated code change would be
    the one simulated output that could be mistaken for real work worth
    reviewing.
    """
    return CodeResult(
        summary="[SIMULATED] No API key is configured, so no code was written. "
                "The workflow ran end to end and stopped short of a real change.",
        files_changed=[], cost_usd=0.0, turns=0, simulated=True)


def estimate_cost(model: str, usage) -> float:
    """Fallback when the SDK does not report a cost."""
    if not usage:
        return 0.0
    return cost_of(model, getattr(usage, "input_tokens", 0) or 0,
                   getattr(usage, "output_tokens", 0) or 0)
