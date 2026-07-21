"""Generic HTTP/JSON collector: watch anything with a JSON endpoint.

Each target extracts one value from the response by dotted path
(e.g. "measurements.0.value"). Credentials are referenced by env-var name
only — never inline in config.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

from agents.common import SourceConfig
from collectors import register
from detection import Observation


def extract_path(payload: Any, dotted: str) -> Any:
    """Walk a dotted path through dicts and lists: "a.0.b" -> payload["a"][0]["b"]."""
    current = payload
    for part in dotted.split("."):
        if isinstance(current, list):
            current = current[int(part)]
        else:
            current = current[part]
    return current


@register("http_json")
class HttpJsonSource:
    def __init__(self, source: SourceConfig):
        self.name = source.name
        self._url = source.options["url"]
        self._targets = source.targets
        self._verify = source.options.get("tls_verify", True)
        token = os.getenv(source.options["auth_env"], "") if source.options.get("auth_env") else ""
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def collect(self) -> list[Observation]:
        async with httpx.AsyncClient(timeout=15, verify=self._verify) as client:
            response = await client.get(self._url, headers=self._headers)
            response.raise_for_status()
            payload = response.json()
        observations = []
        for target in self._targets:
            value = extract_path(payload, target["json_path"])
            observations.append(Observation(
                source=self.name,
                object_type=target.get("object_type", "endpoint"),
                object_name=target["name"],
                metric=target["metric"],
                value=value,
                labels=dict(target.get("labels", {})),
                threshold=target.get("threshold"),
            ))
        return observations
