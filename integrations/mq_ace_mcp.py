"""Deterministic, read-only MQ/ACE MCP collector (no LLM involved)."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from detection import Observation

SERVER_ENV = Path(r"C:\Workspace\accready\mqacemcp\mqacemcpserver\.env")
DEFAULT_CERT = Path(r"C:\Workspace\accready\mqacemcp\certs\cert.pem")
_DEPTH_RE = re.compile(r"\bCURDEPTH\((\d+)\)", re.IGNORECASE)
_CHANNEL_RE = re.compile(r"\bSTATUS\(([^)]+)\)", re.IGNORECASE)


def _settings() -> dict[str, str]:
    local = dotenv_values(SERVER_ENV) if SERVER_ENV.exists() else {}
    host = os.getenv("MQ_MCP_HOST") or local.get("MCP_HOST") or "127.0.0.1"
    if host in {"0.0.0.0", ""}: host = "127.0.0.1"
    cert = os.getenv("MQ_MCP_TLS_CERT") or local.get("MCP_TLS_CERT") or str(DEFAULT_CERT)
    cert_path = Path(cert)
    if cert and not cert_path.is_absolute():
        cert_path = (SERVER_ENV.parent / cert_path).resolve()
    cert = str(cert_path)
    scheme = "https" if cert and cert_path.exists() else "http"
    return {
        "url": os.getenv("MQ_MCP_URL") or local.get("MCP_REMOTE_SERVER_URL") or f"{scheme}://{host}:{local.get('MCP_PORT', '8010')}/mcp",
        "user": os.getenv("MQ_MCP_AUTH_USER") or local.get("MCP_AUTH_USER") or "",
        "password": os.getenv("MQ_MCP_AUTH_PASSWORD") or local.get("MCP_AUTH_PASSWORD") or "",
        "cert": cert,
    }


def _http_client_factory(cert: str):
    def factory(headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(headers=headers, timeout=timeout or httpx.Timeout(30, read=180),
                                 auth=auth, verify=cert if cert and Path(cert).exists() else True,
                                 follow_redirects=True)
    return factory


def _text(result: Any) -> str:
    return "\n".join(block.text for block in result.content if hasattr(block, "text"))


class MqAceMcpCollector:
    """Batches safe inspect calls and converts tool text into observations."""
    def __init__(self):
        self.settings = _settings()

    async def _call(self, tool: str, arguments: dict) -> str:
        auth = httpx.BasicAuth(self.settings["user"], self.settings["password"]) if self.settings["user"] and self.settings["password"] else None
        async with streamablehttp_client(self.settings["url"], auth=auth,
                httpx_client_factory=_http_client_factory(self.settings["cert"])) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool, arguments)
                if result.isError: raise RuntimeError(_text(result))
                return _text(result)

    async def collect_queues(self, queues: list[dict]) -> list[Observation]:
        observations = []
        for group in _group_by_qmgr(queues):
            text = await self._call("mq_queue_inspect", {"queue_names": [q["name"] for q in group],
                                    **({"qmgr_name": group[0]["qmgr"]} if group[0].get("qmgr") else {})})
            sections = _split_multi(text)
            for queue in group:
                section = sections.get(queue["name"], text if len(group) == 1 else "")
                for qmgr, depth in _queue_readings(section, queue.get("qmgr", "")):
                    observations.append(Observation("mq_mcp", "queue", f"{qmgr}/{queue['name']}" if qmgr else queue["name"],
                        "queue_depth", depth, labels={"qmgr": qmgr, "queue": queue["name"],
                        "environment": queue.get("environment", "unknown"),
                        "service": queue.get("service", f"{qmgr}:{queue['name']}"), "role": queue.get("role", "")},
                        threshold=queue.get("depth_threshold")))
        return observations

    async def collect_channels(self, channels: list[dict]) -> list[Observation]:
        observations = []
        for channel in channels:
            text = await self._call("mq_channel_inspect", {"channel_names": [channel["name"]],
                                    **({"qmgr_name": channel["qmgr"]} if channel.get("qmgr") else {})})
            match = _CHANNEL_RE.search(text)
            if match:
                observations.append(Observation("mq_mcp", "channel", channel["name"], "channel_status", match.group(1),
                    labels={"qmgr": channel.get("qmgr", ""), "environment": channel.get("environment", "unknown"),
                            "service": channel.get("service", channel["name"])}))
        return observations

    async def health(self) -> dict:
        text = await self._call("ace_search", {"search_strings": [""], "scope": "nodes"})
        return {"status": "ok", "url": self.settings["url"], "ace_catalog_readable": bool(text)}


def _group_by_qmgr(items: list[dict]) -> list[list[dict]]:
    groups: dict[str, list[dict]] = {}
    for item in items: groups.setdefault(item.get("qmgr", ""), []).append(item)
    return list(groups.values())


def _split_multi(text: str) -> dict[str, str]:
    sections = {}
    markers = list(re.finditer(r"[=═]{2,}\s*(?:Queue:\s*)?([^=═\r\n]+?)\s*[=═]{2,}", text))
    for index, marker in enumerate(markers):
        name = marker.group(1).strip(); end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        sections[name] = text[marker.end():end]
    return sections


def _queue_readings(text: str, configured_qmgr: str = "") -> list[tuple[str, int]]:
    """Extract every queue-manager/depth pair from DISPLAY output."""
    markers = list(re.finditer(r"Resolution chain:.*?\(([^()\r\n]+)\)", text, re.IGNORECASE))
    readings = []
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        depth = _DEPTH_RE.search(text[marker.end():end])
        if depth: readings.append((marker.group(1).strip(), int(depth.group(1))))
    if not readings:
        depth = _DEPTH_RE.search(text)
        if depth: readings.append((configured_qmgr, int(depth.group(1))))
    return readings
