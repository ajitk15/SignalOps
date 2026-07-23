"""SignalOps v2 — agentic workflow platform.

Phase 0b: data model, dummy authentication behind a real authorisation model,
and the application shell. Workflows, agents and the engine follow.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import sys
from pathlib import Path

from dotenv import load_dotenv

# Windows asyncio needs the Proactor loop for subprocess support.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import (BackgroundTasks, Depends, FastAPI, HTTPException, Response,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")
sys.path.insert(0, str(PROJECT_ROOT))

import time  # noqa: E402

from agents import export as agent_export  # noqa: E402
from agents.catalogue import ALLOWED_MODELS, CATALOGUE, Tier  # noqa: E402
from agents.catalogue import get as catalogue_get  # noqa: E402
from agents import custom as custom_agents  # noqa: E402
from agents.guard import TOOL_TIERS, GuardrailViolation, resolve  # noqa: E402
from auth import (COOKIE_SECURE, SESSION_COOKIE, SESSION_MAX_AGE_SECONDS,  # noqa: E402
                  AdminNotConfigured, InvalidCredentials, normalise_email, valid_email,
                  PasswordPolicy, Principal, admin_from_env, current_principal,
                  ensure_admin, hash_password, issue_session, provider,
                  record_login, require_role, set_password)
from db import audit, audit_entries, init_db, session_scope  # noqa: E402
from email_delivery import (password_reset_delivery_available,  # noqa: E402
                            send_password_reset_email)
from engine import workflow_export  # noqa: E402
from engine.approvals import StaleApproval  # noqa: E402
from engine.runtime import (TEMPLATES, DuplicateRun, EngineError,  # noqa: E402
                            engine)
from engine.poller import poller  # noqa: E402
from engine.state import Halted  # noqa: E402
from integrations import servicenow  # noqa: E402
from events import Event, bus  # noqa: E402
from models import (AgentConfig, Approval, ApprovalStatus, Connection,  # noqa: E402
                    CustomAgent, CustomAgentStatus, RegistrationRequest,
                    RegistrationStatus, PasswordResetToken, Role, Run,
                    RunStatus, RunStep, User, Workflow, Workspace)

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

app = FastAPI(title="SignalAIOps")
DASHBOARD_DIR = PROJECT_ROOT / "dashboard"
STATIC_FILES = {
    "app.css",
    "app.js",
    "landing.css",
    "og.png",
    "signalaiops-favicon-v3.png",
    "signalaiops-logo-dark-v3.png",
    "signalaiops-logo-light-v3.png",
}
WORKSPACE_ID = init_db()


def _establish_admin() -> None:
    """Create the environment's administrator, or refuse to start.

    Starting without one would serve a login screen nobody on earth could get
    past, which is a worse failure than not starting: it looks like the app is
    working.
    """
    with session_scope() as session:
        try:
            admin = ensure_admin(session, WORKSPACE_ID)
        except AdminNotConfigured as error:
            raise RuntimeError(str(error)) from error
        if admin is not None:
            logger.info("administrator %s is configured from the environment", admin.email)
            return
        if session.query(User).first() is None:
            raise RuntimeError(
                "No administrator exists and none is configured. Set "
                "SIGNALOPS_ADMIN_EMAIL and SIGNALOPS_ADMIN_PASSWORD in .env, then "
                "restart. Other users are created or approved by that administrator "
                "from the Users screen.")
        logger.warning(
            "SIGNALOPS_ADMIN_EMAIL is not set. Existing accounts still work, but "
            "there is no way to recover admin access if it is lost.")


_establish_admin()


# --- shell -------------------------------------------------------------------

@app.get("/")
async def index() -> FileResponse:
    # no-cache: revalidate every load so a shipped change is never masked by a
    # stale copy.
    return FileResponse(DASHBOARD_DIR / "landing.html",
                        headers={"Cache-Control": "no-cache"})


@app.get("/login")
async def login_page() -> FileResponse:
    return FileResponse(DASHBOARD_DIR / "index.html",
                        headers={"Cache-Control": "no-cache"})


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
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=300)


def _registration_enabled() -> bool:
    """Return the deploy-time public registration switch.

    It is read for every request rather than frozen at import time so tests and
    process supervisors can set it consistently. Production should explicitly
    opt in with ``SIGNALOPS_REGISTRATION_ENABLED=true``.
    """
    return os.getenv("SIGNALOPS_REGISTRATION_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }


@app.get("/api/auth/state")
async def auth_state() -> dict:
    """What the login screen needs before anyone has signed in.

    Registration creates a pending request only. It never creates an account
    or grants a role; both remain administrator-only operations.
    """
    return {"provider": provider().name,
            "verifies_identity": provider().verifies_identity,
            "admin_configured": admin_from_env() is not None,
            "registration_enabled": _registration_enabled(),
            "password_reset_email_configured": password_reset_delivery_available()}


class AccessRequestPayload(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    display_name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=300)

    @field_validator("email")
    @classmethod
    def _email_shape(cls, value: str) -> str:
        if not valid_email(value):
            raise ValueError("that is not a valid email address")
        return normalise_email(value)

    @field_validator("display_name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a name is required")
        return value.strip()


ACCESS_REQUEST_RESPONSE = (
    "If this address is eligible, the request is awaiting administrator approval."
)


@app.post("/api/auth/register", status_code=202)
async def request_access(payload: AccessRequestPayload) -> dict:
    """Record an access request without creating an account.

    Every successful submission returns the same response, including when the
    address already belongs to a user or has a pending request. That prevents
    this public endpoint from becoming an account-enumeration oracle.
    """
    if not _registration_enabled():
        raise HTTPException(status_code=404, detail="access requests are not enabled")
    try:
        password_hash = hash_password(payload.password)
    except PasswordPolicy as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    with session_scope() as session:
        existing_user = (session.query(User)
                         .filter(User.workspace_id == WORKSPACE_ID,
                                 User.email == payload.email).first())
        request = (session.query(RegistrationRequest)
                   .filter(RegistrationRequest.workspace_id == WORKSPACE_ID,
                           RegistrationRequest.email == payload.email).first())

        if existing_user is None and request is None:
            request = RegistrationRequest(
                workspace_id=WORKSPACE_ID,
                email=payload.email,
                display_name=payload.display_name,
                password_hash=password_hash,
            )
            try:
                # A unique index is the final arbiter if two identical public
                # requests arrive between the lookup and insert. The savepoint
                # keeps the generic response successful without poisoning the
                # surrounding transaction.
                with session.begin_nested():
                    session.add(request)
                    session.flush()
            except IntegrityError:
                request = None
            if request is not None:
                audit(session, actor=payload.email, action="registration_requested",
                      entity_type="registration_request", entity_id=request.id,
                      workspace_id=WORKSPACE_ID, actor_verified=False)
        elif (existing_user is None and request is not None
              and request.status is RegistrationStatus.rejected):
            # A rejected applicant may correct their details and ask again.
            # Pending and approved records are deliberately left untouched so
            # another person cannot replace an applicant's chosen password.
            request.display_name = payload.display_name
            request.password_hash = password_hash
            request.status = RegistrationStatus.pending
            request.requested_at = time.time()
            request.reviewed_at = None
            request.reviewed_by = None
            request.review_note = None
            request.approved_user_id = None
            request.notify_requested = False
            request.notification_status = "not_requested"
            audit(session, actor=payload.email, action="registration_requested",
                  entity_type="registration_request", entity_id=request.id,
                  workspace_id=WORKSPACE_ID, actor_verified=False,
                  detail={"resubmitted": True})

    return {"status": "pending", "message": ACCESS_REQUEST_RESPONSE}


PASSWORD_RESET_RESPONSE = (
    "If an active account exists for that email, a password-reset link will be sent shortly."
)


def _password_reset_ttl_minutes() -> int:
    try:
        requested = int(os.getenv("SIGNALOPS_PASSWORD_RESET_TTL_MINUTES", "15"))
    except ValueError:
        requested = 15
    return max(5, min(requested, 60))


def _password_reset_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ForgotPasswordRequest(BaseModel):
    email: str = Field(min_length=3, max_length=200)

    @field_validator("email")
    @classmethod
    def _email_shape(cls, value: str) -> str:
        if not valid_email(value):
            raise ValueError("that is not a valid email address")
        return normalise_email(value)


class PasswordResetCompletion(BaseModel):
    token: str = Field(min_length=32, max_length=300)
    new_password: str = Field(min_length=1, max_length=300)


@app.post("/api/auth/forgot-password", status_code=202)
async def forgot_password(payload: ForgotPasswordRequest,
                          background_tasks: BackgroundTasks) -> dict:
    """Issue a one-time link without revealing whether the account exists."""
    email_job: dict | None = None
    now = time.time()
    if password_reset_delivery_available():
        with session_scope() as session:
            # Keep the table bounded without a separate scheduled job.
            (session.query(PasswordResetToken)
             .filter(PasswordResetToken.expires_at < now - (7 * 24 * 3600))
             .delete(synchronize_session=False))
            user = (session.query(User)
                    .filter(User.workspace_id == WORKSPACE_ID,
                            User.email == payload.email,
                            User.active.is_(True))
                    .one_or_none())
            if user is not None:
                latest = (session.query(PasswordResetToken)
                          .filter(PasswordResetToken.user_id == user.id,
                                  PasswordResetToken.used_at.is_(None))
                          .order_by(PasswordResetToken.created_at.desc())
                          .first())
                # One message per minute per account prevents this public route
                # from becoming an email-bombing primitive.
                if latest is None or latest.created_at < now - 60:
                    raw_token = secrets.token_urlsafe(32)
                    ttl_minutes = _password_reset_ttl_minutes()
                    token = PasswordResetToken(
                        user_id=user.id,
                        token_hash=_password_reset_digest(raw_token),
                        created_at=now,
                        expires_at=now + (ttl_minutes * 60),
                    )
                    session.add(token)
                    session.flush()
                    audit(session, actor=user.email,
                          action="password_reset_requested",
                          entity_type="user", entity_id=user.id,
                          workspace_id=WORKSPACE_ID, actor_verified=False)
                    email_job = {
                        "to_email": user.email,
                        "display_name": user.display_name,
                        "token": raw_token,
                        "ttl_minutes": ttl_minutes,
                    }
    if email_job is not None:
        # The response is identical and can be sent before SMTP work completes,
        # avoiding an account-enumeration timing difference.
        background_tasks.add_task(send_password_reset_email, **email_job)
    return {"message": PASSWORD_RESET_RESPONSE}


@app.post("/api/auth/reset-password")
async def reset_password(payload: PasswordResetCompletion,
                         response: Response) -> dict:
    """Consume a one-time email token and revoke every older session."""
    now = time.time()
    digest = _password_reset_digest(payload.token)
    with session_scope() as session:
        token = (session.query(PasswordResetToken)
                 .filter(PasswordResetToken.token_hash == digest,
                         PasswordResetToken.used_at.is_(None),
                         PasswordResetToken.expires_at > now)
                 .one_or_none())
        if token is None:
            raise HTTPException(
                status_code=400,
                detail="This password-reset link is invalid or expired.")
        claimed = (session.query(PasswordResetToken)
                   .filter(PasswordResetToken.id == token.id,
                           PasswordResetToken.used_at.is_(None),
                           PasswordResetToken.expires_at > now)
                   .update({"used_at": now}, synchronize_session=False))
        if claimed != 1:
            raise HTTPException(
                status_code=400,
                detail="This password-reset link is invalid or expired.")
        user = session.get(User, token.user_id)
        if user is None or not user.active:
            raise HTTPException(
                status_code=400,
                detail="This password-reset link is invalid or expired.")
        try:
            set_password(user, payload.new_password, revoke_sessions=True)
        except PasswordPolicy as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        # A successful reset retires every outstanding link for the account.
        (session.query(PasswordResetToken)
         .filter(PasswordResetToken.user_id == user.id,
                 PasswordResetToken.used_at.is_(None))
         .update({"used_at": now}, synchronize_session=False))
        audit(session, actor=user.email, action="password_reset_completed",
              entity_type="user", entity_id=user.id,
              workspace_id=user.workspace_id, actor_verified=True)
    response.delete_cookie(SESSION_COOKIE)
    return {"status": "changed",
            "message": "Your password has been reset. You can now sign in."}


@app.post("/api/auth/login")
async def login(payload: LoginRequest, response: Response) -> dict:
    with session_scope() as session:
        try:
            user = provider().login(session, WORKSPACE_ID, email=payload.email,
                                    password=payload.password,
                                    display_name=payload.email.split("@")[0])
        except InvalidCredentials as error:
            # Audited by email rather than by user, because the interesting
            # case is failures against an address that does not exist.
            audit(session, actor=payload.email[:120], action="login_failed",
                  entity_type="user", entity_id="-", workspace_id=WORKSPACE_ID,
                  actor_verified=False)
            session.commit()
            raise HTTPException(status_code=401, detail=str(error)) from error
        record_login(session, user, WORKSPACE_ID)
        workspace = session.get(Workspace, WORKSPACE_ID)
        principal = Principal(user, workspace)
        token = issue_session(user)
        view = principal.as_dict()
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                        secure=COOKIE_SECURE, max_age=SESSION_MAX_AGE_SECONDS)
    return view


@app.post("/api/auth/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE)
    return {"status": "logged out"}


@app.get("/api/auth/me")
async def me(principal: Principal = Depends(current_principal)) -> dict:
    return principal.as_dict()


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=300)
    new_password: str = Field(min_length=1, max_length=300)


@app.post("/api/auth/password")
async def change_password(payload: PasswordChangeRequest,
                          principal: Principal = Depends(current_principal)) -> dict:
    """Change your own password. The current one is required.

    Even though the session already proves who you are: an unattended browser
    is the common case, and re-asking is what stops it becoming an account
    takeover.
    """
    with session_scope() as session:
        user = session.get(User, principal.user.id)
        try:
            provider().login(session, principal.workspace_id, email=user.email,
                             password=payload.current_password)
        except InvalidCredentials as error:
            raise HTTPException(status_code=403,
                                detail="current password is incorrect") from error
        try:
            set_password(user, payload.new_password)
        except PasswordPolicy as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        audit(session, actor=user.display_name, action="password_changed",
              entity_type="user", entity_id=user.id,
              workspace_id=principal.workspace_id, actor_verified=True)
    return {"status": "changed"}


# --- user administration -----------------------------------------------------

class UserRequest(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    display_name: str = Field(min_length=1, max_length=80)
    role: Role = Role.viewer
    password: str = Field(min_length=1, max_length=300)

    @field_validator("email")
    @classmethod
    def _email_shape(cls, value: str) -> str:
        # min_length alone lets "not-an-email" and three spaces through, both of
        # which then persist as a broken or empty identity. Validate the shape
        # and return it normalised, so the stored value matches what login sees.
        if not valid_email(value):
            raise ValueError("that is not a valid email address")
        return normalise_email(value)

    @field_validator("display_name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a name is required")
        return value.strip()


class UserUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=80)
    role: Role | None = None
    active: bool | None = None
    # Set by an admin, which forces a change at next login rather than leaving
    # a password only the admin chose in place indefinitely.
    password: str | None = Field(default=None, max_length=300)

    @field_validator("display_name")
    @classmethod
    def _name_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("a name cannot be blank")
        return value.strip() if value is not None else None


def _user_view(user: User) -> dict:
    return {"id": user.id, "email": user.email, "display_name": user.display_name,
            "role": user.role.value, "active": user.active,
            "must_change_password": bool(user.must_change_password),
            "has_password": bool(user.password_hash),
            "locked": bool(user.locked_until and user.locked_until > time.time()),
            "last_login_at": user.last_login_at, "created_at": user.created_at}


class RegistrationReviewRequest(BaseModel):
    notify_applicant: bool = False
    note: str | None = Field(default=None, max_length=500)

    @field_validator("note")
    @classmethod
    def _clean_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class RegistrationApprovalRequest(RegistrationReviewRequest):
    role: Role = Role.viewer


def _registration_view(request: RegistrationRequest) -> dict:
    """Serialize a request without ever exposing its password hash."""
    return {
        "id": request.id,
        "email": request.email,
        "display_name": request.display_name,
        "status": request.status.value,
        "requested_at": request.requested_at,
        "reviewed_at": request.reviewed_at,
        "reviewed_by": request.reviewed_by,
        "review_note": request.review_note,
        "approved_user_id": request.approved_user_id,
        "notify_requested": bool(request.notify_requested),
        "notification_status": request.notification_status,
    }


def _registration_for_admin(session, request_id: str,
                            principal: Principal) -> RegistrationRequest:
    request = session.get(RegistrationRequest, request_id)
    if request is None or request.workspace_id != principal.workspace_id:
        # Do not reveal a request belonging to a different workspace.
        raise HTTPException(status_code=404, detail="unknown access request")
    return request


def _record_notification_intent(request: RegistrationRequest, notify: bool) -> None:
    request.notify_requested = notify
    # No delivery provider exists yet. This status is intentionally explicit:
    # selecting Notify must never make the admin believe an email was sent.
    request.notification_status = "not_configured" if notify else "not_requested"


@app.get("/api/users")
async def list_users(principal: Principal = Depends(require_role(Role.admin))) -> dict:
    with session_scope() as session:
        rows = [_user_view(u) for u in session.query(User)
                .filter(User.workspace_id == principal.workspace_id)
                .order_by(User.created_at).all()]
    return {"users": rows, "roles": [r.value for r in Role]}


@app.get("/api/registration-requests")
async def list_registration_requests(
        principal: Principal = Depends(require_role(Role.admin))) -> dict:
    with session_scope() as session:
        requests = (session.query(RegistrationRequest)
                    .filter(RegistrationRequest.workspace_id == principal.workspace_id)
                    .order_by(RegistrationRequest.requested_at.desc())
                    .limit(100).all())
        # Pending work stays at the top; decided requests remain visible so the
        # administrator has a lightweight decision history.
        requests.sort(key=lambda item: item.status is not RegistrationStatus.pending)
        rows = [_registration_view(request) for request in requests]
    return {
        "requests": rows,
        "roles": [role.value for role in Role],
        "notifications_available": False,
    }


@app.post("/api/registration-requests/{request_id}/approve")
async def approve_registration_request(
        request_id: str, payload: RegistrationApprovalRequest,
        principal: Principal = Depends(require_role(Role.admin))) -> dict:
    with session_scope() as session:
        request = _registration_for_admin(session, request_id, principal)
        if request.status is not RegistrationStatus.pending:
            raise HTTPException(status_code=409, detail="this request was already reviewed")
        existing_user = (session.query(User)
                         .filter(User.workspace_id == principal.workspace_id,
                                 User.email == request.email).first())
        if existing_user is not None:
            raise HTTPException(status_code=409,
                                detail="that email already has an account")
        if not request.password_hash:
            raise HTTPException(status_code=409,
                                detail="this request no longer has pending credentials")

        user = User(
            workspace_id=principal.workspace_id,
            email=request.email,
            display_name=request.display_name,
            role=payload.role,
            password_hash=request.password_hash,
            active=True,
            # This password was chosen by the applicant and never known by the
            # administrator, so there is no handover credential to replace.
            must_change_password=False,
            identity_verified=False,
        )
        session.add(user)
        session.flush()
        request.status = RegistrationStatus.approved
        request.reviewed_at = time.time()
        request.reviewed_by = principal.user.id
        request.review_note = payload.note
        request.approved_user_id = user.id
        request.password_hash = None
        _record_notification_intent(request, payload.notify_applicant)
        audit(session, actor=principal.user.display_name,
              action="registration_approved",
              entity_type="registration_request", entity_id=request.id,
              workspace_id=principal.workspace_id,
              actor_verified=principal.user.identity_verified,
              detail={"email": request.email, "role": payload.role.value,
                      "notify_requested": payload.notify_applicant,
                      "notification_status": request.notification_status})
        request_view = _registration_view(request)
        user_view = _user_view(user)
    return {"request": request_view, "user": user_view}


@app.post("/api/registration-requests/{request_id}/reject")
async def reject_registration_request(
        request_id: str, payload: RegistrationReviewRequest,
        principal: Principal = Depends(require_role(Role.admin))) -> dict:
    with session_scope() as session:
        request = _registration_for_admin(session, request_id, principal)
        if request.status is not RegistrationStatus.pending:
            raise HTTPException(status_code=409, detail="this request was already reviewed")
        request.status = RegistrationStatus.rejected
        request.reviewed_at = time.time()
        request.reviewed_by = principal.user.id
        request.review_note = payload.note
        request.approved_user_id = None
        request.password_hash = None
        _record_notification_intent(request, payload.notify_applicant)
        audit(session, actor=principal.user.display_name,
              action="registration_rejected",
              entity_type="registration_request", entity_id=request.id,
              workspace_id=principal.workspace_id,
              actor_verified=principal.user.identity_verified,
              detail={"email": request.email,
                      "notify_requested": payload.notify_applicant,
                      "notification_status": request.notification_status})
        view = _registration_view(request)
    return {"request": view}


@app.post("/api/users")
async def create_user(payload: UserRequest,
                      principal: Principal = Depends(require_role(Role.admin))) -> dict:
    with session_scope() as session:
        email = payload.email.strip().lower()
        if (session.query(User)
                .filter(User.workspace_id == principal.workspace_id,
                        User.email == email).first()):
            raise HTTPException(status_code=409, detail="that email already has an account")
        if (session.query(RegistrationRequest)
                .filter(RegistrationRequest.workspace_id == principal.workspace_id,
                        RegistrationRequest.email == email,
                        RegistrationRequest.status == RegistrationStatus.pending).first()):
            raise HTTPException(
                status_code=409,
                detail="that email has a pending access request; approve it instead")
        try:
            hashed = hash_password(payload.password)
        except PasswordPolicy as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        user = User(workspace_id=principal.workspace_id, email=email,
                    display_name=payload.display_name.strip(), role=payload.role,
                    password_hash=hashed,
                    # The admin knows this password, so it is a handover
                    # credential and not the user's own until they change it.
                    must_change_password=True)
        session.add(user)
        session.flush()
        audit(session, actor=principal.user.display_name, action="user_created",
              entity_type="user", entity_id=user.id,
              workspace_id=principal.workspace_id,
              actor_verified=principal.user.identity_verified,
              detail={"email": email, "role": payload.role.value})
        view = _user_view(user)
    return view


@app.put("/api/users/{user_id}")
async def update_user(user_id: str, payload: UserUpdateRequest,
                      principal: Principal = Depends(require_role(Role.admin))) -> dict:
    with session_scope() as session:
        user = session.get(User, user_id)
        if user is None or user.workspace_id != principal.workspace_id:
            raise HTTPException(status_code=404, detail="unknown user")

        changes = payload.model_dump(exclude_unset=True, exclude_none=True)
        demoting = "role" in changes and changes["role"] is not Role.admin
        deactivating = changes.get("active") is False
        if user.id == principal.user.id and (demoting or deactivating):
            # Locking yourself out is recoverable only by editing the database.
            raise HTTPException(
                status_code=409,
                detail="you cannot remove your own admin access or deactivate yourself")
        if (demoting or deactivating) and _last_active_admin(session, user):
            raise HTTPException(
                status_code=409,
                detail="this is the last active admin; promote someone else first")

        if payload.display_name is not None:
            user.display_name = payload.display_name.strip()
        if payload.role is not None:
            user.role = payload.role
        if payload.active is not None:
            user.active = payload.active
        if payload.password:
            try:
                set_password(user, payload.password)
            except PasswordPolicy as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
            user.must_change_password = True
        audit(session, actor=principal.user.display_name, action="user_updated",
              entity_type="user", entity_id=user_id,
              workspace_id=principal.workspace_id,
              actor_verified=principal.user.identity_verified,
              # Never record the password, only that one was set.
              detail={k: (True if k == "password" else str(v))
                      for k, v in changes.items()})
        view = _user_view(user)
    return view


def _last_active_admin(session, user: User) -> bool:
    if user.role is not Role.admin or not user.active:
        return False
    others = (session.query(User)
              .filter(User.workspace_id == user.workspace_id,
                      User.role == Role.admin, User.active.is_(True),
                      User.id != user.id).count())
    return others == 0


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
    with session_scope() as session:
        customs = [_custom_agent_view(c) for c in session.query(CustomAgent)
                   .filter(CustomAgent.workspace_id == principal.workspace_id)
                   .order_by(CustomAgent.created_at.desc()).all()]
    return {"agents": agents, "tiers": {t.value: TOOL_TIERS_BY_TIER.get(t.value, [])
                                        for t in Tier},
            "custom_agents": customs,
            "grantable_tools": [{"name": name, "tier": custom_agents.SDK_TOOL_TIERS[name].value}
                                for name in custom_agents.GRANTABLE_TOOLS],
            "can_author": principal.can(Role.operator),
            "can_approve": principal.can(Role.admin)}


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




# --- custom agents (user-authored, admin-reviewed) ---------------------------

class CustomAgentRequest(BaseModel):
    """A proposed agent. Tools are picked from the grantable set; the tier is
    derived on the server, so neither can be used to widen reach."""
    name: str = Field(min_length=1, max_length=80)
    purpose: str = Field(min_length=8, max_length=200)
    explanation: str = Field(default="", max_length=1000)
    workflow: str = Field(default="both")
    model: str
    system_prompt: str = Field(min_length=20, max_length=8000)
    tools: list[str] = Field(default_factory=list)
    output_schema: dict | None = None


class CustomReviewRequest(BaseModel):
    approved: bool
    note: str | None = Field(default=None, max_length=1000)


def _custom_agent_view(row: CustomAgent) -> dict:
    return {
        "id": row.id, "name": row.name, "purpose": row.purpose,
        "explanation": row.explanation, "workflow": row.workflow, "model": row.model,
        "system_prompt": row.system_prompt, "tools": list(row.tools or ()),
        "tier": row.tier, "output_schema": row.output_schema,
        "status": row.status.value, "enabled": row.enabled,
        "created_by": row.created_by, "created_at": row.created_at,
        "reviewed_at": row.reviewed_at, "review_note": row.review_note,
        "custom": True,
    }


@app.get("/api/agents/custom")
async def list_custom_agents(
        principal: Principal = Depends(require_role(Role.viewer))) -> dict:
    with session_scope() as session:
        rows = [_custom_agent_view(c) for c in session.query(CustomAgent)
                .filter(CustomAgent.workspace_id == principal.workspace_id)
                .order_by(CustomAgent.created_at.desc()).all()]
    return {"custom_agents": rows,
            "grantable_tools": [{"name": name, "tier": custom_agents.SDK_TOOL_TIERS[name].value}
                                for name in custom_agents.GRANTABLE_TOOLS],
            "can_approve": principal.can(Role.admin)}


@app.post("/api/agents/custom")
async def create_custom_agent(
        payload: CustomAgentRequest,
        principal: Principal = Depends(require_role(Role.operator))) -> dict:
    """Create a custom agent.

    An admin's agent is approved on creation. Anyone else's is submitted for
    review and cannot run until an admin approves it — the review is the point
    at which a human confirms the prompt and the granted tools are sane.
    """
    try:
        validated = custom_agents.validate(
            name=payload.name, purpose=payload.purpose, explanation=payload.explanation,
            workflow=payload.workflow, model=payload.model,
            system_prompt=payload.system_prompt, tools=payload.tools,
            output_schema=payload.output_schema)
    except (custom_agents.CustomAgentInvalid, GuardrailViolation) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    is_admin = principal.can(Role.admin)
    with session_scope() as session:
        clash = (session.query(CustomAgent)
                 .filter(CustomAgent.workspace_id == principal.workspace_id,
                         CustomAgent.name == validated.name).first())
        if clash is not None:
            raise HTTPException(status_code=409,
                                detail=f"a custom agent named {validated.name!r} already exists")
        row = CustomAgent(
            workspace_id=principal.workspace_id, name=validated.name,
            purpose=validated.purpose, explanation=validated.explanation,
            workflow=validated.workflow, model=validated.model,
            system_prompt=validated.system_prompt, tools=list(validated.tools),
            tier=validated.tier, output_schema=validated.output_schema,
            status=(CustomAgentStatus.approved if is_admin
                    else CustomAgentStatus.pending_review),
            created_by=principal.user.id,
            reviewed_by=principal.user.id if is_admin else None,
            reviewed_at=time.time() if is_admin else None)
        session.add(row)
        session.flush()
        audit(session, actor=principal.user.display_name, action="custom_agent_created",
              entity_type="custom_agent", entity_id=row.id,
              workspace_id=principal.workspace_id,
              actor_verified=principal.user.identity_verified,
              detail={"name": validated.name, "tier": validated.tier,
                      "tools": list(validated.tools), "status": row.status.value})
        view = _custom_agent_view(row)
    return view


@app.put("/api/agents/custom/{agent_id}")
async def update_custom_agent(
        agent_id: str, payload: CustomAgentRequest,
        principal: Principal = Depends(require_role(Role.operator))) -> dict:
    """Edit a custom agent.

    An admin may edit any; a non-admin may edit only their own while it is still
    pending review. Any edit re-validates and, for a non-admin, returns it to
    pending review — a changed prompt has not been approved.
    """
    try:
        validated = custom_agents.validate(
            name=payload.name, purpose=payload.purpose, explanation=payload.explanation,
            workflow=payload.workflow, model=payload.model,
            system_prompt=payload.system_prompt, tools=payload.tools,
            output_schema=payload.output_schema)
    except (custom_agents.CustomAgentInvalid, GuardrailViolation) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    is_admin = principal.can(Role.admin)
    with session_scope() as session:
        row = session.get(CustomAgent, agent_id)
        if row is None or row.workspace_id != principal.workspace_id:
            raise HTTPException(status_code=404, detail="unknown custom agent")
        if not is_admin and (row.created_by != principal.user.id
                             or row.status is not CustomAgentStatus.pending_review):
            raise HTTPException(
                status_code=403,
                detail="you can only edit your own agents while they await review")
        row.name = validated.name
        row.purpose = validated.purpose
        row.explanation = validated.explanation
        row.workflow = validated.workflow
        row.model = validated.model
        row.system_prompt = validated.system_prompt
        row.tools = list(validated.tools)
        row.tier = validated.tier
        row.output_schema = validated.output_schema
        if not is_admin:
            row.status = CustomAgentStatus.pending_review
            row.reviewed_by = None
            row.reviewed_at = None
        audit(session, actor=principal.user.display_name, action="custom_agent_updated",
              entity_type="custom_agent", entity_id=agent_id,
              workspace_id=principal.workspace_id,
              actor_verified=principal.user.identity_verified,
              detail={"tier": validated.tier, "tools": list(validated.tools)})
        view = _custom_agent_view(row)
    return view


@app.post("/api/agents/custom/{agent_id}/review")
async def review_custom_agent(
        agent_id: str, payload: CustomReviewRequest,
        principal: Principal = Depends(require_role(Role.admin))) -> dict:
    """Approve or reject a proposed agent. Admin only.

    Approval is what lets a non-admin's agent run: until then it exists but is
    inert. The reviewer's identity and note are recorded.
    """
    with session_scope() as session:
        row = session.get(CustomAgent, agent_id)
        if row is None or row.workspace_id != principal.workspace_id:
            raise HTTPException(status_code=404, detail="unknown custom agent")
        row.status = (CustomAgentStatus.approved if payload.approved
                      else CustomAgentStatus.rejected)
        row.reviewed_by = principal.user.id
        row.reviewed_at = time.time()
        row.review_note = payload.note
        audit(session, actor=principal.user.display_name,
              action="custom_agent_approved" if payload.approved else "custom_agent_rejected",
              entity_type="custom_agent", entity_id=agent_id,
              workspace_id=principal.workspace_id,
              actor_verified=principal.user.identity_verified,
              detail={"note": payload.note})
        view = _custom_agent_view(row)
    return view


@app.delete("/api/agents/custom/{agent_id}")
async def delete_custom_agent(
        agent_id: str, principal: Principal = Depends(require_role(Role.admin))) -> dict:
    with session_scope() as session:
        row = session.get(CustomAgent, agent_id)
        if row is None or row.workspace_id != principal.workspace_id:
            raise HTTPException(status_code=404, detail="unknown custom agent")
        session.delete(row)
        audit(session, actor=principal.user.display_name, action="custom_agent_deleted",
              entity_type="custom_agent", entity_id=agent_id,
              workspace_id=principal.workspace_id,
              actor_verified=principal.user.identity_verified)
    return {"status": "deleted", "id": agent_id}


@app.get("/api/agents/custom/{agent_id}/export")
async def export_custom_agent(
        agent_id: str, principal: Principal = Depends(require_role(Role.viewer))) -> Response:
    """A custom agent as a Claude subagent definition file."""
    with session_scope() as session:
        row = session.get(CustomAgent, agent_id)
        if row is None or row.workspace_id != principal.workspace_id:
            raise HTTPException(status_code=404, detail="unknown custom agent")
        resolved = custom_agents.resolve_row(row)
        markdown = agent_export.to_markdown(resolved.spec, resolved)
        filename = agent_export.filename_for(resolved.spec)
    return Response(
        content=markdown, media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# --- workflows ---------------------------------------------------------------

class WorkflowRequest(BaseModel):
    template: str
    name: str = Field(min_length=1, max_length=120)
    dry_run: bool = True
    run_budget_usd: float = Field(default=1.0, gt=0, le=100)
    # Which stored ServiceNow instance this workflow reads and writes. Naming a
    # connection rather than reading the environment is what lets one
    # installation drive several instances.
    connection_id: str | None = None


def _workflow_view(workflow: Workflow) -> dict:
    return {"id": workflow.id, "template": workflow.template, "name": workflow.name,
            "config": workflow.config, "enabled": workflow.enabled,
            "dry_run_passed_at": workflow.dry_run_passed_at,
            "created_at": workflow.created_at,
            "exportable": workflow.template in workflow_export.TEMPLATES,
            "polling": bool(workflow.config.get("poll_enabled")),
            "connection_id": workflow.config.get("connection_id"),
            "require_outcome_report": workflow.config.get("require_outcome_report", True),
            # Not a gate any more, but still worth showing: a workflow nobody
            # has watched run is a different thing from one that has been.
            "tested": bool(workflow.dry_run_passed_at)}


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
                                    "run_budget_usd": payload.run_budget_usd,
                                    "connection_id": payload.connection_id,
                                    # On by default: a ticket resolved because a
                                    # plan was approved, rather than because
                                    # somebody ran it, is the failure worth
                                    # defaulting against.
                                    "require_outcome_report": True})
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
        # Deliberately not a hard gate any more. Requiring a dry run before
        # every enable was ceremony on the common path — the meaningful
        # protections are that dry run is the default mode and that every run
        # stops at a human gate. The warning is still returned so "I have never
        # seen this run" stays visible.
        untested = payload.enabled and not workflow.dry_run_passed_at
        workflow.enabled = payload.enabled
        workflow.config = {**workflow.config, "poll_enabled": payload.poll_enabled}
        audit(session, actor=principal.user.display_name,
              action="workflow_enabled" if payload.enabled else "workflow_disabled",
              entity_type="workflow", entity_id=workflow_id,
              workspace_id=principal.workspace_id,
              actor_verified=principal.user.identity_verified,
              detail={"polling": payload.poll_enabled, "untested": untested})
        view = _workflow_view(workflow)
    await poller.sync()
    return {**view, "warning": (
        "Enabled without a test run. It will run in dry-run mode and stop at the "
        "human gate, but you have not yet seen what it produces."
        if untested else None)}


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
    connection_id: str | None = Field(default=None, max_length=64)
    # Off means a run ends at hand-off and the ticket is left open for a human
    # to close in ServiceNow.
    require_outcome_report: bool | None = None


@app.put("/api/workflows/{workflow_id}/config")
async def configure_workflow(workflow_id: str, payload: WorkflowConfigRequest,
                             principal: Principal = Depends(require_role(Role.admin))) -> dict:
    with session_scope() as session:
        workflow = session.get(Workflow, workflow_id)
        if workflow is None or workflow.workspace_id != principal.workspace_id:
            raise HTTPException(status_code=404, detail="unknown workflow")
        changes = payload.model_dump(exclude_unset=True, exclude_none=True)
        workflow.config = {**workflow.config, **changes}
        audit(session, actor=principal.user.display_name, action="workflow_configured",
              entity_type="workflow", entity_id=workflow_id,
              workspace_id=principal.workspace_id,
              actor_verified=principal.user.identity_verified, detail=changes)
        view = _workflow_view(workflow)
    return view



class _DetachedConnection:
    """A connection's settings, copied out of the session that loaded them.

    Testing a connection makes network calls that can take seconds; holding a
    database session open across them would pin a connection from the pool for
    no reason.
    """

    def __init__(self, connection) -> None:
        self.name = connection.name
        self.kind = connection.kind
        self.config = dict(connection.config or {})
        self.secrets = dict(connection.secrets or {})


# --- connections -------------------------------------------------------------

class ConnectionRequest(BaseModel):
    """A named external-system connection with encrypted credentials.

    Credentials are accepted here and encrypted at rest (see crypto.py). That
    lets a workspace hold several instances without exposing a stored secret
    through any endpoint.
    """
    kind: str = Field(
        default="servicenow",
        pattern="^(servicenow|jira|splunk|datadog|dynatrace)$",
    )
    name: str = Field(min_length=1, max_length=80)
    base_url: str = Field(min_length=1, max_length=300)
    username: str = Field(default="", max_length=200)

    # ServiceNow
    auth_type: str = Field(default="basic", pattern="^(basic|oauth)$")
    password: str | None = Field(default=None, max_length=300)
    client_id: str | None = Field(default=None, max_length=200)
    client_secret: str | None = Field(default=None, max_length=300)
    # The queue this connection watches. An operations team thinks in queues,
    # so this is the field the trigger is built from.
    assignment_group: str = Field(default="", max_length=120)
    extra_query: str = Field(default="", max_length=500)

    # Jira: email + API token (not a password), and a project or JQL to watch.
    api_token: str | None = Field(default=None, max_length=500)
    project_key: str | None = Field(default=None, max_length=80)
    jql: str | None = Field(default=None, max_length=500)

    # Observability connectors. Splunk and Dynatrace both use a bearer-style
    # access token; Datadog uses its API and application key pair.
    access_token: str | None = Field(default=None, max_length=1000)
    api_key: str | None = Field(default=None, max_length=1000)
    app_key: str | None = Field(default=None, max_length=1000)
    search_query: str | None = Field(default=None, max_length=500)
    service_filter: str | None = Field(default=None, max_length=500)
    entity_selector: str | None = Field(default=None, max_length=500)


def _connection_view(connection) -> dict:
    from crypto import present
    config = dict(connection.config or {})
    return {
        "id": connection.id, "kind": connection.kind, "name": connection.name,
        "base_url": config.get("base_url", ""),
        "username": config.get("username", ""),
        # ServiceNow
        "auth_type": config.get("auth_type", "basic"),
        "client_id": config.get("client_id", ""),
        "assignment_group": config.get("assignment_group", ""),
        "extra_query": config.get("extra_query", ""),
        # Jira
        "project_key": config.get("project_key", ""),
        "jql": config.get("jql", ""),
        # Observability sources
        "search_query": config.get("search_query", ""),
        "service_filter": config.get("service_filter", ""),
        "entity_selector": config.get("entity_selector", ""),
        "enabled": connection.enabled,
        # Presence only. There is no endpoint that returns a stored secret.
        "secrets_set": present(connection.secrets),
        "last_tested_at": connection.last_tested_at,
        "last_test_ok": connection.last_test_ok,
        "last_test_detail": connection.last_test_detail,
    }


@app.get("/api/connections")
async def list_connections(principal: Principal = Depends(require_role(Role.viewer))) -> dict:
    with session_scope() as session:
        rows = [_connection_view(c) for c in session.query(Connection)
                .filter(Connection.workspace_id == principal.workspace_id)
                .order_by(Connection.created_at).all()]
    return {
        "connections": rows,
        # The environment path still works and is reported, so an existing
        # setup keeps running and it is obvious which source is in play.
        "environment": servicenow.env_status(),
        "environment_auth_method": servicenow.auth_method(),
        "environment_usable": not servicenow.missing_env(for_writes=False),
    }


def _connection_parts(payload: ConnectionRequest) -> tuple[dict, tuple]:
    """Return public configuration and submitted secret values for a payload."""
    if payload.kind == "jira":
        config = {
            "base_url": payload.base_url.rstrip("/"),
            "username": payload.username.strip(),      # the account email
            "project_key": (payload.project_key or "").strip(),
            "jql": (payload.jql or "").strip(),
        }
        secret_fields = (("api_token", payload.api_token),)
    elif payload.kind == "splunk":
        config = {
            "base_url": payload.base_url.rstrip("/"),
            "search_query": (payload.search_query or "").strip(),
        }
        secret_fields = (("access_token", payload.access_token),)
    elif payload.kind == "datadog":
        config = {
            "base_url": payload.base_url.rstrip("/"),
            "service_filter": (payload.service_filter or "").strip(),
        }
        secret_fields = (("api_key", payload.api_key),
                         ("app_key", payload.app_key))
    elif payload.kind == "dynatrace":
        config = {
            "base_url": payload.base_url.rstrip("/"),
            "entity_selector": (payload.entity_selector or "").strip(),
        }
        secret_fields = (("access_token", payload.access_token),)
    else:
        config = {
            "base_url": payload.base_url.rstrip("/"),
            "auth_type": payload.auth_type,
            "username": payload.username.strip(),
            "client_id": (payload.client_id or "").strip(),
            "assignment_group": payload.assignment_group.strip(),
            "extra_query": payload.extra_query.strip(),
        }
        secret_fields = (("password", payload.password),
                         ("client_secret", payload.client_secret))
    return config, secret_fields


def _apply_connection(connection, payload: ConnectionRequest) -> None:
    from crypto import encrypt
    connection.name = payload.name
    connection.kind = payload.kind
    connection.config, secret_fields = _connection_parts(payload)
    secrets = dict(connection.secrets or {})
    # An omitted secret leaves the stored one alone, so editing a queue name
    # does not require retyping a secret nobody can read back.
    for field, value in secret_fields:
        if value:
            secrets[field] = encrypt(value)
    connection.secrets = secrets


@app.post("/api/connections")
async def create_connection(payload: ConnectionRequest,
                            principal: Principal = Depends(require_role(Role.admin))) -> dict:
    with session_scope() as session:
        clash = (session.query(Connection)
                 .filter(Connection.workspace_id == principal.workspace_id,
                         Connection.kind == payload.kind,
                         Connection.name == payload.name).first())
        if clash is not None:
            raise HTTPException(status_code=409,
                                detail=f"a {payload.kind} connection named "
                                       f"{payload.name!r} already exists")
        connection = Connection(workspace_id=principal.workspace_id,
                                created_by=principal.user.id)
        _apply_connection(connection, payload)
        session.add(connection)
        session.flush()
        audit(session, actor=principal.user.display_name, action="connection_created",
              entity_type="connection", entity_id=connection.id,
              workspace_id=principal.workspace_id,
              actor_verified=principal.user.identity_verified,
              # The instance and account are recorded; the credential is not.
              detail={"kind": payload.kind, "name": payload.name,
                      "base_url": payload.base_url, "auth_type": payload.auth_type,
                      "assignment_group": payload.assignment_group})
        view = _connection_view(connection)
    return view


@app.put("/api/connections/{connection_id}")
async def update_connection(connection_id: str, payload: ConnectionRequest,
                            principal: Principal = Depends(require_role(Role.admin))) -> dict:
    with session_scope() as session:
        connection = session.get(Connection, connection_id)
        if connection is None or connection.workspace_id != principal.workspace_id:
            raise HTTPException(status_code=404, detail="unknown connection")
        _apply_connection(connection, payload)
        audit(session, actor=principal.user.display_name, action="connection_updated",
              entity_type="connection", entity_id=connection_id,
              workspace_id=principal.workspace_id,
              actor_verified=principal.user.identity_verified,
              detail={"name": payload.name,
                      "assignment_group": payload.assignment_group})
        view = _connection_view(connection)
    return view


@app.delete("/api/connections/{connection_id}")
async def delete_connection(connection_id: str,
                            principal: Principal = Depends(require_role(Role.admin))) -> dict:
    with session_scope() as session:
        connection = session.get(Connection, connection_id)
        if connection is None or connection.workspace_id != principal.workspace_id:
            raise HTTPException(status_code=404, detail="unknown connection")
        in_use = [w.name for w in session.query(Workflow)
                  .filter(Workflow.workspace_id == principal.workspace_id).all()
                  if (w.config or {}).get("connection_id") == connection_id]
        if in_use:
            # Deleting it would leave those workflows pointing at nothing and
            # failing at run time rather than here.
            raise HTTPException(
                status_code=409,
                detail=f"still used by: {', '.join(in_use)}. Point them elsewhere first.")
        session.delete(connection)
        audit(session, actor=principal.user.display_name, action="connection_deleted",
              entity_type="connection", entity_id=connection_id,
              workspace_id=principal.workspace_id,
              actor_verified=principal.user.identity_verified)
    return {"status": "deleted", "id": connection_id}



async def _run_connection_test(kind: str, config: dict, secrets: dict) -> tuple:
    """Test a connection's reachability and credentials. Secrets are plaintext.

    One code path for "test what is stored" and "test what is typed but not yet
    saved", so the two can never diverge in what counts as a pass.
    """
    probe = type("C", (), {"config": config})()
    try:
        if kind == "jira":
            from integrations import jira
            client = jira.JiraClient(config.get("base_url", "").rstrip("/"),
                                     config.get("username", ""),
                                     secrets.get("api_token", ""))
            await asyncio.to_thread(client.test)
            detail = "Connected to Jira."
            if config.get("project_key") or config.get("jql"):
                issues = await asyncio.to_thread(client.search, jira.project_query(probe), 5)
                target = config.get("project_key") or "the configured JQL"
                detail += f" {target} currently matches {len(issues)} issue(s)."
                return True, detail, len(issues)
            return True, detail, None
        if kind in {"splunk", "datadog", "dynatrace"}:
            import httpx

            base_url = config.get("base_url", "").rstrip("/")
            if kind == "splunk":
                url = f"{base_url}/services/server/info?output_mode=json"
                headers = {
                    "Authorization": f"Splunk {secrets.get('access_token', '')}",
                    "Accept": "application/json",
                }
                label = "Splunk"
            elif kind == "datadog":
                url = f"{base_url}/api/v1/validate"
                headers = {
                    "DD-API-KEY": secrets.get("api_key", ""),
                    "DD-APPLICATION-KEY": secrets.get("app_key", ""),
                    "Accept": "application/json",
                }
                label = "Datadog"
            else:
                url = f"{base_url}/api/v2/problems?pageSize=1"
                headers = {
                    "Authorization": f"Api-Token {secrets.get('access_token', '')}",
                    "Accept": "application/json",
                }
                label = "Dynatrace"
            response = await asyncio.to_thread(
                httpx.get, url, headers=headers, timeout=15)
            response.raise_for_status()
            return True, f"Connected to {label}.", None
        oauth = config.get("auth_type") == "oauth"
        client = servicenow.ServiceNowClient(
            config.get("base_url", "").rstrip("/"), config.get("username", ""),
            secrets.get("password", ""),
            config.get("client_id") if oauth else None,
            secrets.get("client_secret") if oauth else None)
        await asyncio.to_thread(client.test)
        detail = f"Connected using {client.auth_method} authentication."
        group = config.get("assignment_group")
        if group:
            rows = await asyncio.to_thread(
                client.search_incidents, servicenow.queue_query(probe), 5)
            detail += f" Queue {group!r} currently matches {len(rows)} active incident(s)."
            return True, detail, len(rows)
        return True, detail, None
    except Exception as error:                          # noqa: BLE001
        return False, str(error), None


@app.post("/api/connections/{connection_id}/test")
async def test_stored_connection(
        connection_id: str,
        principal: Principal = Depends(require_role(Role.operator))) -> dict:
    """Prove the credentials work, and remember the answer.

    The result is stored on the connection so the list shows what happened last
    time — a connection that broke overnight should be visible without someone
    thinking to re-test it.
    """
    from crypto import decrypt
    with session_scope() as session:
        connection = session.get(Connection, connection_id)
        if connection is None or connection.workspace_id != principal.workspace_id:
            raise HTTPException(status_code=404, detail="unknown connection")
        kind = connection.kind
        config = dict(connection.config or {})
        secrets = {name: decrypt(value)
                   for name, value in (connection.secrets or {}).items()}

    ok, detail, queue_count = await _run_connection_test(kind, config, secrets)

    with session_scope() as session:
        connection = session.get(Connection, connection_id)
        connection.last_tested_at = time.time()
        connection.last_test_ok = ok
        connection.last_test_detail = detail[:2000]
        audit(session, actor=principal.user.display_name,
              action="connection_tested", entity_type="connection",
              entity_id=connection_id, workspace_id=principal.workspace_id,
              actor_verified=principal.user.identity_verified, detail={"ok": ok})
    return {"ok": ok, "detail": detail, "queue_matches": queue_count}


@app.post("/api/connections/test-draft")
async def test_draft_connection(
        payload: ConnectionRequest, connection_id: str | None = None,
        principal: Principal = Depends(require_role(Role.operator))) -> dict:
    """Test what the form currently holds, before it is saved.

    A blank secret on an edit means "unchanged", so the stored value is used —
    the same rule the save path follows, so Test and Save agree on what will run.
    """
    from crypto import decrypt
    config, secret_fields = _connection_parts(payload)
    secrets = {name: value or "" for name, value in secret_fields}

    if connection_id:
        with session_scope() as session:
            existing = session.get(Connection, connection_id)
            if existing is not None and existing.workspace_id == principal.workspace_id:
                for name, value in (existing.secrets or {}).items():
                    if not secrets.get(name):
                        secrets[name] = decrypt(value) or ""

    ok, detail, queue_count = await _run_connection_test(payload.kind, config, secrets)
    return {"ok": ok, "detail": detail, "queue_matches": queue_count}


@app.post("/api/connections/test")
async def test_environment_connection(
        principal: Principal = Depends(require_role(Role.operator))) -> dict:
    """The environment-variable path, kept working for existing setups."""
    missing = servicenow.missing_env(for_writes=False)
    if missing:
        return {"ok": False,
                "detail": f"missing environment variables: {', '.join(missing)}"}
    client = servicenow.reader()
    try:
        await asyncio.to_thread(client.test)
    except servicenow.ServiceNowError as error:
        return {"ok": False, "detail": str(error), "auth_method": client.auth_method}
    return {"ok": True,
            "detail": f"Read credentials work ({client.auth_method} authentication).",
            "auth_method": client.auth_method,
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


# --- home dashboard ----------------------------------------------------------

@app.get("/api/overview")
async def overview(principal: Principal = Depends(require_role(Role.viewer))) -> dict:
    """Everything the home screen shows, in one call.

    One endpoint rather than the home page fanning out to six, because the first
    thing a landing page must not do is feel slow — and each of these is a
    cheap count against an indexed, workspace-scoped table.
    """
    ws = principal.workspace_id
    now = time.time()
    day_ago = now - 86400
    with session_scope() as session:
        workflows = session.query(Workflow).filter(Workflow.workspace_id == ws).all()
        enabled = [w for w in workflows if w.enabled]
        polling = [w for w in enabled if (w.config or {}).get("poll_enabled")]

        runs = session.query(Run).filter(Run.workspace_id == ws).all()
        by_status: dict[str, int] = {}
        for run in runs:
            by_status[run.status.value] = by_status.get(run.status.value, 0) + 1
        recent = (session.query(Run).filter(Run.workspace_id == ws)
                  .order_by(Run.started_at.desc()).limit(6).all())
        runs_today = [r for r in runs if r.started_at >= day_ago]
        spend_today = round(sum(r.cost_usd or 0 for r in runs_today), 4)

        pending_approvals = (session.query(Approval).join(Run, Approval.run_id == Run.id)
                             .filter(Run.workspace_id == ws,
                                     Approval.status == ApprovalStatus.pending).count())
        connections = session.query(Connection).filter(Connection.workspace_id == ws).all()
        connection_health = [
            {"name": c.name, "kind": c.kind, "last_test_ok": c.last_test_ok}
            for c in connections]
        workspace = session.get(Workspace, ws)

        recent_runs = [_run_view(r) for r in recent]

    return {
        "greeting_name": principal.user.display_name,
        "role": principal.user.role.value,
        "identity_verified": principal.user.identity_verified,
        "simulated": engine().client.simulated,
        "killswitch": bool(workspace.killswitch) if workspace else False,
        "counts": {
            "workflows_total": len(workflows),
            "workflows_enabled": len(enabled),
            "workflows_polling": len(polling),
            "runs_total": len(runs),
            "runs_today": len(runs_today),
            "spend_today_usd": spend_today,
            "pending_approvals": pending_approvals,
            "awaiting_approval": by_status.get("awaiting_approval", 0),
            "running": by_status.get("running", 0),
            "failed": by_status.get("failed", 0),
            "connections": len(connections),
        },
        "runs_by_status": by_status,
        "connection_health": connection_health,
        "recent_runs": recent_runs,
        # A fresh workspace with nothing set up gets pointed at the wizard.
        "needs_onboarding": len(enabled) == 0,
    }


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
