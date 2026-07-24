"""Public product page and authenticated-app entry routing."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from server.app import app  # noqa: E402


class LandingPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_root_is_the_public_product_page(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("From signal to safe resolution", response.text)
        self.assertIn('href="/login"', response.text)
        self.assertIn("Human approval gates", response.text)
        self.assertIn("/static/og.png", response.text)
        self.assertIn('class="signal-river"', response.text)
        self.assertIn("Human approval checkpoint passed", response.text)
        stages = [
            response.text.index("<h2>Detect</h2>"),
            response.text.index("<h2>Diagnose</h2>"),
            response.text.index("<h2>Approve</h2>"),
            response.text.index("<h2>Resolve</h2>"),
        ]
        self.assertEqual(stages, sorted(stages))

    def test_login_keeps_the_existing_authenticated_shell(self):
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="login-form"', response.text)
        self.assertIn('id="app"', response.text)
        self.assertIn('class="login-visual-panel"', response.text)
        # The CSS brand panel (no raster image) echoes the landing flow.
        self.assertIn("From signal to", response.text)
        self.assertIn('class="login-flow"', response.text)
        for step in ("<strong>Detect</strong>", "<strong>Diagnose</strong>",
                     "<strong>Approve</strong>", "<strong>Resolve</strong>"):
            self.assertIn(step, response.text)
        # The heavy social-share image is no longer loaded on the login path.
        self.assertNotIn("/static/og.png", response.text)
        self.assertIn('id="forgot-password-form"', response.text)
        self.assertIn('id="reset-password-form"', response.text)
        self.assertNotIn("data-theme-label", response.text)
        self.assertNotIn('class="hero"', response.text)

    def test_landing_assets_are_served(self):
        for path, content_type in (
            ("/static/landing.css", "text/css"),
            ("/static/og.png", "image/png"),
            ("/static/signalaiops-logo-dark-v3.png", "image/png"),
            ("/static/signalaiops-logo-light-v3.png", "image/png"),
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn(content_type, response.headers["content-type"])


if __name__ == "__main__":
    unittest.main()
