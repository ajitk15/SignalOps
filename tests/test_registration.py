"""Administrator-approved registration and its security boundaries."""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _fresh_db(case: unittest.TestCase) -> None:
    """Point the application's session factory at a throwaway SQLite file."""
    import db as db_module
    from models import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    temp = tempfile.TemporaryDirectory()
    engine = create_engine(
        f"sqlite:///{Path(temp.name) / 'test.db'}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    original_engine = db_module.engine
    original_sessionlocal = db_module.SessionLocal
    db_module.engine = engine
    db_module.SessionLocal = sessionmaker(
        bind=engine, expire_on_commit=False, future=True,
    )

    def _restore() -> None:
        db_module.engine = original_engine
        db_module.SessionLocal = original_sessionlocal

    case.addCleanup(temp.cleanup)
    case.addCleanup(engine.dispose)
    case.addCleanup(_restore)


class AccessRequestValidationTests(unittest.TestCase):
    def test_request_normalises_identity_and_rejects_blank_values(self):
        from pydantic import ValidationError
        from server.app import AccessRequestPayload

        payload = AccessRequestPayload(
            email="  Ada@Example.COM ",
            display_name="  Ada Lovelace  ",
            password="a-sufficiently-long-password",
        )
        self.assertEqual(payload.email, "ada@example.com")
        self.assertEqual(payload.display_name, "Ada Lovelace")

        with self.assertRaises(ValidationError):
            AccessRequestPayload(
                email="not-an-email",
                display_name="Ada",
                password="a-sufficiently-long-password",
            )
        with self.assertRaises(ValidationError):
            AccessRequestPayload(
                email="ada@example.com",
                display_name="   ",
                password="a-sufficiently-long-password",
            )


class RegistrationLifecycleTests(unittest.TestCase):
    def setUp(self):
        _fresh_db(self)
        import server.app as app_module
        from auth import Principal, hash_password
        from db import session_scope
        from models import Role, User, Workspace

        self.app_module = app_module
        self.original_workspace_id = app_module.WORKSPACE_ID
        self.addCleanup(
            lambda: setattr(app_module, "WORKSPACE_ID", self.original_workspace_id)
        )
        with session_scope() as session:
            workspace = Workspace(name="registration tests")
            session.add(workspace)
            session.flush()
            admin = User(
                workspace_id=workspace.id,
                email="admin@example.com",
                display_name="Admin",
                role=Role.admin,
                password_hash=hash_password("admin-test-password"),
                identity_verified=True,
            )
            session.add(admin)
            session.flush()
            self.workspace_id = workspace.id
            self.admin_id = admin.id
            self.principal = Principal(admin, workspace)
        app_module.WORKSPACE_ID = self.workspace_id

    def _request(self, *, email="new.user@example.com",
                 password="applicant-owned-password"):
        payload = self.app_module.AccessRequestPayload(
            email=email,
            display_name="New User",
            password=password,
        )
        with patch.dict(
            os.environ, {"SIGNALOPS_REGISTRATION_ENABLED": "true"}, clear=False
        ):
            return asyncio.run(self.app_module.request_access(payload))

    def _request_row(self, email="new.user@example.com"):
        from db import session_scope
        from models import RegistrationRequest

        with session_scope() as session:
            return (
                session.query(RegistrationRequest)
                .filter(
                    RegistrationRequest.workspace_id == self.workspace_id,
                    RegistrationRequest.email == email,
                )
                .one()
            )

    def test_registration_is_disabled_by_default(self):
        from fastapi import HTTPException

        payload = self.app_module.AccessRequestPayload(
            email="new.user@example.com",
            display_name="New User",
            password="applicant-owned-password",
        )
        with patch.dict(
            os.environ, {"SIGNALOPS_REGISTRATION_ENABLED": "false"}, clear=False
        ):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(self.app_module.request_access(payload))
        self.assertEqual(caught.exception.status_code, 404)

    def test_request_is_pending_hashed_and_cannot_sign_in(self):
        from auth import InvalidCredentials, PasswordAuthProvider
        from db import session_scope
        from models import RegistrationRequest, RegistrationStatus, User

        result = self._request()
        self.assertEqual(result["status"], "pending")
        self.assertNotIn("password", repr(result).lower())

        with session_scope() as session:
            request = session.query(RegistrationRequest).one()
            self.assertEqual(request.status, RegistrationStatus.pending)
            self.assertTrue(request.password_hash.startswith("$argon2"))
            self.assertNotIn("applicant-owned-password", request.password_hash)
            self.assertEqual(
                session.query(User)
                .filter(User.workspace_id == self.workspace_id,
                        User.email == "new.user@example.com")
                .count(),
                0,
            )
            with self.assertRaises(InvalidCredentials):
                PasswordAuthProvider().login(
                    session,
                    self.workspace_id,
                    email="new.user@example.com",
                    password="applicant-owned-password",
                )

    def test_duplicate_pending_request_is_generic_and_cannot_replace_password(self):
        from auth import InvalidCredentials, PasswordAuthProvider
        from db import session_scope

        first = self._request()
        second = self._request(
            email="NEW.USER@EXAMPLE.COM",
            password="attacker-replacement-password",
        )
        self.assertEqual(first, second)

        request = self._request_row()
        approval = self.app_module.RegistrationApprovalRequest(role="viewer")
        asyncio.run(
            self.app_module.approve_registration_request(
                request.id, approval, self.principal
            )
        )
        with session_scope() as session:
            PasswordAuthProvider().login(
                session,
                self.workspace_id,
                email="new.user@example.com",
                password="applicant-owned-password",
            )
            with self.assertRaises(InvalidCredentials):
                PasswordAuthProvider().login(
                    session,
                    self.workspace_id,
                    email="new.user@example.com",
                    password="attacker-replacement-password",
                )

    def test_admin_approval_creates_one_account_with_assigned_role(self):
        from auth import PasswordAuthProvider
        from db import session_scope
        from fastapi import HTTPException
        from models import RegistrationStatus, Role, User

        self._request()
        request = self._request_row()
        approval = self.app_module.RegistrationApprovalRequest(
            role=Role.operator,
            notify_applicant=True,
            note="On-call engineer",
        )
        result = asyncio.run(
            self.app_module.approve_registration_request(
                request.id, approval, self.principal
            )
        )

        self.assertNotIn("password_hash", repr(result))
        self.assertEqual(result["request"]["status"], "approved")
        self.assertEqual(result["request"]["notification_status"], "not_configured")
        self.assertEqual(result["user"]["role"], "operator")
        self.assertFalse(result["user"]["must_change_password"])

        with session_scope() as session:
            user = (
                session.query(User)
                .filter(User.workspace_id == self.workspace_id,
                        User.email == "new.user@example.com")
                .one()
            )
            self.assertEqual(user.role, Role.operator)
            PasswordAuthProvider().login(
                session,
                self.workspace_id,
                email=user.email,
                password="applicant-owned-password",
            )
            self.assertEqual(
                session.query(User)
                .filter(User.workspace_id == self.workspace_id,
                        User.email == user.email)
                .count(),
                1,
            )

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(
                self.app_module.approve_registration_request(
                    request.id, approval, self.principal
                )
            )
        self.assertEqual(caught.exception.status_code, 409)

        reviewed = self._request_row()
        self.assertEqual(reviewed.status, RegistrationStatus.approved)
        self.assertEqual(reviewed.reviewed_by, self.admin_id)
        self.assertIsNone(reviewed.password_hash)

    def test_rejection_creates_no_account_and_records_notification_limit(self):
        from db import session_scope
        from models import RegistrationStatus, User

        self._request()
        request = self._request_row()
        rejection = self.app_module.RegistrationReviewRequest(
            notify_applicant=True,
            note="Not currently eligible",
        )
        result = asyncio.run(
            self.app_module.reject_registration_request(
                request.id, rejection, self.principal
            )
        )

        self.assertEqual(result["request"]["status"], "rejected")
        self.assertEqual(result["request"]["notification_status"], "not_configured")
        with session_scope() as session:
            self.assertEqual(
                session.query(User)
                .filter(User.workspace_id == self.workspace_id,
                        User.email == "new.user@example.com")
                .count(),
                0,
            )
            self.assertEqual(
                session.get(type(request), request.id).status,
                RegistrationStatus.rejected,
            )
            self.assertIsNone(
                session.get(type(request), request.id).password_hash
            )

    def test_existing_account_gets_the_same_generic_public_response(self):
        from auth import hash_password
        from db import session_scope
        from models import Role, User

        with session_scope() as session:
            session.add(User(
                workspace_id=self.workspace_id,
                email="existing@example.com",
                display_name="Existing",
                role=Role.viewer,
                password_hash=hash_password("existing-user-password"),
            ))
        existing = self._request(
            email="existing@example.com",
            password="unrelated-request-password",
        )
        fresh = self._request(
            email="fresh@example.com",
            password="unrelated-request-password",
        )
        self.assertEqual(existing, fresh)

    def test_registration_review_routes_are_admin_only(self):
        from auth import SESSION_COOKIE, hash_password, issue_session
        from db import session_scope
        from fastapi.testclient import TestClient
        from models import Role, User

        self._request()
        with session_scope() as session:
            viewer = User(
                workspace_id=self.workspace_id,
                email="viewer@example.com",
                display_name="Viewer",
                role=Role.viewer,
                password_hash=hash_password("viewer-test-password"),
                identity_verified=True,
            )
            session.add(viewer)
            session.flush()
            token = issue_session(viewer)

        client = TestClient(self.app_module.app, base_url="https://testserver")
        client.cookies.set(SESSION_COOKIE, token)
        response = client.get("/api/registration-requests")
        self.assertEqual(response.status_code, 403)

    def test_admin_cannot_bypass_a_pending_request_with_a_manual_invite(self):
        from fastapi import HTTPException
        from models import Role

        self._request()
        invite = self.app_module.UserRequest(
            email="new.user@example.com",
            display_name="New User",
            role=Role.viewer,
            password="administrator-chosen-password",
        )
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(self.app_module.create_user(invite, self.principal))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("approve it instead", caught.exception.detail)


if __name__ == "__main__":
    unittest.main()
