"""Export agents in the Claude subagent definition format.

Lift-and-shift: an exported agent is a self-contained markdown file with YAML
frontmatter — the same shape Claude Code reads from `.claude/agents/*.md`. Drop
one into another project and it runs there, carrying its resolved model, its
tool allowlist and the full composed prompt including the safety preamble.

Two deliberate choices:

- The exported prompt is the **resolved** one, safety preamble included. An
  export that quietly dropped the injection defences would be a footgun the
  moment someone ran it somewhere without this platform around it.
- `tools` are exported as this platform's names, with the risk tier recorded
  alongside. They are SignalOps tool identifiers, not Claude Code built-ins, so
  the importing side must map them — stating that in the file beats emitting
  names that look native and silently do nothing.
"""
from __future__ import annotations

import io
import zipfile
from datetime import datetime, timezone

from agents.catalogue import CATALOGUE, AgentSpec
from agents.guard import ResolvedAgent, resolve

# Claude Code's frontmatter takes a short model alias.
MODEL_ALIASES = {
    "claude-opus-4-8": "opus",
    "claude-sonnet-5": "sonnet",
    "claude-haiku-4-5": "haiku",
}


def _yaml_scalar(value: str) -> str:
    """Quote a frontmatter value when it could otherwise break the YAML."""
    if any(ch in value for ch in ":#\"'\n") or value.strip() != value:
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace("\n", " ") + '"'
    return value


def to_markdown(spec: AgentSpec, resolved: ResolvedAgent) -> str:
    """Render one agent as a Claude subagent definition file."""
    frontmatter = [
        "---",
        f"name: {spec.id.replace('_', '-')}",
        f"description: {_yaml_scalar(spec.purpose + ' ' + spec.explanation.split('.')[0] + '.')}",
    ]
    if resolved.tools:
        frontmatter.append(f"tools: {', '.join(resolved.tools)}")
    alias = MODEL_ALIASES.get(resolved.model)
    if alias:
        frontmatter.append(f"model: {alias}")
    frontmatter.append("---")

    exported_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = [
        "",
        resolved.system_prompt.strip(),
        "",
        "---",
        "",
        "## Provenance",
        "",
        f"- Exported from SignalOps on {exported_at}",
        f"- Source agent: `{spec.id}` (workflow: {spec.workflow})",
        f"- Exact model: `{resolved.model}`"
        + (f" (frontmatter alias `{alias}`)" if alias else ""),
        f"- Risk tier: `{resolved.tier.value}`"
        + ("" if resolved.tools else " — this agent is granted no tools"),
    ]
    if resolved.tools:
        body.append(
            f"- Tools `{', '.join(resolved.tools)}` are SignalOps identifiers, not Claude "
            "Code built-ins. Map them to equivalents in the target environment; do not "
            "assume they resolve automatically."
        )
    body += [
        "- The safety preamble above is part of the prompt on purpose. Keep it: it is "
        "what makes ticket and code content data rather than instructions.",
        "",
        "## Expected output",
        "",
        "```json",
        _schema_block(spec),
        "```",
        "",
    ]
    return "\n".join(frontmatter + body)


def _schema_block(spec: AgentSpec) -> str:
    lines = ["{"]
    items = list(spec.output_schema.items())
    for index, (key, kind) in enumerate(items):
        comma = "," if index < len(items) - 1 else ""
        lines.append(f'  "{key}": "{kind}"{comma}')
    lines.append("}")
    return "\n".join(lines)


def filename_for(spec: AgentSpec) -> str:
    return f"{spec.id.replace('_', '-')}.md"


def bundle(configs: dict) -> bytes:
    """Zip every agent, plus a README explaining what the archive is."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for spec in CATALOGUE:
            resolved = resolve(spec, configs.get(spec.id))
            archive.writestr(f"agents/{filename_for(spec)}", to_markdown(spec, resolved))
        archive.writestr("agents/README.md", _bundle_readme(configs))
    return buffer.getvalue()


def _bundle_readme(configs: dict) -> str:
    exported_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    for spec in CATALOGUE:
        resolved = resolve(spec, configs.get(spec.id))
        customised = "yes" if spec.id in configs else "no"
        rows.append(f"| {spec.id} | {resolved.model} | {resolved.tier.value} | "
                    f"{', '.join(resolved.tools) or '—'} | {customised} |")
    return "\n".join([
        "# SignalOps agents",
        "",
        f"Exported {exported_at}. One markdown file per agent, in the Claude subagent",
        "definition format (YAML frontmatter + system prompt).",
        "",
        "## Using these elsewhere",
        "",
        "Copy the `.md` files into a target project's `.claude/agents/` directory. Each",
        "file is self-contained — model, tools and the full prompt travel with it.",
        "",
        "Two things do **not** travel, and matter:",
        "",
        "1. **Tool names are SignalOps identifiers.** `servicenow_read`, `repo_write` and",
        "   friends mean something here; in another environment they must be mapped to",
        "   real tools or removed. A frontmatter tool that does not exist is silently",
        "   unavailable, not an error.",
        "2. **Tier enforcement is a SignalOps feature.** The `Risk tier` line in each file",
        "   is documentation once exported. Whatever runs these agents is responsible for",
        "   not granting an agent more reach than its tier states.",
        "",
        "## Contents",
        "",
        "| Agent | Model | Tier | Tools | Customised |",
        "|---|---|---|---|---|",
        *rows,
        "",
    ])
