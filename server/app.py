"""SignalOps v2 — agentic workflow platform.

Phase 0a skeleton: the monitoring pipeline has been removed and this is the
minimal server that still boots and serves the shell. The data model, auth and
the real API arrive in Phase 0b.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Windows asyncio needs the Proactor loop for subprocess support.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
sys.path.insert(0, str(PROJECT_ROOT))

import store  # noqa: E402
from events import Event, bus  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("signalops")

# The login is a stub (see the plan's security note). This tripwire exists so a
# build with no real authentication cannot quietly be deployed somewhere shared.
ENV = os.getenv("SIGNALOPS_ENV", "local").lower()
if ENV != "local":
    raise RuntimeError(
        f"SIGNALOPS_ENV={ENV!r} but authentication is still the dummy provider. "
        "Refusing to start outside 'local' until a real AuthProvider is configured."
    )

app = FastAPI(title="SignalOps")
DASHBOARD_DIR = PROJECT_ROOT / "dashboard"
STATIC_FILES = {"app.css", "app.js"}


@app.get("/")
async def index() -> FileResponse:
    # no-cache: revalidate every load so a shipped change is never masked by a
    # stale copy. See the earlier caching incident.
    return FileResponse(DASHBOARD_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/static/{filename}")
async def static_asset(filename: str) -> FileResponse:
    if filename not in STATIC_FILES:
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(DASHBOARD_DIR / filename, headers={"Cache-Control": "no-cache"})


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "env": ENV, "phase": "0a", "auth": "dummy (not yet implemented)"}


@app.get("/api/audit")
async def api_audit(limit: int = 100) -> dict:
    # Carried over intact: the audit trail predates the redesign and outlives it.
    # actor_verified stays false while the login is a stub.
    return {"entries": store.audit_entries(limit=min(limit, 500)), "actor_verified": False}


@app.websocket("/ws/events")
async def ws_events(websocket: WebSocket) -> None:
    await websocket.accept()
    queue = bus.subscribe()
    try:
        await websocket.send_json(Event("hello", {"phase": "0a"}).to_dict())
        while True:
            event = await queue.get()
            await websocket.send_json(event.to_dict())
    except WebSocketDisconnect:
        pass
    finally:
        bus.unsubscribe(queue)
