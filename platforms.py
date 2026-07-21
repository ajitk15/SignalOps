"""Platform profiles: per-platform wording, knowledge and MCP tooling.

The pipeline and agents read everything platform-specific from here instead of
hardcoding MQ, so a new platform is a profile entry plus (optionally) an MCP
server — no agent or pipeline code changes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
PLATFORMS_PATH = ROOT / "config" / "platforms.yaml"


@dataclass
class PlatformProfile:
    key: str
    display_name: str
    domain: str
    escalation_team: str
    next_step: str
    knowledge_file: str | None = None
    mcp_server: dict | None = None
    investigation_tools: list[str] = field(default_factory=list)

    def knowledge_text(self) -> str:
        if not self.knowledge_file:
            return "(no platform knowledge base configured)"
        path = ROOT / self.knowledge_file
        return path.read_text(encoding="utf-8") if path.exists() else "(knowledge base file missing)"


def load_profiles() -> dict[str, PlatformProfile]:
    raw = yaml.safe_load(PLATFORMS_PATH.read_text(encoding="utf-8"))["platforms"]
    return {key: PlatformProfile(key=key, **value) for key, value in raw.items()}


_PROFILES: dict[str, PlatformProfile] | None = None


def profile_for(platform: str | None) -> PlatformProfile:
    global _PROFILES
    if _PROFILES is None:
        _PROFILES = load_profiles()
    return _PROFILES.get(platform or "", _PROFILES["generic"])
