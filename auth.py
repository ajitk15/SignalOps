"""Authentication and authorisation.

The split that matters here: **authentication is a stub, authorisation is real.**
DummyAuthProvider accepts whoever you say you are, but every route still checks
roles and workspace scope server-side. That way swapping in OIDC later replaces
one class and changes nothing downstream — and the permission model is exercised
from day one rather than bolted on once it is load-bearing.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Protocol

from fastapi import Cookie, Depends, HTTPException
from itsdangerous import BadSignature, URLSafeSerializer

from db import audit, session_scope
from models import ROLE_RANK, Role, User, Workspace

logger = logging.getLogger("auth")

SESSION_COOKIE = "signalops_session"
# Dev-only default: a fixed secret is fine while identity itself is a stub, and
# a real provider will bring a real secret with it.
_SECRET = os.getenv("SIGNALOPS_SESSION_SECRET", "dev-only-not-a-real-secret")
_serializer = URLSafeSerializer(_SECRET, salt="signalops-session")


class AuthProvider(Protocol):
    """The seam OIDC slots into later."""
    name: str
    verifies_identity: bool

    def login(self, session, workspace_id: str, **claims) -> User: ...


class DummyAuthProvider:
    """Accepts any name and role. No password, no verification.

    Exists so sessions, roles, onboarding and audit are real and exercised while
    the product is built. The server refuses to start outside SIGNALOPS_ENV=local
    precisely because this class is in the loop.
    """
    name = "dummy"
    verifies_identity = False

    def login(self, session, workspace_id: str, *, display_name: str,
              role: Role = Role.operator, email: str | None = None) -> User:
        email = email or f"{display_name.strip().lower().replace(' ', '.')}@local"
        user = (session.query(User)
                .filter(User.workspace_id == workspace_id, User.email == email)
                .one_or_none())
        if user is None:
            user = User(workspace_id=workspace_id, email=email,
                        display_name=display_name.strip(), role=role,
                        identity_verified=False)
            session.add(user)
            session.flush()
        else:
            # Role is re-asserted each login because there is no directory to
            # read it from; with a real provider this comes from the token.
            user.role = role
            user.display_name = display_name.strip()
        user.last_login_at = time.time()
        return user


_provider: AuthProvider = DummyAuthProvider()


def provider() -> AuthProvider:
    return _provider


def issue_session(user: User) -> str:
    return _serializer.dumps({"uid": user.id, "ws": user.workspace_id})


def _load_session(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        return _serializer.loads(raw)
    except BadSignature:
        return None


class Principal:
    """The authenticated caller, with the workspace already resolved."""

    def __init__(self, user: User, workspace: Workspace):
        self.user = user
        self.workspace = workspace

    @property
    def workspace_id(self) -> str:
        return self.workspace.id

    def can(self, minimum: Role) -> bool:
        return ROLE_RANK[self.user.role] >= ROLE_RANK[minimum]

    def as_dict(self) -> dict:
        return {"id": self.user.id, "display_name": self.user.display_name,
                "email": self.user.email, "role": self.user.role.value,
                # Surfaced so the UI can say plainly that identity is unverified.
                "identity_verified": self.user.identity_verified,
                "auth_provider": _provider.name,
                "workspace": {"id": self.workspace.id, "name": self.workspace.name,
                              "killswitch": self.workspace.killswitch}}


def current_principal(signalops_session: str | None = Cookie(default=None)) -> Principal:
    data = _load_session(signalops_session)
    if not data:
        raise HTTPException(status_code=401, detail="not authenticated")
    with session_scope() as session:
        user = session.get(User, data.get("uid"))
        workspace = session.get(Workspace, data.get("ws"))
        if user is None or workspace is None or user.workspace_id != workspace.id:
            raise HTTPException(status_code=401, detail="session no longer valid")
        return Principal(user, workspace)


def require_role(minimum: Role):
    """Route dependency enforcing a minimum role.

    Enforced server-side regardless of what the UI shows — hiding a button is
    presentation, not authorisation.
    """
    def dependency(principal: Principal = Depends(current_principal)) -> Principal:
        if not principal.can(minimum):
            raise HTTPException(
                status_code=403,
                detail=f"requires {minimum.value}; you are {principal.user.role.value}")
        return principal
    return dependency


def scoped(principal: Principal, obj_workspace_id: str) -> None:
    """Reject cross-workspace access as 404, not 403.

    A 403 confirms the resource exists in someone else's workspace; 404 does
    not leak that.
    """
    if obj_workspace_id != principal.workspace_id:
        raise HTTPException(status_code=404, detail="not found")


def record_login(session, user: User, workspace_id: str) -> None:
    audit(session, actor=user.display_name, action="user_login", entity_type="user",
          entity_id=user.id, workspace_id=workspace_id,
          actor_verified=user.identity_verified,
          detail={"role": user.role.value, "provider": _provider.name})
