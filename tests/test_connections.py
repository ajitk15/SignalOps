"""Connector model, storage mapping, and health-check regression coverage."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.app import (ConnectionRequest, _connection_parts,  # noqa: E402
                        _run_connection_test)


class ConnectorPayloadTests(unittest.TestCase):
    def _request(self, kind, **fields):
        return ConnectionRequest(
            kind=kind, name=f"Production {kind}", base_url=f"https://{kind}.example.com",
            **fields,
        )

    def test_all_supported_connector_kinds_validate(self):
        for kind in ("servicenow", "jira", "splunk", "datadog", "dynatrace"):
            with self.subTest(kind=kind):
                self.assertEqual(self._request(kind).kind, kind)

    def test_unknown_connector_kind_is_rejected(self):
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            self._request("unknown")

    def test_splunk_maps_query_and_token_without_putting_token_in_config(self):
        config, secrets = _connection_parts(self._request(
            "splunk", access_token="splunk-secret", search_query=" index=prod ",
        ))
        self.assertEqual(config["search_query"], "index=prod")
        self.assertNotIn("access_token", config)
        self.assertEqual(dict(secrets)["access_token"], "splunk-secret")

    def test_datadog_maps_filter_and_both_keys(self):
        config, secrets = _connection_parts(self._request(
            "datadog", api_key="api-secret", app_key="app-secret",
            service_filter=" service:checkout ",
        ))
        self.assertEqual(config["service_filter"], "service:checkout")
        self.assertNotIn("api_key", config)
        self.assertEqual(dict(secrets), {
            "api_key": "api-secret",
            "app_key": "app-secret",
        })

    def test_dynatrace_maps_selector_and_token(self):
        config, secrets = _connection_parts(self._request(
            "dynatrace", access_token="dt-secret",
            entity_selector=" type(SERVICE) ",
        ))
        self.assertEqual(config["entity_selector"], "type(SERVICE)")
        self.assertNotIn("access_token", config)
        self.assertEqual(dict(secrets)["access_token"], "dt-secret")


class ConnectorHealthCheckTests(unittest.IsolatedAsyncioTestCase):
    async def _assert_probe(self, kind, config, secrets, expected_url, expected_headers):
        calls = []

        class Response:
            def raise_for_status(self):
                return None

        def fake_get(url, **kwargs):
            calls.append((url, kwargs))
            return Response()

        with patch("httpx.get", fake_get):
            ok, detail, matches = await _run_connection_test(kind, config, secrets)

        self.assertTrue(ok)
        self.assertIn(kind.capitalize() if kind != "datadog" else "Datadog", detail)
        self.assertIsNone(matches)
        self.assertEqual(calls[0][0], expected_url)
        self.assertEqual(calls[0][1]["timeout"], 15)
        for header, value in expected_headers.items():
            self.assertEqual(calls[0][1]["headers"][header], value)

    async def test_splunk_probe(self):
        await self._assert_probe(
            "splunk",
            {"base_url": "https://splunk.example.com:8089"},
            {"access_token": "token"},
            "https://splunk.example.com:8089/services/server/info?output_mode=json",
            {"Authorization": "Splunk token"},
        )

    async def test_datadog_probe(self):
        await self._assert_probe(
            "datadog",
            {"base_url": "https://api.datadoghq.com"},
            {"api_key": "api", "app_key": "app"},
            "https://api.datadoghq.com/api/v1/validate",
            {"DD-API-KEY": "api", "DD-APPLICATION-KEY": "app"},
        )

    async def test_dynatrace_probe(self):
        await self._assert_probe(
            "dynatrace",
            {"base_url": "https://abc.live.dynatrace.com"},
            {"access_token": "token"},
            "https://abc.live.dynatrace.com/api/v2/problems?pageSize=1",
            {"Authorization": "Api-Token token"},
        )


if __name__ == "__main__":
    unittest.main()
