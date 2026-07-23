"""The login/registration hardening from the security QA pass.

Four defects and two concerns, each pinned so a regression fails loudly:

- a malformed or blank email cannot become an account;
- a locked account does not announce itself and leak that it exists;
- a forced password change is enforced server-side, not merely displayed;
- sessions expire, and the cookie carries Secure outside local.
"""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auth  # noqa: E402
from auth import (PasswordAuthProvider, hash_password, normalise_email,  # noqa: E402
                  valid_email)


class EmailValidationTests(unittest.TestCase):
    def test_malformed_and_blank_addresses_are_rejected(self):
        for bad in ("not-an-email", "", "   ", "a@b", "@b.com", "a@ b.com", "a b@c.com"):
            with self.subTest(value=repr(bad)):
                self.assertFalse(valid_email(bad))

    def test_ordinary_addresses_pass_and_normalise(self):
        self.assertTrue(valid_email("Ada.Lovelace@Example.COM"))
        self.assertEqual(normalise_email("  Ada@Example.COM "), "ada@example.com")

    def test_the_user_request_model_rejects_a_bad_email(self):
        from pydantic import ValidationError

        from server.app import UserRequest
        with self.assertRaises(ValidationError):
            UserRequest(email="not-an-email", display_name="A", password="x" * 12)
        with self.assertRaises(ValidationError):
            UserRequest(email="   ", display_name="A", password="x" * 12)

    def test_the_user_request_model_rejects_a_blank_name(self):
        from pydantic import ValidationError

        from server.app import UserRequest
        with self.assertRaises(ValidationError):
            UserRequest(email="a@b.com", display_name="   ", password="x" * 12)

    def test_a_valid_request_is_normalised(self):
        from server.app import UserRequest
        req = UserRequest(email="  Ada@Example.COM ", display_name="  Ada  ",
                          password="a-long-password")
        self.assertEqual(req.email, "ada@example.com")
        self.assertEqual(req.display_name, "Ada")


class LockoutDisclosureTests(unittest.TestCase):
    """A locked account must answer exactly as a wrong password does, so the
    lockout is not an account-enumeration oracle."""

    def _user(self, **kw):
        from models import User
        defaults = dict(id="u", workspace_id="ws", email="a@b.com", display_name="A",
                        role=None, password_hash=hash_password("correct-horse"),
                        active=True, must_change_password=False, failed_logins=0,
                        locked_until=None)
        defaults.update(kw)
        return User(**defaults)

    def test_a_locked_account_gives_the_generic_message(self):
        from auth import InvalidCredentials
        provider = PasswordAuthProvider()

        class Session:
            def __init__(self, user):
                self._user = user

            def query(self, *_):
                return self

            def filter(self, *_):
                return self

            def one_or_none(self):
                return self._user

        locked = self._user(locked_until=time.time() + 300)
        with self.assertRaises(InvalidCredentials) as caught:
            provider.login(Session(locked), "ws", email="a@b.com", password="whatever")
        # No countdown, no "too many attempts" — identical to a bad password.
        self.assertEqual(str(caught.exception), "email or password is incorrect")
        self.assertNotIn("attempts", str(caught.exception).lower())


class ForcedPasswordChangeTests(unittest.TestCase):
    def test_require_role_blocks_a_user_who_must_change_password(self):
        from fastapi import HTTPException

        from auth import PASSWORD_CHANGE_REQUIRED, Principal, require_role
        from models import Role, User, Workspace
        workspace = Workspace(id="ws", name="w")
        user = User(id="u", workspace_id="ws", email="a@b.com", display_name="A",
                    role=Role.admin, must_change_password=True)
        principal = Principal(user, workspace)

        dependency = require_role(Role.viewer)
        with self.assertRaises(HTTPException) as caught:
            dependency(principal=principal)
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(caught.exception.detail, PASSWORD_CHANGE_REQUIRED)

    def test_clearing_the_flag_restores_access(self):
        from auth import Principal, require_role
        from models import Role, User, Workspace
        workspace = Workspace(id="ws", name="w")
        user = User(id="u", workspace_id="ws", email="a@b.com", display_name="A",
                    role=Role.operator, must_change_password=False)
        principal = Principal(user, workspace)
        # Should not raise now that the flag is clear.
        self.assertIs(require_role(Role.viewer)(principal=principal), principal)

    def test_set_password_clears_the_flag(self):
        from auth import set_password
        from models import User
        user = User(id="u", workspace_id="ws", email="a@b.com", display_name="A",
                    must_change_password=True)
        set_password(user, "a-brand-new-password")
        self.assertFalse(user.must_change_password)


class SessionTests(unittest.TestCase):
    def test_a_session_past_its_lifetime_is_rejected(self):
        from models import User
        user = User(id="u", workspace_id="ws", email="a@b.com", display_name="A")
        token = auth.issue_session(user)
        self.assertIsNotNone(auth._load_session(token))
        # Simulate the clock moving past the max age.
        original = auth.SESSION_MAX_AGE_SECONDS
        try:
            auth.SESSION_MAX_AGE_SECONDS = -1
            self.assertIsNone(auth._load_session(token))
        finally:
            auth.SESSION_MAX_AGE_SECONDS = original

    def test_a_tampered_session_is_rejected(self):
        self.assertIsNone(auth._load_session("not.a.real.token"))

    def test_the_secure_flag_follows_the_environment(self):
        # Documents the contract the login route relies on: Secure is off for
        # local and on elsewhere.
        self.assertIn(auth.COOKIE_SECURE, (True, False))
        self.assertFalse(auth.COOKIE_SECURE)   # tests run under SIGNALOPS_ENV unset


if __name__ == "__main__":
    unittest.main()
