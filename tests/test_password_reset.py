"""Password recovery, including enumeration and one-time-token boundaries."""
import asyncio
import hashlib
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import BackgroundTasks, HTTPException, Response  # noqa: E402


def _fresh_db(case: unittest.TestCase) -> None:
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


class PasswordResetLifecycleTests(unittest.TestCase):
    def setUp(self):
        _fresh_db(self)
        import server.app as app_module
        from auth import hash_password, issue_session
        from db import session_scope
        from models import Role, User, Workspace

        self.app_module = app_module
        self.original_workspace_id = app_module.WORKSPACE_ID
        self.addCleanup(
            lambda: setattr(app_module, "WORKSPACE_ID", self.original_workspace_id)
        )
        with session_scope() as session:
            workspace = Workspace(name="password reset tests")
            session.add(workspace)
            session.flush()
            user = User(
                workspace_id=workspace.id,
                email="ada@example.com",
                display_name="Ada",
                role=Role.operator,
                password_hash=hash_password("original-password"),
                active=True,
                identity_verified=True,
            )
            session.add(user)
            session.flush()
            self.workspace_id = workspace.id
            self.user_id = user.id
            self.old_session = issue_session(user)
        app_module.WORKSPACE_ID = self.workspace_id

    def _request(self, email="ada@example.com"):
        background = BackgroundTasks()
        payload = self.app_module.ForgotPasswordRequest(email=email)
        with patch.object(
                self.app_module, "password_reset_delivery_available",
                return_value=True), patch.object(
                    self.app_module, "send_password_reset_email") as sender:
            result = asyncio.run(
                self.app_module.forgot_password(payload, background)
            )
            asyncio.run(background())
        return result, sender

    def test_known_and_unknown_addresses_receive_the_same_public_response(self):
        known, sender = self._request()
        unknown, unknown_sender = self._request("nobody@example.com")
        self.assertEqual(known, unknown)
        self.assertEqual(known["message"], self.app_module.PASSWORD_RESET_RESPONSE)
        sender.assert_called_once()
        unknown_sender.assert_not_called()

    def test_only_a_digest_is_stored_and_the_link_is_delivered_in_background(self):
        from db import session_scope
        from models import PasswordResetToken

        _, sender = self._request()
        raw_token = sender.call_args.kwargs["token"]
        with session_scope() as session:
            stored = session.query(PasswordResetToken).one()
            self.assertNotEqual(stored.token_hash, raw_token)
            self.assertEqual(
                stored.token_hash,
                hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
            )
            self.assertGreater(stored.expires_at, time.time())

    def test_a_token_resets_once_and_revokes_older_sessions(self):
        from auth import current_principal, provider
        from db import session_scope

        _, sender = self._request()
        raw_token = sender.call_args.kwargs["token"]
        payload = self.app_module.PasswordResetCompletion(
            token=raw_token,
            new_password="replacement-password",
        )
        result = asyncio.run(
            self.app_module.reset_password(payload, Response())
        )
        self.assertEqual(result["status"], "changed")

        with session_scope() as session:
            user = provider().login(
                session, self.workspace_id,
                email="ada@example.com",
                password="replacement-password",
            )
            self.assertEqual(user.id, self.user_id)

        with self.assertRaises(HTTPException) as old_session:
            current_principal(signalops_session=self.old_session)
        self.assertEqual(old_session.exception.status_code, 401)

        with self.assertRaises(HTTPException) as reused:
            asyncio.run(
                self.app_module.reset_password(payload, Response())
            )
        self.assertEqual(reused.exception.status_code, 400)

    def test_expired_token_is_rejected(self):
        from db import session_scope
        from models import PasswordResetToken

        _, sender = self._request()
        raw_token = sender.call_args.kwargs["token"]
        with session_scope() as session:
            stored = session.query(PasswordResetToken).one()
            stored.expires_at = time.time() - 1

        payload = self.app_module.PasswordResetCompletion(
            token=raw_token,
            new_password="replacement-password",
        )
        with self.assertRaises(HTTPException) as expired:
            asyncio.run(
                self.app_module.reset_password(payload, Response())
            )
        self.assertEqual(expired.exception.status_code, 400)


class PasswordResetEmailTests(unittest.TestCase):
    def test_reset_link_uses_a_fragment_and_the_configured_public_origin(self):
        from email_delivery import password_reset_url, smtp_settings

        with patch.dict(os.environ, {
            "SIGNALOPS_PUBLIC_URL": "https://signalaiops.com/",
            "SIGNALOPS_SMTP_HOST": "smtp.example.com",
            "SIGNALOPS_SMTP_FROM": "security@signalaiops.com",
            "SIGNALOPS_SMTP_PORT": "587",
            "SIGNALOPS_SMTP_USERNAME": "",
            "SIGNALOPS_SMTP_PASSWORD": "",
        }, clear=True):
            settings = smtp_settings()
            url = password_reset_url("token/with+symbols", settings)
        self.assertEqual(
            url,
            "https://signalaiops.com/login#reset=token%2Fwith%2Bsymbols",
        )
        self.assertNotIn("?", url)


if __name__ == "__main__":
    unittest.main()
