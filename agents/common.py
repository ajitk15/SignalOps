"""Shared config loading, agent-call helper, and audit logging for the pipeline."""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"

logger = logging.getLogger("mq_pipeline")

# The MQ+ACE MCP server (C:\Workspace\accready\mqacemcp\mqacemcpserver) runs
# streamable-http with TLS (self-signed cert) and HTTP Basic Auth by default
# — see its logs/app-*.log on startup. Configured here via env vars rather
# than a checked-in .mcp.json so no credential ever lives in a repo file.
MQ_MCP_URL = os.environ.get("MQ_MCP_URL", "https://localhost:8010/mcp")
MQ_MCP_AUTH_USER = os.environ.get("MQ_MCP_AUTH_USER", "mcpadmin")
MQ_MCP_AUTH_PASSWORD = os.environ.get("MQ_MCP_AUTH_PASSWORD", "")
# Path to the server's self-signed cert, so the CLI subprocess can trust it
# without disabling TLS verification outright. Not secret — just a public
# cert file — so a real default path is fine here.
MQ_MCP_TLS_CERT = os.environ.get(
    "MQ_MCP_TLS_CERT", r"C:\Workspace\accready\mqacemcp\certs\cert.pem"
)


def _mcp_servers_config() -> dict[str, Any]:
    server: dict[str, Any] = {"type": "http", "url": MQ_MCP_URL}
    if MQ_MCP_AUTH_PASSWORD:
        token = base64.b64encode(
            f"{MQ_MCP_AUTH_USER}:{MQ_MCP_AUTH_PASSWORD}".encode()
        ).decode()
        server["headers"] = {"Authorization": f"Basic {token}"}
    else:
        logger.warning(
            "MQ_MCP_AUTH_PASSWORD not set — connecting to %s without Basic Auth, "
            "which will likely be rejected by the server.",
            MQ_MCP_URL,
        )
    return {"ibm-mq": server}


def _subprocess_env() -> dict[str, str] | None:
    """Trust the MQ MCP server's self-signed cert for the spawned CLI's TLS
    stack, instead of disabling certificate verification altogether."""
    # This workflow uses the locally authenticated Claude Code session.  When
    # ANTHROPIC_API_KEY is inherited from the application environment, Claude
    # Code gives it precedence and disables its connector/MCP path, causing
    # the opaque "error result: success" failure before an agent can run.
    env = {"ANTHROPIC_API_KEY": ""}
    if MQ_MCP_TLS_CERT and Path(MQ_MCP_TLS_CERT).exists():
        env["NODE_EXTRA_CA_CERTS"] = MQ_MCP_TLS_CERT
        return env
    logger.warning("MQ MCP TLS cert not found at %s — HTTPS connection may fail "
                   "certificate verification.", MQ_MCP_TLS_CERT)
    return env


@dataclass
class Watchlist:
    poll_interval_seconds: int
    queues: list[dict]
    channels: list[dict]
    max_consecutive_failures_before_backoff: int
    backoff_multiplier: int
    max_backoff_seconds: int


@dataclass
class AgentModels:
    diagnostician: str
    report_writer: str


def load_watchlist() -> Watchlist:
    raw = yaml.safe_load((CONFIG_DIR / "watchlist.yaml").read_text())
    return Watchlist(
        poll_interval_seconds=raw["poll_interval_seconds"],
        queues=raw.get("queues", []),
        channels=raw.get("channels", []),
        max_consecutive_failures_before_backoff=raw.get("max_consecutive_failures_before_backoff", 3),
        backoff_multiplier=raw.get("backoff_multiplier", 2),
        max_backoff_seconds=raw.get("max_backoff_seconds", 600),
    )


def load_agent_models() -> AgentModels:
    raw = yaml.safe_load((CONFIG_DIR / "agents.yaml").read_text())["agents"]
    return AgentModels(
        diagnostician=raw["diagnostician"]["model"],
        report_writer=raw["report_writer"]["model"],
    )


@dataclass
class AgentCallResult:
    agent_name: str
    model: str
    parsed: dict[str, Any] | None
    raw_text: str
    cost_usd: float
    input_tokens: int
    output_tokens: int
    duration_ms: int
    mq_commands_used: list[str] = field(default_factory=list)


async def call_agent_json(
    *,
    agent_name: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    allowed_tools: list[str],
) -> AgentCallResult:
    """Call a single-shot agent, expecting it to reply with JSON.

    Read-only by construction: callers only ever pass DISPLAY-class MQ tool
    names in allowed_tools (see agents/diagnostician.py).
    """
    start = time.monotonic()
    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model,
        allowed_tools=allowed_tools,
        mcp_servers=_mcp_servers_config(),
        env=_subprocess_env(),
        # No terminal is attached under uvicorn, so the SDK's default
        # permission_mode ("default", which prompts for tool approval)
        # would hang forever. allowed_tools already restricts each agent to
        # a tight, DISPLAY-only tool set, so bypassing the interactive
        # prompt here doesn't widen what the agent can actually do.
        permission_mode="bypassPermissions",
    )

    raw_text = ""
    cost_usd = 0.0
    input_tokens = 0
    output_tokens = 0

    async for message in query(prompt=user_prompt, options=options):
        if isinstance(message, ResultMessage):
            if message.subtype != "success":
                raise RuntimeError(f"{agent_name} agent failed: {message.subtype}")
            raw_text = message.result or ""
            cost_usd = message.total_cost_usd or 0.0
            usage = message.usage or {}
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)

    duration_ms = int((time.monotonic() - start) * 1000)

    parsed: dict[str, Any] | None
    try:
        parsed = json.loads(_extract_json_block(raw_text))
    except (json.JSONDecodeError, ValueError):
        parsed = None
        logger.warning("agent=%s returned non-JSON output: %s", agent_name, raw_text[:200])

    # Agents self-report which MQ tools they invoked in their JSON response
    # (see each agent's system prompt / schema) — this is what actually
    # drives the audit-trail requirement, since it doesn't depend on
    # introspecting SDK-internal message shapes.
    mq_commands_used = (parsed or {}).get("mq_commands_used", []) if parsed else []
    if mq_commands_used:
        logger.info("audit agent=%s mq_commands_used=%s", agent_name, mq_commands_used)

    result = AgentCallResult(
        agent_name=agent_name,
        model=model,
        parsed=parsed,
        raw_text=raw_text,
        cost_usd=cost_usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        duration_ms=duration_ms,
        mq_commands_used=mq_commands_used,
    )
    logger.info(
        "agent=%s model=%s cost_usd=%.5f tokens_in=%d tokens_out=%d duration_ms=%d",
        agent_name, model, cost_usd, input_tokens, output_tokens, duration_ms,
    )
    return result


_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json_block(text: str) -> str:
    """Agents are asked to respond with pure JSON, but in practice smaller
    models sometimes "think out loud" first and only fence the JSON later
    in the response (observed with the Diagnostician agent). Look for a
    fenced ```json {...}``` block anywhere in the text before falling back
    to the whole-text-is-JSON case; take the LAST such block, since a model
    thinking out loud may show example/draft JSON earlier and the real
    answer last.
    """
    text = text.strip()
    fenced_matches = _FENCED_JSON_RE.findall(text)
    if fenced_matches:
        return fenced_matches[-1].strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[len("json"):]
    return text.strip()
