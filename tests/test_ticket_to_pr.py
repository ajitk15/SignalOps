"""Workflow B: the code path, where the blast radius is largest.

The tests that matter here are the refusals. A workflow that opens a good pull
request on a good day is easy; what has to hold is that a failing suite blocks
it, that the agent cannot edit CI, that nothing reaches the default branch, and
that approving a diff does not authorise a different one.
"""
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import ticket_to_pr  # noqa: E402
from engine.approvals import canonical_hash  # noqa: E402
from engine.llm import AgentResult  # noqa: E402
from integrations import repo as repo_module  # noqa: E402
from integrations.repo import (DEPENDENCY_PATTERNS, PullRequestSink,  # noqa: E402
                               RepoError, RepoWorkspace, is_protected)
from tests.test_engine import _fresh_db  # noqa: E402


class ProtectedPathTests(unittest.TestCase):
    def test_ci_configuration_is_never_writable(self):
        """An agent that can edit CI can run arbitrary code on the next push,
        which makes the review gate decorative."""
        for path in (".github/workflows/ci.yml", ".gitlab-ci.yml", "Jenkinsfile",
                     ".circleci/config.yml"):
            with self.subTest(path=path):
                self.assertIsNotNone(is_protected(path))

    def test_infrastructure_and_secrets_are_never_writable(self):
        for path in ("main.tf", "Dockerfile", ".env", ".env.production", "id_rsa",
                     "secrets/db.yaml", "app/secrets/token.pem", "k8s/deploy.yaml"):
            with self.subTest(path=path):
                self.assertIsNotNone(is_protected(path))

    def test_ci_stays_refused_even_when_dependencies_are_allowed(self):
        # The dependency opt-in must not be a general-purpose unlock.
        self.assertIsNotNone(is_protected(".github/workflows/ci.yml",
                                          allow_dependencies=True))

    def test_dependency_manifests_are_refused_by_default_and_can_be_opted_into(self):
        for path in DEPENDENCY_PATTERNS[:6]:
            probe = path.replace("*", "")
            with self.subTest(path=probe):
                self.assertIsNotNone(is_protected(probe))
        self.assertIsNone(is_protected("requirements.txt", allow_dependencies=True))

    def test_path_traversal_is_refused(self):
        for path in ("../../etc/passwd", "src/../../secrets.txt"):
            with self.subTest(path=path):
                self.assertIn("escapes", is_protected(path) or "")

    def test_ordinary_source_files_are_writable(self):
        for path in ("src/app.py", "lib/util/helpers.ts", "README.md"):
            with self.subTest(path=path):
                self.assertIsNone(is_protected(path))


def _make_origin(root: Path) -> Path:
    origin = root / "origin"
    origin.mkdir()

    def git(*args):
        subprocess.run(["git", *args], cwd=origin, check=True, capture_output=True)

    git("init", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (origin / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (origin / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8")
    (origin / ".github").mkdir()
    (origin / ".github" / "workflows").mkdir()
    (origin / ".github" / "workflows" / "ci.yml").write_text("on: push\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "initial")
    return origin


class RepoWorkspaceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.origin = _make_origin(self.root)

    def _workspace(self, **kwargs):
        workspace = RepoWorkspace(url=str(self.origin), base_branch="main",
                                  branch="signalops/test", **kwargs)
        workspace.clone()
        self.addCleanup(workspace.cleanup)
        return workspace

    def test_a_clone_is_on_its_own_branch(self):
        workspace = self._workspace()
        head = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                              cwd=workspace.path, capture_output=True, text=True)
        self.assertEqual(head.stdout.strip(), "signalops/test")

    def test_pushing_to_the_base_branch_is_refused(self):
        """There is no merge method on this class at all; this is the other
        half — the branch it pushes can never be the default one."""
        workspace = self._workspace()
        workspace.branch = "main"
        with self.assertRaises(RepoError):
            workspace.push()

    def test_a_protected_edit_is_caught_before_a_commit_exists(self):
        workspace = self._workspace()
        (workspace.path / ".github" / "workflows" / "ci.yml").write_text(
            "on: push\njobs: {evil: {}}\n", encoding="utf-8")
        with self.assertRaises(repo_module.PathRefused) as caught:
            workspace.assert_changes_allowed()
        self.assertIn("ci.yml", str(caught.exception))

    def test_reading_outside_the_repository_is_refused(self):
        workspace = self._workspace()
        with self.assertRaises(repo_module.PathRefused):
            workspace.read("../../../etc/passwd")

    def test_a_patch_survives_being_applied_to_a_fresh_clone(self):
        """This is what lets a review gate outlive the checkout the diff was
        made in — the patch is the durable artifact, never the working copy."""
        first = self._workspace()
        (first.path / "calc.py").write_text("def add(a, b):\n    return a + b\n",
                                            encoding="utf-8")
        patch_text = first.diff()
        self.assertIn("diff --git", patch_text)
        first.cleanup()

        second = self._workspace()
        second.apply_patch(patch_text)
        self.assertEqual((second.path / "calc.py").read_text(encoding="utf-8"),
                         "def add(a, b):\n    return a + b\n")

    def test_a_patch_that_no_longer_applies_fails_loudly(self):
        """Opening a pull request from a stale base is exactly the surprise the
        review gate exists to prevent, so this must not degrade quietly."""
        workspace = self._workspace()
        with self.assertRaises(RepoError) as caught:
            workspace.apply_patch(
                "diff --git a/calc.py b/calc.py\n"
                "--- a/calc.py\n+++ b/calc.py\n"
                "@@ -1,2 +1,2 @@\n-def nonexistent():\n-    pass\n"
                "+def nonexistent():\n+    return 1\n")
        self.assertIn("no longer applies", str(caught.exception))

    def test_credentials_never_reach_an_error_message(self):
        scrubbed = repo_module._scrub(
            "fatal: could not read from https://x-access-token:ghp_SECRETVALUE@github.com/a/b")
        self.assertNotIn("ghp_SECRETVALUE", scrubbed)
        self.assertIn("***@", scrubbed)


class CodeAgentPermissionTests(unittest.IsolatedAsyncioTestCase):
    """The per-call veto. Refusing after the fact still leaves a diff somebody
    has to review and discard."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.workspace = RepoWorkspace(url=str(_make_origin(root)), base_branch="main",
                                       branch="signalops/test")
        self.workspace.clone()
        self.addCleanup(self.workspace.cleanup)
        self.refusals = []
        from engine.coder import _permission_callback
        self.callback = _permission_callback(self.workspace, self.refusals)

    async def test_a_shell_is_refused(self):
        """The Agent SDK ships one and it is a way to reach everything the
        allowlist just finished restricting."""
        result = await self.callback("Bash", {"command": "rm -rf /"}, None)
        self.assertEqual(result.behavior, "deny")

    async def test_network_tools_are_refused(self):
        for tool in ("WebFetch", "WebSearch"):
            with self.subTest(tool=tool):
                result = await self.callback(tool, {"url": "https://example.com"}, None)
                self.assertEqual(result.behavior, "deny")

    async def test_editing_ci_is_refused_at_the_moment_of_the_call(self):
        target = self.workspace.path / ".github" / "workflows" / "ci.yml"
        result = await self.callback("Edit", {"file_path": str(target)}, None)
        self.assertEqual(result.behavior, "deny")
        self.assertIn("CI", result.message)
        # The message tells the agent the rule rather than inviting a hunt for
        # a path that slips through.
        self.assertIn("cannot be worked around", result.message)

    async def test_editing_outside_the_checkout_is_refused(self):
        result = await self.callback("Write", {"file_path": "C:/Windows/system32/x.dll"
                                               if sys.platform == "win32" else "/etc/passwd"},
                                     None)
        self.assertEqual(result.behavior, "deny")

    async def test_an_ordinary_source_edit_is_allowed(self):
        target = self.workspace.path / "calc.py"
        result = await self.callback("Edit", {"file_path": str(target)}, None)
        self.assertEqual(result.behavior, "allow")

    async def test_reading_is_allowed_anywhere_in_the_checkout(self):
        result = await self.callback("Read", {"file_path": str(self.workspace.path / "calc.py")},
                                     None)
        self.assertEqual(result.behavior, "allow")


class _FakeLLM:
    """Answers each agent with something usable, so the graph can be exercised."""
    name = "fake"
    simulated = False
    OUT = {
        "code_locator": {"files": ["calc.py"], "reasoning": "add() is here",
                         "confidence": 0.9},
        "impact_analyst": {"complexity": "trivial", "risk": "low",
                           "blast_radius": "one function", "test_coverage": "covered",
                           "concerns": [], "confidence": 0.9},
        # Deliberately approving, so a blocked run proves the tests outrank it.
        "qa_reviewer": {"verdict": "approve", "findings": [], "confidence": 0.95},
        "triage": {"in_scope": True, "urgency": "P3", "reason": "bug", "confidence": 0.9},
    }

    def complete(self, agent, task, schema):
        output = schema.model_validate(self.OUT[agent.id]).model_dump()
        return AgentResult(output=output, model=agent.model, input_tokens=100,
                           output_tokens=50, cost_usd=0.001, simulated=False)


class _CodeHarness:
    """A real repo, a real engine, and an implementer we control."""

    def __init__(self, case, *, edit, test_command=None):
        temp = tempfile.TemporaryDirectory()
        case.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.origin = _make_origin(self.root)
        _fresh_db(case)

        from db import init_db, session_scope
        from engine.runtime import Engine
        from models import Workflow
        self.session_scope = session_scope
        workspace_id = init_db()
        with session_scope() as session:
            workflow = Workflow(
                workspace_id=workspace_id, template="ticket_to_pr", name="Bug to PR",
                config={"dry_run": True, "run_budget_usd": 1.0,
                        "repo_url": str(self.origin), "base_branch": "main",
                        "repo_full_name": "acme/widget",
                        "test_command": test_command},
                enabled=True)
            session.add(workflow)
            session.flush()
            self.workflow_id = workflow.id

        from engine import coder
        from engine.coder import CodeResult

        async def fake_implement(*, agent, workspace, ticket, analysis, files,
                                 budget_usd=None):
            edit(workspace.path)
            return CodeResult(summary="Changed it.", files_changed=workspace.changed_files(),
                              cost_usd=0.01, turns=2, simulated=False)

        case.addCleanup(setattr, coder, "implement", coder.implement)
        coder.implement = fake_implement

        self.engine = Engine(client=_FakeLLM(), checkpoint_path=self.root / "cp.db")
        case.addCleanup(lambda: self.engine.shutdown(drain=True))

    def run(self, ticket=None):
        return self.engine.start(
            workflow_id=self.workflow_id, actor="tester",
            ticket=ticket or {"number": "BUG-1", "short_description": "add() is wrong"})

    def wait(self, run_id, *statuses, timeout=240):
        from models import Run
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.session_scope() as session:
                run = session.get(Run, run_id)
                if run.status.value in statuses:
                    return run
            time.sleep(0.1)
        with self.session_scope() as session:
            run = session.get(Run, run_id)
            raise AssertionError(f"stuck in {run.status.value}: {run.error}")

    def steps(self, run_id):
        from models import RunStep
        with self.session_scope() as session:
            return [s.node for s in session.query(RunStep)
                    .filter(RunStep.run_id == run_id).order_by(RunStep.started_at)]

    def approval(self, run_id):
        from models import Approval, ApprovalStatus
        with self.session_scope() as session:
            return (session.query(Approval)
                    .filter(Approval.run_id == run_id,
                            Approval.status == ApprovalStatus.pending).first())


PYTEST = f'"{sys.executable}" -m pytest -q'


def _fix(path: Path):
    (path / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")


def _break(path: Path):
    (path / "calc.py").write_text("def add(a, b):\n    return a * b\n", encoding="utf-8")


class QaAuthorityTests(unittest.TestCase):
    def test_a_failing_suite_blocks_the_pull_request(self):
        """The reviewer agent approves in _FakeLLM. The tests must win anyway."""
        harness = _CodeHarness(self, edit=_break, test_command=PYTEST)
        run_id = harness.run()
        run = harness.wait(run_id, "succeeded", "failed")
        steps = harness.steps(run_id)
        self.assertIn("qa", steps)
        self.assertNotIn("pull_request", steps)
        self.assertIsNone(harness.approval(run_id))    # never even reached a human

    def test_a_passing_suite_reaches_the_human_gate(self):
        harness = _CodeHarness(self, edit=_fix, test_command=PYTEST)
        run_id = harness.run()
        harness.wait(run_id, "awaiting_approval")
        approval = harness.approval(run_id)
        self.assertIsNotNone(approval)
        self.assertEqual(approval.payload["tests"]["status"], "passed")

    def test_no_configured_command_is_not_reported_as_a_pass(self):
        """An unknown must not look like a green build to whoever approves."""
        harness = _CodeHarness(self, edit=_fix, test_command=None)
        run_id = harness.run()
        harness.wait(run_id, "awaiting_approval")
        self.assertEqual(harness.approval(run_id).payload["tests"]["status"],
                         "not_configured")


class CodeGateTests(unittest.TestCase):
    def test_the_approval_is_pinned_to_the_patch_itself(self):
        """Approving a diff must not authorise a different diff."""
        harness = _CodeHarness(self, edit=_fix, test_command=PYTEST)
        run_id = harness.run()
        harness.wait(run_id, "awaiting_approval")
        approval = harness.approval(run_id)
        self.assertIn("diff --git", approval.payload["patch"])
        self.assertEqual(canonical_hash(approval.payload), approval.payload_hash)

        from engine.approvals import StaleApproval
        from models import Approval
        with harness.session_scope() as session:
            row = session.get(Approval, approval.id)
            row.payload = {**row.payload, "patch": "diff --git a/x b/x\n(something else)"}
        with self.assertRaises(StaleApproval):
            harness.engine.decide(approval_id=approval.id, approved=True, actor="mallory",
                                  actor_id=None)

    def test_a_dry_run_prepares_the_branch_and_pushes_nothing(self):
        harness = _CodeHarness(self, edit=_fix, test_command=PYTEST)
        run_id = harness.run()
        harness.wait(run_id, "awaiting_approval")
        harness.engine.decide(approval_id=harness.approval(run_id).id, approved=True,
                              actor="ada", actor_id=None, note="good")
        harness.wait(run_id, "succeeded")
        self.assertIn("pull_request", harness.steps(run_id))
        branches = subprocess.run(["git", "branch"], cwd=harness.origin,
                                  capture_output=True, text=True).stdout
        # The upstream must be untouched: one branch, the default one.
        self.assertEqual([b.strip("* ").strip() for b in branches.splitlines()], ["main"])

    def test_rejecting_opens_nothing(self):
        harness = _CodeHarness(self, edit=_fix, test_command=PYTEST)
        run_id = harness.run()
        harness.wait(run_id, "awaiting_approval")
        harness.engine.decide(approval_id=harness.approval(run_id).id, approved=False,
                              actor="ada", actor_id=None, note="not this way")
        harness.wait(run_id, "succeeded")
        self.assertNotIn("pull_request", harness.steps(run_id))

    def test_an_agent_that_edits_ci_fails_the_run_rather_than_shipping_it(self):
        def edit_ci(path: Path):
            (path / ".github" / "workflows" / "ci.yml").write_text(
                "on: push\njobs:\n  x:\n    runs-on: ubuntu-latest\n", encoding="utf-8")

        harness = _CodeHarness(self, edit=edit_ci, test_command=PYTEST)
        run_id = harness.run()
        run = harness.wait(run_id, "failed", "succeeded")
        self.assertEqual(run.status.value, "failed")
        self.assertIn("ci.yml", run.error)
        self.assertNotIn("pull_request", harness.steps(run_id))


class PullRequestSinkTests(unittest.TestCase):
    def test_dry_run_holds_no_token(self):
        sink = PullRequestSink(dry_run=True, token="ghp_x")
        self.assertFalse(sink.live)
        result = sink.open(repo="a/b", branch="x", base="main", title="t", body="b")
        self.assertFalse(result.opened)
        self.assertIsNone(result.url)

    def test_there_is_no_way_to_merge(self):
        """Not a disabled method, not one behind a flag — absent."""
        for name in dir(PullRequestSink):
            self.assertNotIn("merge", name.lower())

    def test_the_pull_request_is_opened_as_a_draft(self):
        captured = {}

        class Response:
            status_code = 201

            def raise_for_status(self):
                pass

            def json(self):
                return {"html_url": "https://github.com/a/b/pull/1"}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured.update(json)
            return Response()

        with patch("integrations.repo.httpx.post", fake_post):
            sink = PullRequestSink(dry_run=False, token="ghp_x")
            result = sink.open(repo="a/b", branch="signalops/1", base="main",
                               title="t", body="b")
        self.assertTrue(captured["draft"])
        self.assertTrue(result.opened)


class PullRequestBodyTests(unittest.TestCase):
    def test_the_body_states_it_was_automated_and_not_merged(self):
        state = {"ticket": {"number": "BUG-1"},
                 "outputs": {"implementer": {"summary": "Fixed it."},
                             "impact_analyst": {"complexity": "trivial", "risk": "low"}},
                 "context": {"tests": {"status": "passed", "summary": "exited 0"}},
                 "approval": {"by": "ada", "note": "good"}}
        body = ticket_to_pr._pr_body(state, simulated=False)
        self.assertIn("automated workflow", body)
        self.assertIn("not merged", body)
        self.assertIn("ada", body)
        self.assertIn("passed", body)

    def test_a_simulated_run_says_do_not_merge_at_the_top(self):
        state = {"ticket": {}, "outputs": {"implementer": {}}, "context": {}, "approval": {}}
        body = ticket_to_pr._pr_body(state, simulated=True)
        self.assertTrue(body.startswith("> **Simulated run**"))
        self.assertIn("Do not merge", body)


class TestRunnerTests(unittest.TestCase):
    def test_the_command_comes_from_configuration_not_from_the_ticket(self):
        """A ticket that could choose the command could run anything."""
        source = Path(ticket_to_pr.__file__).read_text(encoding="utf-8")
        self.assertIn('command = ctx.config.get("test_command")', source)
        self.assertNotIn('ticket.get("test_command")', source)

    def test_a_timeout_is_reported_as_a_failure_not_a_pass(self):
        class Workspace:
            path = Path(".")

        with patch("engine.ticket_to_pr.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="x", timeout=1)):
            result = ticket_to_pr._run_tests(Workspace(), "pytest")
        self.assertEqual(result["status"], "failed")


if __name__ == "__main__":
    unittest.main()
