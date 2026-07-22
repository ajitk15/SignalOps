"""FastAPI server for enterprise collection, live dashboard events, and incidents."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from dotenv import load_dotenv

# Windows asyncio requires ProactorEventLoop to support subprocess operations
# (which the Claude Agent SDK needs to spawn the bundled CLI).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from fastapi.responses import FileResponse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
sys.path.insert(0, str(PROJECT_ROOT))

import store  # noqa: E402
from events import Event, bus  # noqa: E402
from enterprise_pipeline import EnterprisePipeline  # noqa: E402
from detection import Observation  # noqa: E402
from knowledge.service import draft_from_incident, search as search_kb  # noqa: E402
from collector_loop import collect_forever, collector_health  # noqa: E402
from agents.common import load_watchlist  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

enterprise_pipeline = EnterprisePipeline(
    use_ai=os.getenv("ENABLE_INCIDENT_AI", "false").lower() == "true"
)
try:
    DASHBOARD_TITLE = load_watchlist().dashboard_title
except Exception:  # a broken watchlist must not stop the server from starting
    DASHBOARD_TITLE = "Incident Triage Pipeline"
_enterprise_collect_task: asyncio.Task | None = None
_servicenow_task: asyncio.Task | None = None


def _log_collector_exit(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    logging.getLogger("collector").error(
        "collection task exited unexpectedly — polling has stopped", exc_info=task.exception())


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _enterprise_collect_task, _servicenow_task
    if os.getenv("ENABLE_MQ_ACE_COLLECTOR", "false").lower() == "true":
        _enterprise_collect_task = asyncio.create_task(collect_forever(enterprise_pipeline))
        # collect_forever is meant to be immortal; if it ever returns or raises,
        # say so rather than letting polling stop with nothing in the log.
        _enterprise_collect_task.add_done_callback(_log_collector_exit)
    from integrations.servicenow import deliver_forever
    _servicenow_task = asyncio.create_task(deliver_forever())
    yield
    if _enterprise_collect_task:
        _enterprise_collect_task.cancel()
    if _servicenow_task:
        _servicenow_task.cancel()


app = FastAPI(lifespan=lifespan)

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"


@app.get("/")
async def index() -> FileResponse:
    # "no-cache" means revalidate on every load, not "don't store". Without it
    # browsers apply heuristic freshness and keep running a stale copy of the
    # dashboard for minutes after it changes. FileResponse sets an ETag but does
    # not answer conditional requests, so each load re-sends the body — fine at
    # this size, and correctness beats saving 36KB.
    return FileResponse(DASHBOARD_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/incidents")
async def api_list_incidents(limit: int = 50) -> list[dict]:
    return store.list_incidents(limit=limit)


# --- rules management --------------------------------------------------------
# Built-in rules (config/rules.yaml) carry the behavioural-equivalence
# guarantee and stay file-managed/read-only. The UI owns rules.custom.yaml,
# which loads AFTER built-ins so first-match-wins means custom rules add
# detections without shadowing existing behaviour.

from detection import CUSTOM_RULES_PATH, RULES_PATH  # noqa: E402
import yaml  # noqa: E402

RULE_TEMPLATES_PATH = PROJECT_ROOT / "config" / "rule_templates.yaml"
_KNOWN_CONDITIONS = {"greater_than", "not_in", "rising"}


class RuleRequest(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    metric: str = Field(pattern=r"^[a-z0-9_]{1,64}$")
    condition: dict
    # P1-P4 fixes the severity (AI cannot override it), "ai" delegates the
    # decision to the Diagnostician, and omitting it falls back to P3.
    severity: str | None = Field(default=None, pattern=r"^(P[1-4]|ai)$")
    ai_provisional: str | None = Field(default=None, pattern=r"^P[1-4]$")
    message: str = Field(min_length=3, max_length=200)
    escalate: dict | None = None


def _load_custom_rules() -> list[dict]:
    if not CUSTOM_RULES_PATH.exists():
        return []
    return (yaml.safe_load(CUSTOM_RULES_PATH.read_text(encoding="utf-8")) or {}).get("rules", [])


def _write_custom_rules(rules: list[dict]) -> None:
    CUSTOM_RULES_PATH.write_text(
        "# Custom rules created from the SignalOps dashboard. Loaded after the\n"
        "# built-in rules in config/rules.yaml (first match wins).\n"
        + yaml.safe_dump({"rules": rules}, sort_keys=False, allow_unicode=True),
        encoding="utf-8")


def _builtin_rules() -> list[dict]:
    return yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))["rules"]


def _apply_custom(custom: list[dict], previous: list[dict]) -> None:
    """Persist and reload, rolling back if the engine rejects the result."""
    _write_custom_rules(custom)
    try:
        enterprise_pipeline.reload_rules()
    except Exception as exc:
        _write_custom_rules(previous)
        enterprise_pipeline.reload_rules()
        raise HTTPException(status_code=422, detail=f"rule rejected by the engine: {exc}")


def _rule_from(payload: RuleRequest) -> dict:
    condition_type = payload.condition.get("type")
    if condition_type not in _KNOWN_CONDITIONS:
        raise HTTPException(status_code=422, detail=f"condition.type must be one of {sorted(_KNOWN_CONDITIONS)}")
    if condition_type == "not_in" and not payload.condition.get("values"):
        raise HTTPException(status_code=422, detail="not_in requires a values list")
    rule = {"id": payload.id, "when": {"metric": payload.metric},
            "condition": payload.condition, "message": payload.message}
    if payload.severity:
        rule["severity"] = payload.severity
    if payload.severity == "ai" and payload.ai_provisional:
        rule["ai_provisional"] = payload.ai_provisional
    if payload.escalate:
        rule["escalate"] = payload.escalate
    return rule


@app.get("/api/rules")
async def api_list_rules() -> dict:
    builtin = _builtin_rules()
    custom = _load_custom_rules()
    overrides = {rule["id"]: rule for rule in custom}
    builtin_view = []
    for rule in builtin:
        override = overrides.get(rule["id"])
        builtin_view.append({**(override or rule), "origin": "built-in",
                             "overridden": override is not None and not override.get("disabled"),
                             "disabled": bool(override and override.get("disabled"))})
    builtin_ids = {rule["id"] for rule in builtin}
    custom_view = [{**rule, "origin": "custom"} for rule in custom if rule["id"] not in builtin_ids]
    templates = yaml.safe_load(RULE_TEMPLATES_PATH.read_text(encoding="utf-8"))["categories"] \
        if RULE_TEMPLATES_PATH.exists() else []
    return {"builtin": builtin_view, "custom": custom_view, "templates": templates}


@app.post("/api/rules")
async def api_create_rule(payload: RuleRequest) -> dict:
    custom = _load_custom_rules()
    if payload.id in {r["id"] for r in _builtin_rules()} or any(r["id"] == payload.id for r in custom):
        raise HTTPException(status_code=409, detail="a rule with this id already exists")
    rule = _rule_from(payload)
    _apply_custom(custom + [rule], custom)
    store.audit(actor="dashboard", action="rule_created", entity_type="rule", entity_id=payload.id, detail=rule)
    return {"status": "created", "rule": rule}


@app.put("/api/rules/{rule_id}")
async def api_update_rule(rule_id: str, payload: RuleRequest) -> dict:
    """Edit a custom rule, or override a built-in one.

    Overriding writes the edited copy into rules.custom.yaml; the engine
    substitutes it for the shipped rule in place, so evaluation order — which
    is behaviour under first-match-wins — never shifts. The shipped file is
    left untouched, so reset restores it exactly.
    """
    if rule_id != payload.id:
        raise HTTPException(status_code=422, detail="rule id in the path and body must match")
    custom = _load_custom_rules()
    is_builtin = rule_id in {r["id"] for r in _builtin_rules()}
    if not is_builtin and not any(r["id"] == rule_id for r in custom):
        raise HTTPException(status_code=404, detail="rule not found")
    rule = _rule_from(payload)
    updated = [r for r in custom if r["id"] != rule_id] + [rule]
    _apply_custom(updated, custom)
    store.audit(actor="dashboard", action="rule_updated", entity_type="rule", entity_id=rule_id,
                detail={"rule": rule, "overrides_builtin": is_builtin})
    return {"status": "updated", "rule": rule, "overrides_builtin": is_builtin}


@app.delete("/api/rules/{rule_id}")
async def api_delete_rule(rule_id: str) -> dict:
    """Remove a custom rule, or disable a built-in one (reversible via reset)."""
    custom = _load_custom_rules()
    if rule_id in {r["id"] for r in _builtin_rules()}:
        updated = [r for r in custom if r["id"] != rule_id] + [{"id": rule_id, "disabled": True}]
        _apply_custom(updated, custom)
        store.audit(actor="dashboard", action="rule_disabled", entity_type="rule", entity_id=rule_id)
        return {"status": "disabled", "id": rule_id}
    remaining = [r for r in custom if r["id"] != rule_id]
    if len(remaining) == len(custom):
        raise HTTPException(status_code=404, detail="rule not found")
    _apply_custom(remaining, custom)
    store.audit(actor="dashboard", action="rule_deleted", entity_type="rule", entity_id=rule_id)
    return {"status": "deleted", "id": rule_id}


@app.post("/api/rules/{rule_id}/reset")
async def api_reset_rule(rule_id: str) -> dict:
    """Drop any override/disable for a built-in rule, restoring the shipped one."""
    if rule_id not in {r["id"] for r in _builtin_rules()}:
        raise HTTPException(status_code=404, detail="only built-in rules can be reset")
    custom = _load_custom_rules()
    remaining = [r for r in custom if r["id"] != rule_id]
    if len(remaining) == len(custom):
        return {"status": "unchanged", "id": rule_id}
    _apply_custom(remaining, custom)
    store.audit(actor="dashboard", action="rule_reset", entity_type="rule", entity_id=rule_id)
    return {"status": "reset", "id": rule_id}


class IncidentPatch(BaseModel):
    status: str = Field(pattern=r"^(open|acknowledged|resolved|closed|false_positive)$")
    # Self-asserted: with no authentication in front of this API, the audit log
    # records a claimed actor, not a verified identity.
    actor: str = Field(default="dashboard", min_length=1, max_length=80)
    note: str | None = Field(default=None, max_length=2000)
    assignee: str | None = Field(default=None, max_length=80)


@app.patch("/api/incidents/{incident_id}")
async def api_update_incident(incident_id: int, payload: IncidentPatch) -> dict:
    incident = store.get_incident(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident not found")
    updated = store.set_incident_status(incident_id, payload.status, actor=payload.actor,
                                        note=payload.note, assignee=payload.assignee)
    # Terminal means a human is finished: drop the dedup memory so the same
    # condition recurring is never silently swallowed as a duplicate.
    if payload.status in store.TERMINAL_STATUSES and updated.get("fingerprint"):
        enterprise_pipeline.correlator.forget(updated["fingerprint"])
    snow_result = await asyncio.to_thread(snow.push_incident_state, updated, payload.status) \
        if payload.status in store.TERMINAL_STATUSES else None
    bus.publish(Event("incident_updated", {"incident_id": incident_id, "status": payload.status,
                                           "severity": updated.get("severity"),
                                           "title": updated.get("title"),
                                           "assignee": updated.get("assignee")}))
    return {"status": "updated", "incident": updated, "servicenow": snow_result}


@app.get("/api/audit")
async def api_audit(limit: int = 100, entity_type: str | None = None,
                    entity_id: str | None = None) -> dict:
    return {"entries": store.audit_entries(entity_type, entity_id, limit=min(limit, 500)),
            # Surfaced in the UI: an audit trail without authn records claims.
            "actor_verified": False}


@app.get("/api/metrics")
async def api_metrics() -> dict:
    return store.incident_metrics()


@app.get("/api/incidents/{incident_id}")
async def api_get_incident(incident_id: int) -> dict:
    incident = store.get_incident(incident_id)
    if incident is None:
        return {"error": "not found"}
    return incident | {"audit": store.audit_entries("incident", str(incident_id), limit=50)}


class ObservationRequest(BaseModel):
    # Open contract: any collector or webhook may post, constrained by charset
    # and length rather than a closed source list. object_name must accept the
    # names this system itself produces (e.g. MQNODE1/QL.INPUT) — uppercase,
    # dots and slashes included. Defence in depth behind the output escaping.
    source: str = Field(pattern=r"^[a-z0-9_.\-]{1,64}$")
    object_type: str = Field(pattern=r"^[A-Za-z0-9_.\-]{1,64}$")
    object_name: str = Field(pattern=r"^[A-Za-z0-9_.:/\-]{1,128}$")
    metric: str = Field(pattern=r"^[a-z0-9_]{1,64}$")
    value: str | float | int
    labels: dict[str, str] = Field(default_factory=dict)
    threshold: float | None = None


class KbApprovalRequest(BaseModel):
    markdown: str = Field(min_length=20)
    approved_by: str = Field(min_length=1, max_length=100)


class KbArticleUpdateRequest(BaseModel):
    markdown: str = Field(min_length=20)
    edited_by: str = Field(min_length=1, max_length=100)


@app.post("/api/observations")
async def api_ingest_observation(payload: ObservationRequest) -> dict:
    """Common ingestion boundary for MCP collectors and monitoring webhooks."""
    return await enterprise_pipeline.ingest(Observation(**payload.model_dump()))


@app.get("/api/incidents/{incident_id}/kb-draft")
async def api_kb_draft(incident_id: int) -> dict:
    incident = store.get_incident(incident_id)
    if incident is None:
        return {"error": "not found"}
    return {"status": "draft_requires_human_approval", "markdown": draft_from_incident(incident)}


@app.get("/api/incidents/{incident_id}/kb-articles")
async def api_incident_kb_articles(incident_id: int) -> list[dict]:
    """Return approved KB articles relevant to an incident for dashboard review."""
    incident = store.get_incident(incident_id)
    if incident is None:
        return []
    watcher = incident.get("watcher_json") or {}
    query = " ".join(filter(None, [watcher.get("reason"), incident.get("object_name"), incident.get("title")]))
    return search_kb(query, threshold=0.15)


@app.post("/api/incidents/{incident_id}/kb-approve")
async def api_approve_kb_article(incident_id: int, payload: KbApprovalRequest) -> dict:
    """Publish a human-reviewed KB article without permitting arbitrary paths."""
    if store.get_incident(incident_id) is None:
        raise HTTPException(status_code=404, detail="incident not found")
    title_match = re.search(r"^#\s+(.+)$", payload.markdown, flags=re.MULTILINE)
    title = title_match.group(1).strip() if title_match else f"Incident {incident_id} runbook"
    filename = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or f"incident-{incident_id}-runbook"
    kb_dir = PROJECT_ROOT / "knowledge" / "approved"
    kb_dir.mkdir(parents=True, exist_ok=True)
    path = kb_dir / f"{filename}.md"
    if path.exists():
        raise HTTPException(status_code=409, detail="an approved article with this title already exists")
    reviewed = f"<!-- Approved by: {payload.approved_by.strip()} · Incident: #{incident_id} -->\n\n{payload.markdown.strip()}\n"
    path.write_text(reviewed, encoding="utf-8")
    store.audit(actor=payload.approved_by, action="kb_approved", entity_type="kb_article",
                entity_id=path.stem, detail={"title": title, "incident_id": incident_id})
    return {"status": "approved", "title": title, "filename": path.name}


def _approved_kb_path(slug: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
        raise HTTPException(status_code=404, detail="article not found")
    return PROJECT_ROOT / "knowledge" / "approved" / f"{slug}.md"


@app.get("/api/kb-articles")
async def api_list_kb_articles() -> list[dict]:
    kb_dir = PROJECT_ROOT / "knowledge" / "approved"
    if not kb_dir.exists():
        return []
    kb_refs = snow.load_kb_refs()
    articles = []
    for path in sorted(kb_dir.glob("*.md"), key=lambda item: item.stat().st_mtime, reverse=True):
        content = path.read_text(encoding="utf-8")
        title_match = re.search(r"^#\s+(.+)$", content, flags=re.MULTILINE)
        articles.append({"slug": path.stem, "title": title_match.group(1).strip() if title_match else path.stem,
                         "content": content, "updated_at": path.stat().st_mtime,
                         "servicenow": kb_refs.get(path.stem)})
    return articles


@app.put("/api/kb-articles/{slug}")
async def api_update_kb_article(slug: str, payload: KbArticleUpdateRequest) -> dict:
    path = _approved_kb_path(slug)
    if not path.exists():
        raise HTTPException(status_code=404, detail="article not found")
    reviewed = f"<!-- Last edited by: {payload.edited_by.strip()} -->\n\n{payload.markdown.strip()}\n"
    path.write_text(reviewed, encoding="utf-8")
    store.audit(actor=payload.edited_by, action="kb_edited", entity_type="kb_article", entity_id=slug)
    return {"status": "updated", "filename": path.name}


@app.delete("/api/kb-articles/{slug}")
async def api_delete_kb_article(slug: str) -> dict:
    path = _approved_kb_path(slug)
    if not path.exists():
        raise HTTPException(status_code=404, detail="article not found")
    path.unlink()
    store.audit(actor="dashboard", action="kb_deleted", entity_type="kb_article", entity_id=slug)
    return {"status": "deleted", "filename": path.name}


# --- integrations status + connection tests ---------------------------------
# Status never includes credential values — only which env var NAMES are set.

from integrations import context as ctx  # noqa: E402
from integrations import servicenow as snow  # noqa: E402
from integrations.mq_ace_mcp import MqAceMcpCollector  # noqa: E402

_INTEGRATIONS = {
    "mq_mcp": {"name": "IBM MQ / ACE MCP", "purpose": "Live queue, channel and flow collection (read-only).",
               "env": ["MQ_MCP_URL", "MQ_MCP_AUTH_USER", "MQ_MCP_AUTH_PASSWORD", "MQ_MCP_TLS_CERT"],
               "configured": lambda: bool(os.getenv("MQ_MCP_URL"))},
    "splunk": {"name": "Splunk", "purpose": "Historical log context attached to new incidents.",
               "env": ["SPLUNK_BASE_URL", "SPLUNK_TOKEN"],
               "configured": lambda: bool(os.getenv("SPLUNK_BASE_URL") and os.getenv("SPLUNK_TOKEN"))},
    "dynatrace": {"name": "Dynatrace", "purpose": "Open problems on the affected service, attached as context.",
                  "env": ["DYNATRACE_BASE_URL", "DYNATRACE_TOKEN"],
                  "configured": lambda: bool(os.getenv("DYNATRACE_BASE_URL") and os.getenv("DYNATRACE_TOKEN"))},
    "servicenow": {"name": "ServiceNow", "purpose": "Creates an incident per new SignalOps incident and mirrors "
                                                    "approved KB articles into Knowledge (create/update/retire); "
                                                    "reads change requests for 'what changed?' context.",
                   "env": ["SN_INSTANCE_URL", "SN_READ_USER", "SN_READ_PASSWORD", "SN_WRITE_USER", "SN_WRITE_PASSWORD"],
                   "configured": lambda: bool(os.getenv("SN_INSTANCE_URL"))},
}


@app.get("/api/integrations")
async def api_integrations() -> list[dict]:
    result = []
    for key, spec in _INTEGRATIONS.items():
        entry = {"key": key, "name": spec["name"], "purpose": spec["purpose"],
                 "env": spec["env"], "configured": spec["configured"]()}
        if key == "servicenow":
            entry["mode"] = snow.mode()
        result.append(entry)
    return result


def _test_integration(key: str) -> None:
    if key == "splunk":
        splunk, _ = ctx.readers_from_env()
        if splunk is None: raise RuntimeError("not configured")
        ctx._get(f"{splunk.base_url}/services/server/info?output_mode=json", splunk.token, "Splunk")
    elif key == "dynatrace":
        _, dynatrace = ctx.readers_from_env()
        if dynatrace is None: raise RuntimeError("not configured")
        dynatrace.problems('type("SERVICE")', minutes=5)
    elif key == "servicenow":
        reader = snow.reader_from_env()
        if reader is None: raise RuntimeError("not configured (read credentials missing)")
        reader.test()
    else:
        raise RuntimeError("unknown integration")


@app.post("/api/integrations/{key}/test")
async def api_test_integration(key: str) -> dict:
    if key not in _INTEGRATIONS:
        raise HTTPException(status_code=404, detail="unknown integration")
    try:
        if key == "mq_mcp":
            await MqAceMcpCollector().health()
        else:
            await asyncio.to_thread(_test_integration, key)
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@app.websocket("/ws/events")
async def ws_events(websocket: WebSocket) -> None:
    await websocket.accept()
    queue = bus.subscribe()
    try:
        # Current state goes out on this same ordered channel, ahead of the
        # history replay, so a client can never race a parallel fetch against
        # the live events that supersede it. Constructed, not published — a
        # published snapshot would land in every other client's history.
        await websocket.send_json(Event("state_snapshot", {
            "title": DASHBOARD_TITLE,
            "watched_objects": enterprise_pipeline.watched_objects(),
            "collector": collector_health(),
            # Full stage roster, so the dashboard shows every agent even before
            # (or without) any agent event firing.
            "pipeline": {
                "ai_enabled": enterprise_pipeline.use_ai,
                "minimum_ai_severity": enterprise_pipeline.minimum_ai_severity,
                "kb_reuse_threshold": enterprise_pipeline.kb_reuse_threshold,
                "stages": [
                    {"name": "watcher", "label": "Collection & rules", "ai": False,
                     "role": "Collects observations from every configured source and evaluates "
                             "deterministic rules. Runs continuously and costs nothing."},
                    {"name": "diagnostician", "label": "Diagnostician", "ai": True,
                     "model": enterprise_pipeline.models.diagnostician,
                     "role": "Investigates an eligible new incident with read-only tools and "
                             "produces a root-cause hypothesis with confidence and severity."},
                    {"name": "report_writer", "label": "Report writer", "ai": True,
                     "model": enterprise_pipeline.models.report_writer,
                     "role": "Turns the diagnosis into a ticket-ready incident report. "
                             "Has no tools and takes no actions."},
                ],
            },
        }).to_dict())
        for event in bus.recent(50):
            await websocket.send_json(event.to_dict())
        while True:
            event = await queue.get()
            await websocket.send_json(event.to_dict())
    except WebSocketDisconnect:
        pass
    finally:
        bus.unsubscribe(queue)
