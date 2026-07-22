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

import time  # noqa: E402

from agents import export as agent_export  # noqa: E402
from agents.catalogue import ALLOWED_MODELS, CATALOGUE, Tier  # noqa: E402
from agents.catalogue import get as catalogue_get  # noqa: E402
from agents.guard import TOOL_TIERS, GuardrailViolation, resolve  # noqa: E402
from auth import (SESSION_COOKIE, Principal, current_principal, issue_session,  # noqa: E402
                  provider, record_login, require_role)
from db import audit, audit_entries, init_db, session_scope  # noqa: E402
from events import Event, bus  # noqa: E402
from models import AgentConfig, Role, Workspace  # noqa: E402

# Which tools sit at each tier — shown in the UI so the envelope is legible.
TOOL_TIERS_BY_TIER: dict[str, list[str]] = {}
for _tool, _tier in TOOL_TIERS.items():
    TOOL_TIERS_BY_TIER.setdefault(_tier.value, []).append(_tool)

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
    with session_scope() as session:
        workspace = session.get(Workspace, principal.workspace_id)
        workspace.killswitch = payload.enabled
        audit(session, actor=principal.user.display_name,
              action="killswitch_enabled" if payload.enabled else "killswitch_disabled",
              entity_type="workspace", entity_id=workspace.id,
              workspace_id=workspace.id, actor_verified=principal.user.identity_verified,
              detail={"reason": payload.reason})
    return {"killswitch": payload.enabled}


# --- agent catalogue ---------------------------------------------------------

class AgentConfigRequest(BaseModel):
    """Only the customisable fields.

    There is deliberately no `tools` or `tier` here. Rejecting them would be
    weaker than not accepting them: a field that does not exist cannot be
    forgotten in a validator.
    """
    model: str | None = None
    # Full replacement for the task instructions. The safety preamble is
    # prepended in code and is not reachable from here.
    custom_prompt: str | None = Field(default=None, max_length=8000)
    extra_guidance: str | None = Field(default=None, max_length=4000)
    confidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    requires_approval: bool | None = None
    enabled: bool | None = None


def _agent_view(spec, config, resolved) -> dict:
    return {
        "id": spec.id, "name": spec.name, "purpose": spec.purpose,
        "explanation": spec.explanation, "workflow": spec.workflow,
        "tier": spec.tier.value, "tools": list(spec.tools),
        "output_schema": spec.output_schema,
        "produces_confidence": spec.produces_confidence,
        "advisory_only": spec.advisory_only,
        "optional": spec.optional,
        "disabled_effect": spec.disabled_effect,
        "tags": list(spec.tags),
        "default_model": spec.default_model,
        "allowed_models": ALLOWED_MODELS,
        # Effective values after customisation, so the UI shows what will run.
        "model": resolved.model,
        "confidence_threshold": resolved.confidence_threshold,
        "requires_approval": resolved.requires_approval,
        "enabled": resolved.enabled,
        "extra_guidance": getattr(config, "extra_guidance", None),
        "custom_prompt": getattr(config, "custom_prompt", None),
        # The shipped task prompt, so the editor can show what it is replacing
        # and offer a revert.
        "default_prompt": spec.system_prompt,
        "customised": config is not None,
    }


@app.get("/api/agents")
async def list_agents(principal: Principal = Depends(require_role(Role.viewer))) -> dict:
    with session_scope() as session:
        configs = {c.agent_id: c for c in session.query(AgentConfig)
                   .filter(AgentConfig.workspace_id == principal.workspace_id).all()}
        agents = []
        for spec in CATALOGUE:
            config = configs.get(spec.id)
            agents.append(_agent_view(spec, config, resolve(spec, config)))
    return {"agents": agents, "tiers": {t.value: TOOL_TIERS_BY_TIER.get(t.value, [])
                                        for t in Tier}}


@app.put("/api/agents/{agent_id}")
async def update_agent(agent_id: str, payload: AgentConfigRequest,
                       principal: Principal = Depends(require_role(Role.admin))) -> dict:
    spec = catalogue_get(agent_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="unknown agent")
    with session_scope() as session:
        config = (session.query(AgentConfig)
                  .filter(AgentConfig.workspace_id == principal.workspace_id,
                          AgentConfig.agent_id == agent_id).one_or_none())
        if config is None:
            config = AgentConfig(workspace_id=principal.workspace_id, agent_id=agent_id)
            session.add(config)
        for attribute, value in payload.model_dump(exclude_unset=True).items():
            setattr(config, attribute, value)
        config.updated_at = time.time()
        config.updated_by = principal.user.id
        try:
            # Resolve before committing: an invalid customisation must not be
            # stored and then rejected later at run time.
            resolved = resolve(spec, config)
        except GuardrailViolation as violation:
            session.rollback()
            audit(session, actor=principal.user.display_name, action="agent_customise_rejected",
                  entity_type="agent", entity_id=agent_id, workspace_id=principal.workspace_id,
                  actor_verified=principal.user.identity_verified,
                  detail={"reason": str(violation)})
            session.commit()
            raise HTTPException(status_code=422, detail=str(violation))
        audit(session, actor=principal.user.display_name, action="agent_customised",
              entity_type="agent", entity_id=agent_id, workspace_id=principal.workspace_id,
              actor_verified=principal.user.identity_verified,
              detail=payload.model_dump(exclude_unset=True))
        view = _agent_view(spec, config, resolved)
    return view


@app.post("/api/agents/{agent_id}/reset")
async def reset_agent(agent_id: str,
                      principal: Principal = Depends(require_role(Role.admin))) -> dict:
    if catalogue_get(agent_id) is None:
        raise HTTPException(status_code=404, detail="unknown agent")
    with session_scope() as session:
        deleted = (session.query(AgentConfig)
                   .filter(AgentConfig.workspace_id == principal.workspace_id,
                           AgentConfig.agent_id == agent_id).delete())
        if deleted:
            audit(session, actor=principal.user.display_name, action="agent_reset",
                  entity_type="agent", entity_id=agent_id,
                  workspace_id=principal.workspace_id,
                  actor_verified=principal.user.identity_verified)
    return {"status": "reset" if deleted else "unchanged", "id": agent_id}


@app.get("/api/agents/{agent_id}/prompt")
async def agent_prompt(agent_id: str,
                       principal: Principal = Depends(require_role(Role.admin))) -> dict:
    """The exact prompt this agent would run with.

    Shown so customisation is inspectable rather than a black box — you can see
    where your guidance lands relative to the safety rules.
    """
    spec = catalogue_get(agent_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="unknown agent")
    with session_scope() as session:
        config = (session.query(AgentConfig)
                  .filter(AgentConfig.workspace_id == principal.workspace_id,
                          AgentConfig.agent_id == agent_id).one_or_none())
        resolved = resolve(spec, config)
    return {"id": agent_id, "model": resolved.model, "tools": list(resolved.tools),
            "tier": resolved.tier.value, "system_prompt": resolved.system_prompt}


def _workspace_agent_configs(session, workspace_id: str) -> dict:
    return {c.agent_id: c for c in session.query(AgentConfig)
            .filter(AgentConfig.workspace_id == workspace_id).all()}


@app.get("/api/agents/{agent_id}/export")
async def export_agent(agent_id: str,
                       principal: Principal = Depends(require_role(Role.viewer))) -> Response:
    """One agent as a Claude subagent definition file — lift and shift."""
    spec = catalogue_get(agent_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="unknown agent")
    with session_scope() as session:
        config = _workspace_agent_configs(session, principal.workspace_id).get(agent_id)
        markdown = agent_export.to_markdown(spec, resolve(spec, config))
    return Response(
        content=markdown, media_type="text/markdown",
        headers={"Content-Disposition":
                 f'attachment; filename="{agent_export.filename_for(spec)}"'})


@app.get("/api/agents/export/bundle")
async def export_agents(principal: Principal = Depends(require_role(Role.viewer))) -> Response:
    """Every agent plus a README, as a zip."""
    with session_scope() as session:
        archive = agent_export.bundle(_workspace_agent_configs(session, principal.workspace_id))
    return Response(
        content=archive, media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="signalops-agents.zip"'})


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
