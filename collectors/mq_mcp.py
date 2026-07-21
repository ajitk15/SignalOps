"""IBM MQ/ACE collector: adapts source targets onto the existing MCP client.

The MCP parsing (integrations/mq_ace_mcp.py) is unchanged; this adapter only
translates the source-driven target shape into the queue/channel dicts that
client expects, and stamps observations with the configured source name so
multiple MQ sources stay distinguishable downstream.
"""
from __future__ import annotations

from agents.common import SourceConfig
from collectors import register
from detection import Observation
from integrations.mq_ace_mcp import MqAceMcpCollector


@register("mq_mcp")
class MqMcpSource:
    def __init__(self, source: SourceConfig):
        self.name = source.name
        self._client = MqAceMcpCollector()
        self._queues, self._channels = [], []
        for target in source.targets:
            labels = target.get("labels", {})
            entry = {"name": target["name"], **labels}
            if target.get("qmgr") or labels.get("qmgr"):
                entry["qmgr"] = target.get("qmgr") or labels.get("qmgr")
            if target.get("object_type", "queue") == "channel":
                self._channels.append(entry)
            else:
                entry["depth_threshold"] = target.get("threshold")
                self._queues.append(entry)

    async def collect(self) -> list[Observation]:
        observations = await self._client.collect_queues(self._queues)
        observations += await self._client.collect_channels(self._channels)
        for observation in observations:
            observation.source = self.name
        return observations
