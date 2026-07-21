"""Collector plugin layer.

A collector turns one configured source into Observations. Everything
downstream (rules, correlation, storage, dashboard) speaks Observation, so
adding a platform means one collector class plus config — nothing else.
"""
from __future__ import annotations

from typing import Callable, Protocol, runtime_checkable

from agents.common import SourceConfig, Watchlist
from detection import Observation


@runtime_checkable
class Collector(Protocol):
    name: str

    async def collect(self) -> list[Observation]: ...


_REGISTRY: dict[str, Callable[[SourceConfig], Collector]] = {}


def register(kind: str):
    def decorator(factory: Callable[[SourceConfig], Collector]):
        _REGISTRY[kind] = factory
        return factory
    return decorator


def build_collectors(watchlist: Watchlist) -> list[Collector]:
    collectors = []
    for source in watchlist.sources:
        factory = _REGISTRY.get(source.kind)
        if factory is None:
            raise ValueError(f"unknown collector kind '{source.kind}' for source '{source.name}' "
                             f"(registered: {sorted(_REGISTRY)})")
        collectors.append(factory(source))
    return collectors


# Import for side effect: each module registers its kind.
from collectors import mq_mcp  # noqa: E402,F401
from collectors import http_json  # noqa: E402,F401
from collectors import prometheus  # noqa: E402,F401
