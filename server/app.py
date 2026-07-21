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
from fastapi.staticfiles import StaticFiles

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
sys.path.insert(0, str(PROJECT_ROOT))

import store  # noqa: E402
from events import bus  # noqa: E402
from enterprise_pipeline import EnterprisePipeline  # noqa: E402
from detection import Observation  # noqa: E402
from knowledge.service import draft_from_incident, search as search_kb  # noqa: E402
from collect_mq_ace import collect_forever  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

enterprise_pipeline = EnterprisePipeline(
    use_ai=os.getenv("ENABLE_INCIDENT_AI", "false").lower() == "true"
)
_enterprise_collect_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _enterprise_collect_task
    if os.getenv("ENABLE_MQ_ACE_COLLECTOR", "false").lower() == "true":
        _enterprise_collect_task = asyncio.create_task(collect_forever(enterprise_pipeline))
    yield
    if _enterprise_collect_task:
        _enterprise_collect_task.cancel()


app = FastAPI(lifespan=lifespan)

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"
app.mount("/static", StaticFiles(directory=DASHBOARD_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(DASHBOARD_DIR / "index.html")


@app.get("/api/incidents")
async def api_list_incidents(limit: int = 50) -> list[dict]:
    return store.list_incidents(limit=limit)


@app.get("/api/incidents/{incident_id}")
async def api_get_incident(incident_id: int) -> dict:
    incident = store.get_incident(incident_id)
    if incident is None:
        return {"error": "not found"}
    return incident


class ObservationRequest(BaseModel):
    source: str = Field(pattern="^(mq_mcp|ace_mcp|splunk|dynatrace)$")
    object_type: str
    object_name: str
    metric: str
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
    articles = []
    for path in sorted(kb_dir.glob("*.md"), key=lambda item: item.stat().st_mtime, reverse=True):
        content = path.read_text(encoding="utf-8")
        title_match = re.search(r"^#\s+(.+)$", content, flags=re.MULTILINE)
        articles.append({"slug": path.stem, "title": title_match.group(1).strip() if title_match else path.stem,
                         "content": content, "updated_at": path.stat().st_mtime})
    return articles


@app.put("/api/kb-articles/{slug}")
async def api_update_kb_article(slug: str, payload: KbArticleUpdateRequest) -> dict:
    path = _approved_kb_path(slug)
    if not path.exists():
        raise HTTPException(status_code=404, detail="article not found")
    reviewed = f"<!-- Last edited by: {payload.edited_by.strip()} -->\n\n{payload.markdown.strip()}\n"
    path.write_text(reviewed, encoding="utf-8")
    return {"status": "updated", "filename": path.name}


@app.delete("/api/kb-articles/{slug}")
async def api_delete_kb_article(slug: str) -> dict:
    path = _approved_kb_path(slug)
    if not path.exists():
        raise HTTPException(status_code=404, detail="article not found")
    path.unlink()
    return {"status": "deleted", "filename": path.name}


@app.websocket("/ws/events")
async def ws_events(websocket: WebSocket) -> None:
    await websocket.accept()
    queue = bus.subscribe()
    try:
        for event in bus.recent(50):
            await websocket.send_json(event.to_dict())
        while True:
            event = await queue.get()
            await websocket.send_json(event.to_dict())
    except WebSocketDisconnect:
        pass
    finally:
        bus.unsubscribe(queue)
