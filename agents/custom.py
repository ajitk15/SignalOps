"""User-authored agents, and the rules that keep them inside the envelope.

The catalogue agents are declared in code; these are created from the UI. That
does not move the safety envelope into the UI. Three rules hold regardless of
what a form submits:

1. **Tools are chosen from a grantable set, never typed.** Only the Claude
   Agent SDK tools the platform is willing to hand out — Read, Glob, Grep,
   Edit, Write — can be selected. A shell, a web fetch, anything in
   `SDK_TOOLS_NEVER_GRANTED`, is rejected, so a custom agent cannot escalate to
   arbitrary code the way a `Bash` tool would allow.

2. **The tier is derived, not declared.** An agent that can Edit or Write is
   `write_code`; otherwise it is `read`. The client never sends a tier, so it
   cannot claim `read` while holding a write tool.

3. **The safety preamble is prepended and the override guard runs.** A custom
   prompt is held to the same rule as operator guidance — an attempt to talk
   past the safety rules is refused — and the preamble is always in front of
   it, so ticket text stays data rather than instructions.

Approval is the fourth control, enforced above this module: a non-admin's agent
cannot run until an admin has looked at exactly these fields and approved them.
"""
from __future__ import annotations

from dataclasses import dataclass

from agents.catalogue import ALLOWED_MODELS, SAFETY_PREAMBLE, TIER_RANK, Tier
from agents.guard import (SDK_TOOL_TIERS, SDK_TOOLS_NEVER_GRANTED,
                          GuardrailViolation, ResolvedAgent, check_guidance)

# The only tools a custom agent may be granted. A subset of the SDK tool map,
# excluding anything the platform never grants — belt and braces, since the
# never-granted set is also checked explicitly below.
GRANTABLE_TOOLS: tuple[str, ...] = tuple(
    name for name in SDK_TOOL_TIERS if name not in SDK_TOOLS_NEVER_GRANTED)

VALID_WORKFLOWS = ("incident_remediation", "ticket_to_pr", "both")


class CustomAgentInvalid(Exception):
    """The proposed agent is not acceptable and was not saved."""


@dataclass(frozen=True)
class ValidatedAgent:
    """A custom agent's fields after the envelope checks have passed."""
    name: str
    purpose: str
    explanation: str
    workflow: str
    model: str
    system_prompt: str
    tools: tuple[str, ...]
    tier: str                       # derived
    output_schema: dict | None


def derive_tier(tools: list[str]) -> Tier:
    """The lowest tier that covers every granted tool.

    Derived so it cannot be understated: an agent holding Edit is write_code
    whatever a form claims.
    """
    ceiling = Tier.read
    for tool in tools:
        tier = SDK_TOOL_TIERS.get(tool)
        if tier is not None and TIER_RANK[tier] > TIER_RANK[ceiling]:
            ceiling = tier
    return ceiling


def validate(*, name: str, purpose: str, explanation: str, workflow: str,
             model: str, system_prompt: str, tools: list[str],
             output_schema: dict | None = None) -> ValidatedAgent:
    """Check a proposed agent and return its validated, tier-derived form."""
    name = (name or "").strip()
    purpose = (purpose or "").strip()
    system_prompt = (system_prompt or "").strip()

    if not name:
        raise CustomAgentInvalid("a name is required")
    if len(purpose) < 8:
        raise CustomAgentInvalid("a one-line purpose is required")
    if len(system_prompt) < 20:
        raise CustomAgentInvalid("the instructions are too short to be a real prompt")
    if workflow not in VALID_WORKFLOWS:
        raise CustomAgentInvalid(f"workflow must be one of {', '.join(VALID_WORKFLOWS)}")
    if model not in ALLOWED_MODELS:
        raise CustomAgentInvalid(f"model must be one of {', '.join(ALLOWED_MODELS)}")

    cleaned_tools = []
    for tool in tools or []:
        if tool in SDK_TOOLS_NEVER_GRANTED:
            raise CustomAgentInvalid(
                f"{tool} can never be granted to any agent")
        if tool not in GRANTABLE_TOOLS:
            raise CustomAgentInvalid(f"{tool!r} is not a grantable tool")
        if tool not in cleaned_tools:
            cleaned_tools.append(tool)

    # A rewritten task is held to the same override rule as operator guidance.
    check_guidance(system_prompt)

    tier = derive_tier(cleaned_tools)
    return ValidatedAgent(
        name=name, purpose=purpose, explanation=(explanation or "").strip(),
        workflow=workflow, model=model, system_prompt=system_prompt,
        tools=tuple(cleaned_tools), tier=tier.value, output_schema=output_schema)


def resolve_row(row) -> ResolvedAgent:
    """Turn a stored CustomAgent row into a ResolvedAgent for export/run.

    The safety preamble is prepended here, exactly as for a catalogue agent, so
    an exported custom agent carries the same defences.
    """
    from agents.catalogue import AgentSpec

    spec = AgentSpec(
        id=f"custom_{row.id[:8]}", name=row.name, purpose=row.purpose,
        explanation=row.explanation or "", workflow=row.workflow,
        tier=Tier(row.tier), tools=tuple(row.tools or ()),
        default_model=row.model, output_schema=row.output_schema or {},
        system_prompt=row.system_prompt)
    return ResolvedAgent(
        spec=spec, model=row.model, tools=tuple(row.tools or ()), tier=Tier(row.tier),
        system_prompt=f"{SAFETY_PREAMBLE}\n{row.system_prompt}",
        confidence_threshold=None, requires_approval=True, enabled=row.enabled)
