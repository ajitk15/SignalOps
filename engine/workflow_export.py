"""Export a workflow as a standalone, runnable Python app.

The agent export (agents/export.py) hands over the *actors*. This hands over the
*workflow*: the graph, the agents it calls, the setup to run it and a Dockerfile
to run it without setting anything up.

The exported files are real files in `engine/standalone/`, not strings built
here. That is deliberate — generated code that lives only inside a generator is
code nobody runs until a customer does, and by then its imports have rotted.
`test_engine.py` imports and compiles the standalone graph, so the thing being
shipped is the thing being tested.

Only the README is templated, because only the README has to name the workflow
it was exported from.
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from agents import export as agent_export
from agents.catalogue import for_workflow
from agents.guard import resolve
from engine import incident

STANDALONE = Path(__file__).resolve().parent / "standalone"
SCHEMAS = Path(__file__).resolve().parent.parent / "agents" / "schemas.py"

# Rendered into the README so the shape of the thing is visible before you read
# any code.
GRAPH_DIAGRAM = """\
enrich -> triage -+-> diagnose -> plan -> work note -> gate -+-> hand off (propose only)
                  |                                          |
                  +-> out of scope (end)                     +-> rejected (end)\
"""

TEMPLATES = {incident.TEMPLATE: {"name": "Incident remediation", "module": incident}}


def slug(name: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")


def bundle(*, template: str, workflow_name: str, agent_configs: dict) -> bytes:
    """Zip a runnable copy of one workflow."""
    meta = TEMPLATES.get(template)
    if meta is None:
        raise KeyError(f"no standalone export for template {template!r}")

    exported_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    root = f"signalops-{slug(workflow_name or meta['name'])}"
    specs = for_workflow(template)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        readme = (STANDALONE / "README.md").read_text(encoding="utf-8")
        archive.writestr(f"{root}/README.md",
                         readme.replace("{{WORKFLOW_NAME}}", workflow_name or meta["name"])
                               .replace("{{EXPORTED_AT}}", exported_at)
                               .replace("{{GRAPH}}", GRAPH_DIAGRAM))

        for name in ("workflow.py", "requirements.txt", "Dockerfile", "sample_ticket.json"):
            archive.writestr(f"{root}/{name}",
                             (STANDALONE / name).read_text(encoding="utf-8"))
        # Shipped as env.example because some tooling hides dotfiles; the README
        # tells the reader to copy it to .env.
        archive.writestr(f"{root}/.env.example",
                         (STANDALONE / "env.example").read_text(encoding="utf-8"))
        archive.writestr(f"{root}/.dockerignore",
                         ".venv/\n__pycache__/\ncheckpoints.db\ncheckpoints/\n.env\n")
        archive.writestr(f"{root}/.gitignore",
                         ".venv/\n__pycache__/\ncheckpoints.db\ncheckpoints/\n.env\n")

        # Verbatim: schemas.py imports nothing from this platform, so the
        # validation that runs here is the validation that runs there.
        archive.writestr(f"{root}/schemas.py", SCHEMAS.read_text(encoding="utf-8"))

        config = {"exported_at": exported_at, "template": template,
                  "workflow": workflow_name, "agents": {}}
        for spec in specs:
            resolved = resolve(spec, agent_configs.get(spec.id))
            archive.writestr(f"{root}/agents/{agent_export.filename_for(spec)}",
                             agent_export.to_markdown(spec, resolved))
            config["agents"][spec.id] = {
                "model": resolved.model, "tools": list(resolved.tools),
                "tier": resolved.tier.value, "enabled": resolved.enabled,
                "confidence_threshold": resolved.confidence_threshold,
                "requires_approval": resolved.requires_approval,
                "customised": spec.id in agent_configs,
            }
        archive.writestr(f"{root}/agents_config.json",
                         json.dumps(config, indent=2, sort_keys=True))
    return buffer.getvalue()


def filename_for(workflow_name: str) -> str:
    return f"signalops-{slug(workflow_name)}.zip"
