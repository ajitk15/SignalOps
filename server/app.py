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
from engine import workflow_export  # noqa: E402
from engine.approvals import StaleApproval  # noqa: E402
from engine.runtime import (TEMPLATES, DuplicateRun, EngineError,  # noqa: E402
                            engine)
from engine.poller import poller  # noqa: E402
from engine.state import Halted  # noqa: E402
from integrations import servicenow  # noqa: E402
from events import Event, bus  # noqa: E402
from models import (AgentConfig, Approval, ApprovalStatus, Connection,  # noqa: E402
                    Role, Run, RunStatus, RunStep, Workflow, Workspace)

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


@app.on_event("startup")
async def on_startup() -> None:
    # Runs execute on a thread pool; the bus needs to know which loop the
    # WebSocket subscribers live on before anything publishes from a worker.
    bus.bind_loop()
    resumed = engine().reconcile()
    if resumed:
        logger.info("resumed %d run(s) left in flight by the previous process", resumed)
    await poller.sync()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    poller.stop_all()
    engine().shutdown()


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "env": ENV, "phase": "3",
            "auth_provider": provider().name,
            "identity_verified": provider().verifies_identity,
            # Whether the engine will call a model or simulate. Surfaced so a
            # simulated deployment is visible without reading a log.
            "model_client": engine().client.name,
            "simulated": engine().client.simulated}


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


# --- workflows ---------------------------------------------------------------

class WorkflowRequest(BaseModel):
    template: str
    name: str = Field(min_length=1, max_length=120)
    dry_run: bool = True
    run_budget_usd: float = Field(default=1.0, gt=0, le=100)


def _workflow_view(workflow: Workflow) -> dict:
    return {"id": workflow.id, "template": workflow.template, "name": workflow.name,
            "config": workflow.config, "enabled": workflow.enabled,
            "dry_run_passed_at": workflow.dry_run_passed_at,
            "created_at": workflow.created_at,
            "exportable": workflow.template in workflow_export.TEMPLATES,
            "polling": bool(workflow.config.get("poll_enabled")),
            # The UI needs to explain *why* Enable is unavailable, not just
            # grey it out.
            "can_enable": bool(workflow.dry_run_passed_at)}


@app.get("/api/workflows")
async def list_workflows(principal: Principal = Depends(require_role(Role.viewer))) -> dict:
    with session_scope() as session:
        workflows = (session.query(Workflow)
                     .filter(Workflow.workspace_id == principal.workspace_id)
                     .order_by(Workflow.created_at.desc()).all())
        views = [_workflow_view(w) for w in workflows]
    return {"workflows": views,
            "templates": [{"id": key, "name": meta["name"]}
                          for key, meta in workflow_export.TEMPLATES.items()]}


@app.post("/api/workflows")
async def create_workflow(payload: WorkflowRequest,
                          principal: Principal = Depends(require_role(Role.admin))) -> dict:
    if payload.template not in TEMPLATES:
        raise HTTPException(status_code=422, detail="unknown template")
    with session_scope() as session:
        workflow = Workflow(workspace_id=principal.workspace_id, template=payload.template,
                            name=payload.name, created_by=principal.user.id,
                            config={"dry_run": payload.dry_run,
                                    "run_budget_usd": payload.run_budget_usd})
        session.add(workflow)
        session.flush()
        audit(session, actor=principal.user.display_name, action="workflow_created",
              entity_type="workflow", entity_id=workflow.id,
              workspace_id=principal.workspace_id,
              actor_verified=principal.user.identity_verified,
              detail={"template": payload.template, "name": payload.name})
        view = _workflow_view(workflow)
    return view


@app.get("/api/workflows/{workflow_id}/export")
async def export_workflow(workflow_id: str,
                          principal: Principal = Depends(require_role(Role.viewer))) -> Response:
    """The whole workflow as a standalone Python app — lift and shift.

    Includes the graph, the agents, a Dockerfile and a setup document. The
    README states what does not travel: audit, roles, tier enforcement,
    budgets and the kill switch are platform features, not graph features.
    """
    with session_scope() as session:
        workflow = session.get(Workflow, workflow_id)
        if workflow is None or workflow.workspace_id != principal.workspace_id:
            raise HTTPException(status_code=404, detail="unknown workflow")
        try:
            archive = workflow_export.bundle(
                template=workflow.template, workflow_name=workflow.name,
                agent_configs=_workspace_agent_configs(session, principal.workspace_id))
        except KeyError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        filename = workflow_export.filename_for(workflow.name)
        audit(session, actor=principal.user.display_name, action="workflow_exported",
              entity_type="workflow", entity_id=workflow_id,
              workspace_id=principal.workspace_id,
              actor_verified=principal.user.identity_verified,
              detail={"bytes": len(archive)})
    return Response(content=archive, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


class EnableRequest(BaseModel):
    enabled: bool
    poll_enabled: bool = False


@app.post("/api/workflows/{workflow_id}/enable")
async def enable_workflow(workflow_id: str, payload: EnableRequest,
                          principal: Principal = Depends(require_role(Role.admin))) -> dict:
    """Turn a workflow on — but not before a dry run has succeeded.

    The gate is server-side rather than a disabled button, because a disabled
    button is a suggestion. Onboarding's whole claim is that you see what a
    workflow *would* do before it can do anything, and that only holds if the
    enable path itself refuses.
    """
    with session_scope() as session:
        workflow = session.get(Workflow, workflow_id)
        if workflow is None or workflow.workspace_id != principal.workspace_id:
            raise HTTPException(status_code=404, detail="unknown workflow")
        if payload.enabled and not workflow.dry_run_passed_at:
            raise HTTPException(
                status_code=409,
                detail="This workflow has not completed a dry run yet. Run one first: "
                       "it executes against a real ticket and writes nothing.")
        workflow.enabled = payload.enabled
        workflow.config = {**workflow.config, "poll_enabled": payload.poll_enabled}
        audit(session, actor=principal.user.display_name,
              action="workflow_enabled" if payload.enabled else "workflow_disabled",
              entity_type="workflow", entity_id=workflow_id,
              workspace_id=principal.workspace_id,
              actor_verified=principal.user.identity_verified,
              detail={"polling": payload.poll_enabled})
        view = _workflow_view(workflow)
    await poller.sync()
    return view


class WorkflowConfigRequest(BaseModel):
    filter_query: str | None = Field(default=None, max_length=1000)
    poll_interval_seconds: int | None = Field(default=None, ge=30, le=3600)
    dry_run: bool | None = None
    run_budget_usd: float | None = Field(default=None, gt=0, le=100)
    # Code workflows. The repository is configuration and never comes from a
    # ticket — a ticket that could name its target could choose what the bot
    # writes to.
    repo_url: str | None = Field(default=None, max_length=500)
    repo_full_name: str | None = Field(default=None, max_length=200)
    base_branch: str | None = Field(default=None, max_length=100)
    test_command: str | None = Field(default=None, max_length=500)
    allow_dependency_changes: bool | None = None


@app.put("/api/workflows/{workflow_id}/config")
async def configure_workflow(workflow_id: str, payload: WorkflowConfigRequest,
                             principal: Principal = Depends(require_role(Role.admin))) -> dict:
    with session_scope() as session:
        workflow = session.get(Workflow, workflow_id)
        if workflow is None or workflow.workspace_id != principal.workspace_id:
            raise HTTPException(status_code=404, detail="unknown workflow")
        changes = payload.model_dump(exclude_unset=True, exclude_none=True)
        if changes.get("dry_run") is False and not workflow.dry_run_passed_at:
            raise HTTPException(
                status_code=409,
                detail="Leave dry run on until a dry run has succeeded.")
        workflow.config = {**workflow.config, **changes}
        audit(session, actor=principal.user.display_name, action="workflow_configured",
              entity_type="workflow", entity_id=workflow_id,
              workspace_id=principal.workspace_id,
              actor_verified=principal.user.identity_verified, detail=changes)
        view = _workflow_view(workflow)
    return view


# --- connections -------------------------------------------------------------

class ConnectionRequest(BaseModel):
    """No credential fields, deliberately.

    Secrets come from the environment. A form that accepted a password and
    promised not to store it would be a weaker guarantee than a form that has
    nowhere to put one.
    """
    kind: str = "servicenow"
    name: str = Field(min_length=1, max_length=80)
    filter_query: str = Field(default="", max_length=1000)


@app.get("/api/connections")
async def list_connections(principal: Principal = Depends(require_role(Role.viewer))) -> dict:
    with session_scope() as session:
        rows = [{"id": c.id, "kind": c.kind, "name": c.name, "config": c.config,
                 "enabled": c.enabled}
                for c in session.query(Connection)
                .filter(Connection.workspace_id == principal.workspace_id).all()]
    return {
        "connections": rows,
        # Presence only. Values are never returned, and there is no endpoint
        # that would return them.
        "environment": servicenow.env_status(),
        "missing_for_reads": servicenow.missing_env(for_writes=False),
        "missing_for_writes": servicenow.missing_env(for_writes=True),
    }


@app.post("/api/connections")
async def create_connection(payload: ConnectionRequest,
                            principal: Principal = Depends(require_role(Role.admin))) -> dict:
    with session_scope() as session:
        connection = Connection(workspace_id=principal.workspace_id, kind=payload.kind,
                                name=payload.name,
                                config={"filter_query": payload.filter_query})
        session.add(connection)
        session.flush()
        audit(session, actor=principal.user.display_name, action="connection_created",
              entity_type="connection", entity_id=connection.id,
              workspace_id=principal.workspace_id,
              actor_verified=principal.user.identity_verified,
              detail={"kind": payload.kind, "name": payload.name})
        view = {"id": connection.id, "kind": connection.kind, "name": connection.name,
                "config": connection.config, "enabled": connection.enabled}
    return view


@app.post("/api/connections/test")
async def test_connection(principal: Principal = Depends(require_role(Role.operator))) -> dict:
    """Prove the credentials work before a workflow depends on them."""
    missing = servicenow.missing_env(for_writes=False)
    if missing:
        return {"ok": False, "detail": f"missing environment variables: {', '.join(missing)}"}
    client = servicenow.reader()
    try:
        await asyncio.to_thread(client.test)
    except servicenow.ServiceNowError as error:
        return {"ok": False, "detail": str(error)}
    return {"ok": True, "detail": "Read credentials work.",
            "writes_available": not servicenow.missing_env(for_writes=True)}


# --- runs --------------------------------------------------------------------

class StartRunRequest(BaseModel):
    # The ticket is untrusted data. It is never treated as instructions; see
    # engine/llm.py for how it is fenced in the prompt.
    ticket: dict
    dry_run: bool | None = None


def _run_view(run: Run) -> dict:
    return {"id": run.id, "workflow_id": run.workflow_id, "status": run.status.value,
            "trigger_ref": run.trigger_ref, "dry_run": run.dry_run,
            "started_at": run.started_at, "finished_at": run.finished_at,
            "cost_usd": run.cost_usd, "error": run.error}


@app.post("/api/workflows/{workflow_id}/runs")
async def start_run(workflow_id: str, payload: StartRunRequest,
                    principal: Principal = Depends(require_role(Role.operator))) -> dict:
    with session_scope() as session:
        workflow = session.get(Workflow, workflow_id)
        if workflow is None or workflow.workspace_id != principal.workspace_id:
            raise HTTPException(status_code=404, detail="unknown workflow")
    try:
        run_id = engine().start(workflow_id=workflow_id, ticket=payload.ticket,
                                actor=principal.user.display_name,
                                actor_verified=principal.user.identity_verified,
                                dry_run=payload.dry_run)
    except DuplicateRun as duplicate:
        # 409 with the run that already exists — a poller re-seeing a ticket
        # should be able to follow the link, not treat this as an error.
        raise HTTPException(status_code=409,
                            detail={"message": str(duplicate),
                                    "run_id": duplicate.run_id}) from duplicate
    except Halted as halt:
        # 409: the request was valid, the workspace is stopped.
        raise HTTPException(status_code=409, detail=str(halt)) from halt
    except EngineError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"run_id": run_id, "status": "started"}


@app.get("/api/runs")
async def list_runs(limit: int = 50,
                    principal: Principal = Depends(require_role(Role.viewer))) -> dict:
    with session_scope() as session:
        runs = (session.query(Run).filter(Run.workspace_id == principal.workspace_id)
                .order_by(Run.started_at.desc()).limit(min(limit, 200)).all())
        views = [_run_view(r) for r in runs]
    return {"runs": views}


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str,
                  principal: Principal = Depends(require_role(Role.viewer))) -> dict:
    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None or run.workspace_id != principal.workspace_id:
            # 404 rather than 403: a cross-workspace 403 confirms the run exists.
            raise HTTPException(status_code=404, detail="unknown run")
        steps = [{"id": s.id, "node": s.node, "agent_id": s.agent_id, "status": s.status,
                  "started_at": s.started_at, "finished_at": s.finished_at,
                  "output": s.output, "cost_usd": s.cost_usd,
                  "input_tokens": s.input_tokens, "output_tokens": s.output_tokens,
                  "error": s.error}
                 for s in session.query(RunStep).filter(RunStep.run_id == run_id)
                 .order_by(RunStep.started_at).all()]
        approvals = [_approval_view(a) for a in session.query(Approval)
                     .filter(Approval.run_id == run_id)
                     .order_by(Approval.requested_at).all()]
        view = _run_view(run)
    return {**view, "steps": steps, "approvals": approvals}


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str,
                     principal: Principal = Depends(require_role(Role.operator))) -> dict:
    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None or run.workspace_id != principal.workspace_id:
            raise HTTPException(status_code=404, detail="unknown run")
    try:
        engine().cancel(run_id=run_id, actor=principal.user.display_name,
                        actor_verified=principal.user.identity_verified)
    except EngineError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"run_id": run_id, "status": "cancelled"}


# --- approvals ---------------------------------------------------------------

class DecisionRequest(BaseModel):
    approved: bool
    note: str | None = Field(default=None, max_length=1000)


def _approval_view(approval: Approval) -> dict:
    return {"id": approval.id, "run_id": approval.run_id, "node": approval.node,
            "summary": approval.summary, "payload": approval.payload,
            "payload_hash": approval.payload_hash, "status": approval.status.value,
            "requested_at": approval.requested_at, "decided_at": approval.decided_at,
            "note": approval.note}


@app.get("/api/approvals")
async def list_approvals(principal: Principal = Depends(require_role(Role.viewer))) -> dict:
    with session_scope() as session:
        pending = (session.query(Approval).join(Run, Approval.run_id == Run.id)
                   .filter(Run.workspace_id == principal.workspace_id,
                           Approval.status == ApprovalStatus.pending)
                   .order_by(Approval.requested_at).all())
        views = [_approval_view(a) for a in pending]
    return {"approvals": views,
            # A viewer can see the queue but not act on it; the UI uses this to
            # explain the disabled buttons rather than just showing them greyed.
            "can_decide": principal.can(Role.approver)}


@app.post("/api/approvals/{approval_id}")
async def decide_approval(approval_id: str, payload: DecisionRequest,
                          principal: Principal = Depends(require_role(Role.approver))) -> dict:
    with session_scope() as session:
        approval = session.get(Approval, approval_id)
        if approval is None:
            raise HTTPException(status_code=404, detail="unknown approval")
        run = session.get(Run, approval.run_id)
        if run is None or run.workspace_id != principal.workspace_id:
            raise HTTPException(status_code=404, detail="unknown approval")
    try:
        run_id = engine().decide(approval_id=approval_id, approved=payload.approved,
                                 actor=principal.user.display_name,
                                 actor_id=principal.user.id,
                                 actor_verified=principal.user.identity_verified,
                                 note=payload.note)
    except StaleApproval as stale:
        # 409 Conflict is the accurate answer: what you approved is not what is
        # there now.
        raise HTTPException(status_code=409, detail=str(stale)) from stale
    except EngineError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"run_id": run_id, "approved": payload.approved}


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
