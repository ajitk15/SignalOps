"""Validated output schemas, one per catalogue agent.

The catalogue's `output_schema` is documentation — it renders in the UI so a
human can see what an agent returns. This module is the enforcement: every
agent's reply is parsed into the model below, and a reply that does not fit
fails the node.

That distinction matters because the alternative is coercion. An agent whose
malformed answer gets patched up with defaults produces a plausible-looking
plan built on a field nobody supplied, and the confidence score — the thing the
routing decision rests on — is exactly the field a lenient parser would invent.
Failing loudly costs a retry; guessing costs trust in every number downstream.

`test_engine.py` asserts these stay in step with the catalogue's documented
shape, so the two cannot drift apart silently.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AgentOutput(BaseModel):
    """Common base. Every agent reports a confidence it is willing to defend."""
    model_config = ConfigDict(extra="forbid")

    confidence: float = Field(ge=0.0, le=1.0)


class TriageOutput(AgentOutput):
    in_scope: bool
    urgency: Literal["P1", "P2", "P3", "P4"]
    reason: str


class DiagnosisOutput(AgentOutput):
    root_cause: str
    evidence: list[str]
    related_change: str | None


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str
    verify: str
    rollback: str


class RemediationPlanOutput(AgentOutput):
    steps: list[PlanStep]
    risk: Literal["low", "medium", "high"]
    requires_downtime: bool


class CodeLocationOutput(AgentOutput):
    files: list[str]
    reasoning: str


class ImpactOutput(AgentOutput):
    complexity: Literal["trivial", "small", "moderate", "large"]
    risk: Literal["low", "medium", "high"]
    blast_radius: str
    test_coverage: str
    concerns: list[str]


class ImplementationOutput(AgentOutput):
    summary: str
    files_changed: list[str]


class ReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: Literal["low", "medium", "high"]
    file: str
    note: str


class ReviewOutput(AgentOutput):
    verdict: Literal["approve", "request_changes"]
    findings: list[ReviewFinding]


SCHEMAS: dict[str, type[AgentOutput]] = {
    "triage": TriageOutput,
    "diagnostician": DiagnosisOutput,
    "remediation_planner": RemediationPlanOutput,
    "code_locator": CodeLocationOutput,
    "impact_analyst": ImpactOutput,
    "implementer": ImplementationOutput,
    "qa_reviewer": ReviewOutput,
}


def schema_for(agent_id: str) -> type[AgentOutput]:
    schema = SCHEMAS.get(agent_id)
    if schema is None:
        raise KeyError(f"no output schema declared for agent {agent_id!r}")
    return schema
