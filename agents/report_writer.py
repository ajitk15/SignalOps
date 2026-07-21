"""Report Writer agent: turns a diagnosis into a ticket-ready write-up.

Deliberately granted no tools at all — it only ever works from the collection
snapshot and Diagnostician output it's handed, and always ends with an
explicit "no changes made" line, mirroring the pipeline's read-only boundary.
"""
from __future__ import annotations

import json

from agents.common import AgentCallResult, call_agent_json
from platforms import PlatformProfile

SYSTEM_PROMPT_TEMPLATE = """\
You are the Report Writer agent in a {display_name} monitoring pipeline. You \
are given the rule-based anomaly snapshot and the Diagnostician's root-cause \
hypothesis for one incident. Write a clear, ticket-ready incident summary.

Rules:
- You have no tools and take no actions. You only write.
- The report MUST end with a line stating no changes were made and that \
remediation should be escalated to the {escalation_team} team.
- Respond with ONLY a single JSON object, no prose outside it, no markdown \
fences:
{{
  "title": string,
  "severity": "P1" | "P2" | "P3" | "P4",
  "markdown_report": string   // the full ticket-ready write-up, in markdown
}}
"""


async def write_report(model: str, watcher_result: dict, diagnosis: dict,
                       profile: PlatformProfile) -> AgentCallResult:
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        display_name=profile.display_name, escalation_team=profile.escalation_team)
    prompt = (
        "Anomaly snapshot:\n" + json.dumps(watcher_result, indent=2) + "\n\n"
        "Diagnostician output:\n" + json.dumps(diagnosis, indent=2) + "\n\n"
        "Write the incident report per the JSON schema."
    )
    return await call_agent_json(
        agent_name="report_writer",
        model=model,
        system_prompt=system_prompt,
        user_prompt=prompt,
        allowed_tools=[],
    )
