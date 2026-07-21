"""Diagnostician agent: runs only when the Watcher flags an anomaly.

Gathers extra read-only context via mq_host_overview / mq_connection_verify
/ ace_search (the real composite tools on this MCP server — see
agents/watcher.py's note on tool-name verification), cross-references a
local knowledge base of common MQ/ACE failure patterns, and produces a
root-cause hypothesis with confidence and severity.
"""
from __future__ import annotations

import json
from pathlib import Path

from agents.common import AgentCallResult, call_agent_json

KNOWLEDGE_PATH = Path(__file__).resolve().parent.parent / "knowledge" / "mq_failure_patterns.md"

SYSTEM_PROMPT = """\
You are the Diagnostician agent in an IBM MQ monitoring pipeline. You are \
given one anomalous MQ object (queue or channel) that the Watcher agent \
flagged, plus a knowledge base of common failure patterns. Your job is to \
gather a little more read-only context and produce a root-cause hypothesis.

Rules:
- You may call mcp__ibm-mq__mq_host_overview (dspmq/dspmqver plus an \
optional read-only DISPLAY MQSC command for the object's queue manager), \
mcp__ibm-mq__mq_connection_verify (fact-checks connection details like \
host/port/channel against the manifest), and mcp__ibm-mq__ace_search \
(searches configured ACE nodes / cached BIP messages, for when the root \
cause may be an ACE flow rather than MQ itself). Only DISPLAY-class MQSC \
commands are ever permitted — never attempt ALTER, DEFINE, or DELETE.
- Call AT MOST ONE tool. Pick whichever single tool is most likely to be \
useful — do not chain multiple investigative tool calls. You have enough \
information after one call (or zero, if the Watcher's snapshot is already \
sufficient) to form a hypothesis.
- If a tool call comes back empty or "not found", say so in your evidence \
rather than filling the gap with a guess.
- Use the knowledge base to ground your hypothesis in a known pattern where \
possible; say so explicitly if the situation doesn't match anything listed.
- Assign a severity: P1 (production-impacting, urgent), P2 (degraded, needs \
prompt attention), P3 (minor/isolated), P4 (informational/low risk).
- Your FINAL message must be ONLY a single JSON object — no reasoning, no \
commentary, no markdown fences, before or after it. Do not think out loud \
in your last message; the schema below is the entire response:
{
  "object_name": string,
  "root_cause_hypothesis": string,
  "confidence": "low" | "medium" | "high",
  "severity": "P1" | "P2" | "P3" | "P4",
  "evidence": [string],          // what you observed that supports the hypothesis
  "matched_known_pattern": string|null,
  "mq_commands_used": [string]   // names of MCP tools you actually called
}
"""


async def diagnose(model: str, anomaly: dict) -> AgentCallResult:
    knowledge = KNOWLEDGE_PATH.read_text()
    prompt = (
        f"Anomalous object from the Watcher agent:\n{json.dumps(anomaly, indent=2)}\n\n"
        f"Known failure patterns (knowledge base):\n{knowledge}\n\n"
        "Investigate with mq_host_overview / mq_connection_verify / ace_search as needed, "
        "then respond with the JSON schema."
    )
    return await call_agent_json(
        agent_name="diagnostician",
        model=model,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=prompt,
        allowed_tools=[
            "mcp__ibm-mq__mq_host_overview",
            "mcp__ibm-mq__mq_connection_verify",
            "mcp__ibm-mq__ace_search",
        ],
    )
