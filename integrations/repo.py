"""The repository a code workflow works in, and the pull request it opens.

Three boundaries, each enforced somewhere different on purpose, because a
single check is a single thing to get wrong.

**A throwaway clone.** Every run gets its own shallow clone in a temp directory
that is deleted afterwards. The agent never sees a working copy anyone cares
about, and "it edited the wrong checkout" is not a failure mode that exists.

**A branch, never the default.** Commits land on a per-run branch. `push()`
refuses to push to the repository's default branch, and there is no merge
method on this class at all — not a disabled one, not one behind a flag.

**A path allowlist.** CI configuration, infrastructure, dependency manifests
and anything that looks like a secret are refused. An agent that can edit
`.github/workflows` can run arbitrary code on the next push, which makes the
review gate decorative.

The repository URL and branch come from validated configuration and never from
ticket text — a ticket that could name the repository would be a ticket that
could choose its own target.
"""
from __future__ import annotations

import fnmatch
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import httpx

logger = logging.getLogger("repo")

# Paths an agent may never write, whatever the ticket says. Globs, matched
# against the repo-relative POSIX path.
PROTECTED_PATTERNS = (
    ".github/*", ".github/**/*", ".gitlab-ci.yml", ".circleci/*", "Jenkinsfile",
    "azure-pipelines.yml", ".buildkite/**/*",
    "Dockerfile", "docker-compose*.yml", "*.tf", "*.tfvars", "helm/**/*",
    "k8s/**/*", "kubernetes/**/*", "charts/**/*",
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*",
    "secrets/**/*", "**/secrets/**/*",
    ".git/**/*", ".claude/**/*",
)

# Dependency manifests are separate: sometimes a fix legitimately needs one, so
# they are refused by default but a workflow can opt in. CI and secrets have no
# opt-in at all.
DEPENDENCY_PATTERNS = (
    "requirements*.txt", "pyproject.toml", "package.json", "package-lock.json",
    "yarn.lock", "poetry.lock", "go.mod", "go.sum", "Gemfile*", "pom.xml",
    "build.gradle*", "Cargo.toml", "Cargo.lock",
)

MAX_TREE_ENTRIES = 400
MAX_FILE_BYTES = 200_000
CLONE_TIMEOUT = 180
GIT_TIMEOUT = 60


class RepoError(Exception):
    """Something went wrong with the checkout or the remote."""


class PathRefused(Exception):
    """A write was attempted outside the allowlist."""


@dataclass(frozen=True)
class PullRequest:
    opened: bool
    url: str | None
    branch: str
    title: str
    body: str

    def as_record(self) -> dict:
        return {"target": "git.pull_request", "ref": self.url, "sent": self.opened,
                "payload": {"branch": self.branch, "title": self.title}}


def _normalise(path: str) -> str:
    """Repo-relative POSIX form, without mangling dotfiles.

    Written out rather than using `lstrip("./")`, which strips *characters* and
    not a prefix: it turns `.github/workflows/ci.yml` into `github/...` and
    `.env` into `env`, quietly disabling every dot-prefixed rule below.
    """
    normalised = str(path).replace("\\", "/")
    while normalised.startswith("./"):
        normalised = normalised[2:]
    return normalised


def _matches(path: str, pattern: str) -> bool:
    """fnmatch, with two corrections that matter here.

    **`**` means "zero or more directories".** fnmatch has no notion of it; its
    `*` already crosses `/`, so `secrets/**/*` alone would require at least two
    path segments and let `secrets/db.yaml` through.

    **A pattern with no `/` matches the file wherever it lives.** `id_rsa*` and
    `.env` are descriptions of a *kind of file*, not of a location, so matching
    only at the repository root left `config/id_rsa` and `app/.env` writable —
    which is precisely where a real one tends to sit.
    """
    if fnmatch.fnmatch(path, pattern):
        return True
    if "/" not in pattern and fnmatch.fnmatch(PurePosixPath(path).name, pattern):
        return True
    collapsed = pattern.replace("/**/", "/")
    if collapsed != pattern and fnmatch.fnmatch(path, collapsed):
        return True
    if pattern.startswith("**/") and _matches(path, pattern[3:]):
        return True
    return False


def is_protected(path: str, *, allow_dependencies: bool = False) -> str | None:
    """Return the reason a path is refused, or None if it is writable."""
    raw = str(path).replace("\\", "/")
    # Checked before normalising: a traversal segment must be refused, never
    # tidied away into something that looks like an ordinary relative path.
    if raw.startswith("/") or raw[1:3] == ":/" or ".." in PurePosixPath(raw).parts:
        return f"{raw} escapes the repository"
    normalised = _normalise(raw)
    for pattern in PROTECTED_PATTERNS:
        if _matches(normalised, pattern):
            return f"{normalised} is CI, infrastructure or secret material"
    if not allow_dependencies:
        for pattern in DEPENDENCY_PATTERNS:
            if _matches(normalised, pattern):
                return (f"{normalised} is a dependency manifest; this workflow is not "
                        "configured to change dependencies")
    return None


def _run(args: list[str], cwd: Path | None = None, timeout: int = GIT_TIMEOUT) -> str:
    try:
        completed = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout,
            # A git that stops to ask for credentials would hang the run.
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"})
    except subprocess.TimeoutExpired as error:
        raise RepoError(f"git timed out after {timeout}s: {' '.join(args[:3])}") from error
    if completed.returncode != 0:
        raise RepoError(_scrub(completed.stderr.strip() or completed.stdout.strip()))
    return completed.stdout


def _scrub(text: str) -> str:
    """Never let a token reach a log line or an error message."""
    return re.sub(r"(https://)[^@/\s]+@", r"\1***@", text or "")[:500]


class RepoWorkspace:
    """A disposable checkout for one run."""

    def __init__(self, *, url: str, base_branch: str = "main", branch: str,
                 token: str | None = None, allow_dependencies: bool = False) -> None:
        self.url = url
        self.base_branch = base_branch
        self.branch = branch
        self.allow_dependencies = allow_dependencies
        self._token = token
        self._dir: Path | None = None

    @property
    def path(self) -> Path:
        if self._dir is None:
            raise RepoError("workspace has not been cloned")
        return self._dir

    def _authenticated_url(self) -> str:
        if self._token and self.url.startswith("https://"):
            return self.url.replace("https://", f"https://x-access-token:{self._token}@", 1)
        return self.url

    def clone(self) -> Path:
        self._dir = Path(tempfile.mkdtemp(prefix="signalops-repo-"))
        # Shallow and single-branch: this is a working copy for one change, not
        # a mirror, and history costs time and disk for no benefit here.
        _run(["git", "clone", "--depth", "1", "--branch", self.base_branch,
              "--single-branch", self._authenticated_url(), str(self._dir)],
             timeout=CLONE_TIMEOUT)
        _run(["git", "checkout", "-b", self.branch], cwd=self._dir)
        _run(["git", "config", "user.name", "SignalOps bot"], cwd=self._dir)
        _run(["git", "config", "user.email", "signalops-bot@users.noreply.github.com"],
             cwd=self._dir)
        return self._dir

    def tree(self, limit: int = MAX_TREE_ENTRIES) -> list[str]:
        """Tracked files, so the locator picks from what exists."""
        output = _run(["git", "ls-files"], cwd=self.path)
        return output.splitlines()[:limit]

    def read(self, relative: str) -> str:
        reason = is_protected(relative, allow_dependencies=True)
        if reason and "escapes" in reason:
            raise PathRefused(reason)
        target = (self.path / relative).resolve()
        if not str(target).startswith(str(self.path.resolve())):
            raise PathRefused(f"{relative} resolves outside the repository")
        if not target.is_file():
            raise PathRefused(f"{relative} is not a file in this repository")
        return target.read_text(encoding="utf-8", errors="replace")[:MAX_FILE_BYTES]

    def changed_files(self) -> list[str]:
        _run(["git", "add", "-A"], cwd=self.path)
        return [line.strip() for line in
                _run(["git", "diff", "--cached", "--name-only"], cwd=self.path).splitlines()
                if line.strip()]

    def assert_changes_allowed(self) -> None:
        """Last line of defence, after the agent has finished.

        The tool-call hook refuses a protected write as it happens; this catches
        anything that arrived another way, and runs before a commit exists.
        """
        refused = [(path, reason) for path in self.changed_files()
                   if (reason := is_protected(path, allow_dependencies=self.allow_dependencies))]
        if refused:
            raise PathRefused("; ".join(f"{p}: {r}" for p, r in refused))

    def diff(self, max_bytes: int = 60_000) -> str:
        _run(["git", "add", "-A"], cwd=self.path)
        return _run(["git", "diff", "--cached"], cwd=self.path)[:max_bytes]

    def commit(self, message: str) -> str | None:
        if not self.changed_files():
            return None
        _run(["git", "commit", "-m", message], cwd=self.path)
        return _run(["git", "rev-parse", "HEAD"], cwd=self.path).strip()

    def push(self) -> None:
        if self.branch == self.base_branch:
            raise RepoError("refusing to push to the base branch")
        _run(["git", "push", "--set-upstream", self._authenticated_url(),
              f"HEAD:{self.branch}"], cwd=self.path)

    def apply_patch(self, patch: str) -> None:
        """Re-apply a checkpointed diff to a fresh clone.

        This is what lets a review gate outlive the checkout the diff was made
        in. A patch that no longer applies means the base branch moved under
        the change, which has to be a loud failure — silently opening a pull
        request from a stale base is exactly the surprise a review gate exists
        to prevent.
        """
        if not patch.strip():
            return
        patch_file = self.path / ".signalops.patch"
        patch_file.write_text(patch if patch.endswith("\n") else patch + "\n",
                              encoding="utf-8")
        try:
            _run(["git", "apply", "--whitespace=nowarn", str(patch_file)], cwd=self.path)
        except RepoError as error:
            raise RepoError(
                "the approved change no longer applies to the base branch — it has "
                f"moved since the diff was reviewed ({error}). Re-run the ticket."
            ) from error
        finally:
            patch_file.unlink(missing_ok=True)

    def cleanup(self) -> None:
        if self._dir and self._dir.exists():
            shutil.rmtree(self._dir, ignore_errors=True)
        self._dir = None

    def __enter__(self):
        self.clone()
        return self

    def __exit__(self, *exc):
        self.cleanup()
        return False


class PullRequestSink:
    """Opens the pull request. Deliberately has no merge method.

    Same shape as the ticket sink: in dry run it holds no token, so there is
    nothing to push or open with even if a node forgot to check.
    """

    def __init__(self, *, dry_run: bool = True, token: str | None = None,
                 api_url: str = "https://api.github.com") -> None:
        self.dry_run = dry_run
        self._token = None if dry_run else (token or os.getenv("GIT_BOT_TOKEN"))
        self._api = api_url.rstrip("/")

    @property
    def live(self) -> bool:
        return self._token is not None

    def open(self, *, repo: str, branch: str, base: str, title: str, body: str) -> PullRequest:
        if not self.live:
            return PullRequest(False, None, branch, title, body)
        try:
            response = httpx.post(
                f"{self._api}/repos/{repo}/pulls",
                json={"title": title, "head": branch, "base": base, "body": body,
                      # Never auto-merge, and never let a maintainer's tooling
                      # treat this as ready without a human reading it.
                      "draft": True},
                headers={"Authorization": f"Bearer {self._token}",
                         "Accept": "application/vnd.github+json"},
                timeout=30)
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise RepoError(
                f"GitHub returned {error.response.status_code} opening the pull request"
            ) from error
        except httpx.HTTPError as error:
            raise RepoError(f"could not reach GitHub: {type(error).__name__}") from error
        return PullRequest(True, response.json().get("html_url"), branch, title, body)


def bot_token() -> str | None:
    return os.getenv("GIT_BOT_TOKEN")


def missing_env() -> list[str]:
    return [name for name in ("GIT_BOT_TOKEN",) if not os.getenv(name)]
