"""Authentication and authorisation.

Authentication is now real: an email address and an Argon2-hashed password.
Authorisation always was — every route checks roles and workspace scope
server-side, which is why swapping the provider changed nothing downstream. The
`AuthProvider` seam stays, so OIDC replaces one class when it arrives.

Argon2id rather than bcrypt or PBKDF2: it is the current password-hashing
competition winner and is memory-hard, which is the property that matters
against the hardware an attacker actually rents. The library picks parameters
and re-hashes on login when they change, so raising the cost later needs no
migration.

Two behaviours worth knowing. Login does not say whether it was the email or
the password that was wrong, because "no such user" is an account-enumeration
oracle. And repeated failures lock an account briefly rather than forever — a
permanent lock is a denial-of-service anyone can trigger by guessing at
somebody else's email.
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


MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 300
MIN_PASSWORD_LENGTH = 10


class InvalidCredentials(Exception):
    """Wrong email, wrong password, inactive or locked — deliberately one type.

    Distinguishing them to the caller is an enumeration oracle: an attacker
    with a list of addresses learns which ones exist without ever logging in.
    """


class PasswordPolicy(Exception):
    """The proposed password is not acceptable."""


def hash_password(password: str) -> str:
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise PasswordPolicy(
            f"Use at least {MIN_PASSWORD_LENGTH} characters. Length is what makes a "
            "password expensive to guess; composition rules mostly make it hard to "
            "remember.")
    return _hasher().hash(password)


def _hasher():
    from argon2 import PasswordHasher
    global _password_hasher
    if _password_hasher is None:
        _password_hasher = PasswordHasher()
    return _password_hasher


_password_hasher = None


class PasswordAuthProvider:
    """Email and password, checked against an Argon2 hash."""

    name = "password"
    verifies_identity = True

    def login(self, session, workspace_id: str, *, email: str,
              password: str, **_) -> User:
        from argon2.exceptions import InvalidHashError, VerifyMismatchError

        email = (email or "").strip().lower()
        user = (session.query(User)
                .filter(User.workspace_id == workspace_id, User.email == email)
                .one_or_none())

        if user is None:
            # Spend roughly the time a real verification costs, so a missing
            # account is not detectable by how fast the answer comes back.
            _hasher().hash("timing-equalisation")
            raise InvalidCredentials("email or password is incorrect")
        if not user.active:
            raise InvalidCredentials("email or password is incorrect")
        if user.locked_until and time.time() < user.locked_until:
            remaining = int(user.locked_until - time.time())
            raise InvalidCredentials(
                f"too many failed attempts; try again in {remaining} seconds")
        if not user.password_hash:
            raise InvalidCredentials("email or password is incorrect")

        try:
            _hasher().verify(user.password_hash, password or "")
        except (VerifyMismatchError, InvalidHashError):
            user.failed_logins = (user.failed_logins or 0) + 1
            if user.failed_logins >= MAX_FAILED_LOGINS:
                # Temporary, not permanent: a lock that never lifts is a denial
                # of service anyone can trigger against anyone else's address.
                user.locked_until = time.time() + LOCKOUT_SECONDS
                user.failed_logins = 0
                logger.warning("locked %s after repeated failed logins", email)
            raise InvalidCredentials("email or password is incorrect")

        if _hasher().check_needs_rehash(user.password_hash):
            # Parameters were raised since this hash was made; upgrade it now
            # that the plaintext is in hand, which is the only moment it can be.
            user.password_hash = _hasher().hash(password)
        user.failed_logins = 0
        user.locked_until = None
        user.identity_verified = True
        user.last_login_at = time.time()
        return user


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


# Password auth is the default now. SIGNALOPS_AUTH=dummy brings back the stub
# for a demo, and the startup tripwire still refuses to run it outside local.
_provider: AuthProvider = (DummyAuthProvider() if os.getenv("SIGNALOPS_AUTH") == "dummy"
                           else PasswordAuthProvider())


def provider() -> AuthProvider:
    return _provider


def set_password(user: User, password: str) -> None:
    user.password_hash = hash_password(password)
    user.must_change_password = False
    user.failed_logins = 0
    user.locked_until = None


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
                "must_change_password": bool(self.user.must_change_password),
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
        if not user.active:
            # Deactivation has to take effect on the next request, not the next
            # login, or revoking access means nothing while a session is open.
            raise HTTPException(status_code=401, detail="this account is deactivated")
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
