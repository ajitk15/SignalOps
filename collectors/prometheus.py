"""Prometheus collector: each PromQL result series becomes one Observation.

The series' Prometheus labels are copied straight into Observation.labels —
the two label models line up, which is what makes this adapter thin. The
observation name comes from a configured label (default: the metric name plus
instance), so rule matching and correlation work on the same vocabulary as
every other source.
"""
from __future__ import annotations

import os

import httpx

from agents.common import SourceConfig
from collectors import register
from detection import Observation


@register("prometheus")
class PrometheusSource:
    def __init__(self, source: SourceConfig):
        self.name = source.name
        self._base_url = source.options["url"].rstrip("/")
        self._targets = source.targets
        token = os.getenv(source.options["auth_env"], "") if source.options.get("auth_env") else ""
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def collect(self) -> list[Observation]:
        observations = []
        async with httpx.AsyncClient(timeout=15) as client:
            for target in self._targets:
                response = await client.get(f"{self._base_url}/api/v1/query",
                                            params={"query": target["query"]}, headers=self._headers)
                response.raise_for_status()
                for series in response.json()["data"]["result"]:
                    series_labels = dict(series.get("metric", {}))
                    name_label = target.get("name_label", "instance")
                    object_name = series_labels.get(name_label) \
                        or series_labels.get("__name__") or target["name"]
                    observations.append(Observation(
                        source=self.name,
                        object_type=target.get("object_type", "series"),
                        object_name=object_name,
                        metric=target["metric"],
                        value=float(series["value"][1]),
                        labels={**series_labels, **target.get("labels", {})},
                        threshold=target.get("threshold"),
                    ))
        return observations
