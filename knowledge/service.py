from __future__ import annotations
import re
from pathlib import Path
KB_DIR = Path(__file__).resolve().parent / "approved"
def _tokens(text): return {w for w in re.findall(r"[a-z0-9_]+", text.lower()) if len(w) > 2}
def search(query: str, threshold: float = .55):
    wanted, matches = _tokens(query), []
    if not wanted: return matches
    for path in KB_DIR.glob("*.md"):
        body = path.read_text(encoding="utf-8"); score = len(wanted & _tokens(body)) / len(wanted)
        if score >= threshold: matches.append({"title": path.stem, "score": round(score, 3), "content": body})
    return sorted(matches, key=lambda item: item["score"], reverse=True)
def draft_from_incident(i):
    d, r = i.get("diagnosis_json") or {}, i.get("report_json") or {}
    return f"# {r.get('title', i.get('title'))}\n\n## Symptoms\n{i.get('object_name')} - {d.get('evidence', [])}\n\n## Root cause\n{d.get('root_cause_hypothesis', 'To be confirmed')}\n\n## Resolution\nReplace with the human-confirmed resolution before approval.\n\n## Validation\n{r.get('markdown_report', '')}\n"
