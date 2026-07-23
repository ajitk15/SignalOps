"""How the ServiceNow client proves who it is.

Written after a real debugging session: an instance accepted a credential at
its UI login form and returned `401 User is not authenticated` for every REST
call, including one needing no roles — the same response a nonexistent user
gets. Basic auth was being refused platform-wide. These tests pin the two
things that made that expensive: the client can speak OAuth instead, and a 401
now says what it might actually mean.
"""
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from integrations import servicenow  # noqa: E402
from integrations.servicenow import (OAuthTokenProvider, ServiceNowAuthError,  # noqa: E402
                                     ServiceNowClient, ServiceNowError)

TOKEN_URL = "https://example.service-now.com/oauth_token.do"


class Response:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {"result": []}
        self.text = text
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("boom", request=None, response=self)


class AuthSelectionTests(unittest.TestCase):
    def test_without_client_credentials_the_client_uses_basic(self):
        client = ServiceNowClient("https://x", "u", "p")
        self.assertEqual(client.auth_method, "basic")

    def test_client_credentials_switch_it_to_oauth(self):
        client = ServiceNowClient("https://x", "u", "p", "cid", "csecret")
        self.assertEqual(client.auth_method, "oauth")

    def test_basic_auth_sends_no_bearer_header(self):
        client = ServiceNowClient("https://x", "u", "p")
        captured = {}

        def fake_request(method, url, **kwargs):
            captured.update(kwargs)
            return Response()

        with patch("integrations.servicenow.httpx.request", fake_request):
            client.test()
        self.assertIn("auth", captured)
        self.assertNotIn("Authorization", captured["headers"])

    def test_oauth_sends_a_bearer_token_and_no_basic_auth(self):
        client = ServiceNowClient("https://example.service-now.com", "u", "p", "cid", "sec")
        captured = {}

        def fake_post(url, data=None, timeout=None):
            return Response(200, {"access_token": "tok-abc", "expires_in": 1800})

        def fake_request(method, url, **kwargs):
            captured.update(kwargs)
            return Response()

        with patch("integrations.servicenow.httpx.post", fake_post), \
             patch("integrations.servicenow.httpx.request", fake_request):
            client.test()
        self.assertEqual(captured["headers"]["Authorization"], "Bearer tok-abc")
        self.assertNotIn("auth", captured)


class TokenLifecycleTests(unittest.TestCase):
    def _provider(self):
        return OAuthTokenProvider("https://example.service-now.com", "cid", "sec", "u", "p")

    def test_a_token_is_fetched_once_and_reused(self):
        provider = self._provider()
        calls = []

        def fake_post(url, data=None, timeout=None):
            calls.append(data)
            return Response(200, {"access_token": "tok", "expires_in": 1800})

        with patch("integrations.servicenow.httpx.post", fake_post):
            self.assertEqual(provider.token(), "tok")
            self.assertEqual(provider.token(), "tok")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["grant_type"], "password")

    def test_an_expired_token_is_replaced(self):
        provider = self._provider()
        calls = []

        def fake_post(url, data=None, timeout=None):
            calls.append(data["grant_type"])
            return Response(200, {"access_token": f"tok{len(calls)}", "expires_in": 1800,
                                  "refresh_token": "refresh-1"})

        with patch("integrations.servicenow.httpx.post", fake_post):
            provider.token()
            provider._expires_at = time.time() - 1          # pretend it aged out
            second = provider.token()
        self.assertEqual(second, "tok2")
        # The second fetch uses the refresh token rather than the password.
        self.assertEqual(calls, ["password", "refresh_token"])

    def test_a_dead_refresh_token_falls_back_to_the_password_grant(self):
        """Otherwise a revoked refresh token fails every run until a restart."""
        provider = self._provider()
        calls = []

        def fake_post(url, data=None, timeout=None):
            calls.append(data["grant_type"])
            if data["grant_type"] == "refresh_token":
                return Response(401, {"error": "invalid_grant"})
            return Response(200, {"access_token": "fresh", "expires_in": 1800,
                                  "refresh_token": "r"})

        with patch("integrations.servicenow.httpx.post", fake_post):
            provider.token()
            provider._expires_at = time.time() - 1
            self.assertEqual(provider.token(), "fresh")
        self.assertEqual(calls, ["password", "refresh_token", "password"])

    def test_a_401_refreshes_the_token_and_retries_exactly_once(self):
        """A token can expire between the expiry check and the call. One retry
        separates that from a credential that is genuinely not accepted."""
        client = ServiceNowClient("https://example.service-now.com", "u", "p", "cid", "sec")
        attempts = []

        def fake_post(url, data=None, timeout=None):
            return Response(200, {"access_token": f"tok{len(attempts)}", "expires_in": 1800})

        def fake_request(method, url, **kwargs):
            attempts.append(kwargs["headers"]["Authorization"])
            return Response(200) if len(attempts) > 1 else Response(401, {})

        with patch("integrations.servicenow.httpx.post", fake_post), \
             patch("integrations.servicenow.httpx.request", fake_request):
            client.test()
        self.assertEqual(len(attempts), 2)
        self.assertNotEqual(attempts[0], attempts[1])       # a genuinely new token

    def test_a_persistent_401_raises_rather_than_looping(self):
        client = ServiceNowClient("https://example.service-now.com", "u", "p", "cid", "sec")
        attempts = []

        with patch("integrations.servicenow.httpx.post",
                   lambda url, data=None, timeout=None: Response(
                       200, {"access_token": "t", "expires_in": 1800})), \
             patch("integrations.servicenow.httpx.request",
                   lambda method, url, **kwargs: (attempts.append(1), Response(401, {}))[1]):
            with self.assertRaises(ServiceNowAuthError):
                client.test()
        self.assertEqual(len(attempts), 2)


class ErrorMessageTests(unittest.TestCase):
    """The messages are the deliverable here: the wrong one costs an hour."""

    def test_a_401_does_not_claim_the_password_is_wrong(self):
        message = servicenow._explain_rejection(Response(401), "basic")
        self.assertIn("same response the instance gives for a user that does not exist",
                      message)
        self.assertIn("/login.do", message)
        self.assertIn("SN_CLIENT_ID", message)
        self.assertIn("multi-factor", message)

    def test_a_403_points_at_roles_not_at_the_credential(self):
        message = servicenow._explain_rejection(Response(403), "basic")
        self.assertIn("accepted the credential", message)
        self.assertIn("role", message)
        self.assertNotIn("does not exist", message)

    def test_an_oauth_401_does_not_send_you_to_the_basic_auth_advice(self):
        message = servicenow._explain_rejection(Response(401), "oauth")
        self.assertIn("OAuth token", message)
        self.assertNotIn("SN_CLIENT_ID", message)

    def test_oauth_errors_name_the_screen_to_go_to(self):
        self.assertIn("Application Registry", servicenow._explain_token_failure(
            Response(401, {"error": "invalid_client"})))
        self.assertIn("multi-factor", servicenow._explain_token_failure(
            Response(401, {"error": "invalid_grant"})))

    def test_a_timeout_blames_reachability_not_the_credential(self):
        """A dev instance that has gone to sleep times out, and the credentials
        are never the cause — saying "could not reach" sends people to re-check
        a password that was fine."""
        import httpx
        client = ServiceNowClient("https://dev123.service-now.com", "u", "p")
        with patch("integrations.servicenow.httpx.request",
                   side_effect=httpx.ReadTimeout("timed out")):
            with self.assertRaises(ServiceNowError) as caught:
                client.test()
        message = str(caught.exception)
        self.assertIn("did not respond", message)
        self.assertIn("developer.servicenow.com", message)
        self.assertIn("credentials are not the issue", message)

    def test_a_non_json_token_failure_still_produces_a_message(self):
        class Broken(Response):
            def json(self):
                raise ValueError("not json")

        self.assertIn("OAuth token request failed",
                      servicenow._explain_token_failure(Broken(502)))


class SecretHygieneTests(unittest.TestCase):
    def test_no_error_message_carries_the_token_or_the_password(self):
        client = ServiceNowClient("https://example.service-now.com", "u", "hunter2",
                                  "cid", "s3cret")
        with patch("integrations.servicenow.httpx.post",
                   lambda url, data=None, timeout=None: Response(
                       200, {"access_token": "tok-SECRET", "expires_in": 1800})), \
             patch("integrations.servicenow.httpx.request",
                   lambda method, url, **kwargs: Response(401, {})):
            with self.assertRaises(ServiceNowError) as caught:
                client.test()
        rendered = str(caught.exception)
        for secret in ("hunter2", "s3cret", "tok-SECRET"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, rendered)

    def test_env_status_reports_oauth_presence_without_values(self):
        with patch.dict("os.environ", {"SN_CLIENT_ID": "abc", "SN_CLIENT_SECRET": "xyz"},
                        clear=False):
            status = servicenow.env_status()
            self.assertIn("SN_CLIENT_ID", status)
            self.assertTrue(status["SN_CLIENT_ID"])
            self.assertEqual(servicenow.auth_method(), "oauth")
        self.assertNotIn("xyz", str(status))

    def test_without_oauth_variables_the_reported_method_is_basic(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(servicenow.auth_method(), "basic")


if __name__ == "__main__":
    unittest.main()
