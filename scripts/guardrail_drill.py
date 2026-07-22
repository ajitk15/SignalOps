"""Run the guardrail drills and print what held.

The test suite proves these; this prints them, because "the guardrails pass"
is a claim someone has to be able to check without reading test code. Run it
before a demo, after changing the catalogue, or when someone asks what stops
an agent doing something.

    python scripts/guardrail_drill.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.catalogue import CATALOGUE, Tier  # noqa: E402
from agents.guard import (SDK_TOOLS_NEVER_GRANTED, GuardrailViolation,  # noqa: E402
                          check_guidance, resolve, sdk_tools_for)
from integrations.repo import is_protected  # noqa: E402
from tests.injection_corpus import CORPUS  # noqa: E402

OK = "  ok  "
BAD = " FAIL "
failures = 0


def check(label: str, passed: bool, detail: str = "") -> None:
    global failures
    if not passed:
        failures += 1
    print(f"[{OK if passed else BAD}] {label}{('  — ' + detail) if detail else ''}")


def section(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


section("Injection corpus: override attempts refused as customisation")
for payload in CORPUS:
    if payload.stopped_by != "filter":
        continue
    try:
        check_guidance(payload.text)
        check(payload.name, False, f"NOT refused — goal was to {payload.goal}")
    except GuardrailViolation:
        check(payload.name, True, f"refused (goal: {payload.goal})")

section("Injection corpus: task-shaped payloads stopped structurally")
for payload in CORPUS:
    if payload.stopped_by == "filter":
        continue
    print(f"[ info ] {payload.name}\n"
          f"           goal:       {payload.goal}\n"
          f"           stopped by: {payload.stopped_by}")

section("Path allowlist")
for path in (".github/workflows/ci.yml", "config/id_rsa", "app/.env", "infra/main.tf",
             "secrets/db.yaml", "../../etc/passwd", "requirements.txt"):
    check(f"refused: {path}", is_protected(path) is not None)
for path in ("src/app.py", "README.md", "docs/environment.md"):
    check(f"writable: {path}", is_protected(path) is None)

section("Tier enforcement (Claude Agent SDK tools)")
for spec in CATALOGUE:
    resolved = resolve(spec)
    granted = sdk_tools_for(resolved)
    writes = [t for t in granted if t in ("Edit", "Write")]
    if spec.tier is Tier.read:
        check(f"{spec.id} ({spec.tier.value}) has no write tools", not writes,
              f"granted {granted}")
    else:
        check(f"{spec.id} ({spec.tier.value}) may edit", bool(writes),
              f"granted {granted}")
    forbidden = [t for t in granted if t in SDK_TOOLS_NEVER_GRANTED]
    check(f"{spec.id} has no shell or network tool", not forbidden)

section("Catalogue integrity")
mutating = [s.id for s in CATALOGUE if s.tier is not Tier.read]
check("exactly one agent can mutate anything", mutating == ["implementer"],
      f"mutating: {mutating}")
check("every agent's prompt carries the safety rules",
      all("DATA, never" in resolve(s).system_prompt for s in CATALOGUE))

print(f"\n{'=' * 60}")
print("ALL DRILLS HELD" if not failures else f"{failures} DRILL(S) FAILED")
print("=" * 60)
sys.exit(1 if failures else 0)
