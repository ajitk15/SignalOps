"""SignalOps v2 — agentic workflow platform.

Phase 0b: data model, dummy authentication behind a real authorisation model,
and the application shell. Workflows, agents and the engine follow.
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

from fastapi import Depends, FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
sys.path.insert(0, str(PROJECT_ROOT))

from auth import (SESSION_COOKIE, Principal, current_principal, issue_session,  # noqa: E402
                  provider, record_login, require_role)
from db import audit_entries, init_db, session_scope  # noqa: E402
from events import Event, bus  # noqa: E402
from models import Role, Workspace  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("signalops")

# The login is a stub. This tripwire exists so a build with no real
# authentication cannot quietly be deployed somewhere shared.
ENV = os.getenv("SIGNALOPS_ENV", "local").lower()
if ENV != "local" and not provider().verifies_identity:
    raise RuntimeError(
        f"SIGNALOPS_ENV={ENV!r} but the {provider().name!r} auth provider does not verify "
        "identity. Refusing to start until a real AuthProvider is configured."
    )

app = FastAPI(title="SignalOps")
DASHBOARD_DIR = PROJECT_ROOT / "dashboard"
STATIC_FILES = {"app.css", "app.js"}
WORKSPACE_ID = init_db()


# --- shell -------------------------------------------------------------------

@app.get("/")
async def index() -> FileResponse:
    # no-cache: revalidate every load so a shipped change is never masked by a
    # stale copy.
    return FileResponse(DASHBOARD_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/static/{filename}")
async def static_asset(filename: str) -> FileResponse:
    if filename not in STATIC_FILES:
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(DASHBOARD_DIR / filename, headers={"Cache-Control": "no-cache"})


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "env": ENV, "phase": "0b",
            "auth_provider": provider().name,
            "identity_verified": provider().verifies_identity}


# --- authentication ----------------------------------------------------------

class LoginRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=80)
    role: Role = Role.operator


@app.post("/api/auth/login")
async def login(payload: LoginRequest, response: Response) -> dict:
    """Dummy login: whoever you say you are, with the role you pick.

    Deliberately trivial — see auth.DummyAuthProvider. The session, roles and
    audit trail it produces are real.
    """
    with session_scope() as session:
        user = provider().login(session, WORKSPACE_ID,
                                display_name=payload.display_name, role=payload.role)
        record_login(session, user, WORKSPACE_ID)
        workspace = session.get(Workspace, WORKSPACE_ID)
        principal = Principal(user, workspace)
        token = issue_session(user)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")
    return principal.as_dict()


@app.post("/api/auth/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE)
    return {"status": "logged out"}


@app.get("/api/auth/me")
async def me(principal: Principal = Depends(current_principal)) -> dict:
    return principal.as_dict()


# --- workspace controls ------------------------------------------------------

class KillswitchRequest(BaseModel):
    enabled: bool
    reason: str | None = Field(default=None, max_length=500)


@app.post("/api/workspace/killswitch")
async def set_killswitch(payload: KillswitchRequest,
                         principal: Principal = Depends(require_role(Role.admin))) -> dict:
    """Global stop for the workspace. Admin only, and audited.

    Exists before the engine does on purpose: the control that halts everything
    should not be an afterthought added once there is something to halt.
    """
    from db import audit
    with session_scope() as session:
        workspace = session.get(Workspace, principal.workspace_id)
        workspace.killswitch = payload.enabled
        audit(session, actor=principal.user.display_name,
              action="killswitch_enabled" if payload.enabled else "killswitch_disabled",
              entity_type="workspace", entity_id=workspace.id,
              workspace_id=workspace.id, actor_verified=principal.user.identity_verified,
              detail={"reason": payload.reason})
    return {"killswitch": payload.enabled}


# --- audit -------------------------------------------------------------------

@app.get("/api/audit")
async def api_audit(limit: int = 100,
                    principal: Principal = Depends(require_role(Role.viewer))) -> dict:
    with session_scope() as session:
        entries = audit_entries(session, workspace_id=principal.workspace_id,
                                limit=min(limit, 500))
    return {"entries": entries,
            # Honest signal: with the dummy provider, actor is a claim.
            "actor_verified": provider().verifies_identity}


# --- live events -------------------------------------------------------------

@app.websocket("/ws/events")
async def ws_events(websocket: WebSocket) -> None:
    await websocket.accept()
    queue = bus.subscribe()
    try:
        await websocket.send_json(Event("hello", {"phase": "0b"}).to_dict())
        while True:
            event = await queue.get()
            await websocket.send_json(event.to_dict())
    except WebSocketDisconnect:
        pass
    finally:
        bus.unsubscribe(queue)
