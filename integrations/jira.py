"""Jira for the workflow platform: read issues, write comments back.

Deliberately the same shape as integrations/servicenow.py — a client built from
a stored connection, a detached-settings carrier, presence-only secret
reporting, and errors that never carry the token. Two integrations that behave
differently under the hood are two integrations to remember the quirks of.

Jira Cloud authenticates the REST API with **email + API token** as HTTP Basic,
not a password. That is a real difference from a human login and worth stating
in the UI, because "use your Jira password" is the wrong instinct and fails the
same opaque way a bad ServiceNow credential does. The token is created at
id.atlassian.com → Security → API tokens.

The queue equivalent is a **project key** (or a full JQL for anything more
specific): new issues in that project are what a workflow triggers on.
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger("jira")

TIMEOUT = 20


class JiraError(Exception):
    """A call failed. Safe to show a user — never the token or the raw body."""


class JiraAuthError(JiraError):
    """Authentication itself failed, as opposed to a request being refused."""


class JiraClient:
    def __init__(self, base_url: str, email: str, api_token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._auth = (email, api_token)

    def _request(self, method: str, path: str, **kwargs):
        url = f"{self.base_url}/rest/api/3/{path.lstrip('/')}"
        try:
            response = httpx.request(method, url, auth=self._auth, timeout=TIMEOUT,
                                     headers={"Accept": "application/json"}, **kwargs)
            if response.status_code in (401, 403):
                raise JiraAuthError(_explain_rejection(response))
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise JiraError(
                f"Jira returned {error.response.status_code} for {method} {path}"
            ) from error
        except httpx.HTTPError as error:
            raise JiraError(f"could not reach Jira: {type(error).__name__}") from error
        return response.json()

    def test(self) -> None:
        """Cheapest call that proves the credentials: who am I."""
        self._request("GET", "myself")

    def search(self, jql: str, limit: int = 10) -> list[dict]:
        body = self._request("GET", "search",
                             params={"jql": jql, "maxResults": limit,
                                     "fields": "summary,description,priority,status,created"})
        return body.get("issues", [])

    def add_comment(self, issue_key: str, comment: str) -> None:
        # Jira Cloud wants Atlassian Document Format for comment bodies.
        self._request("POST", f"issue/{issue_key}/comment",
                      json={"body": _adf(comment)})


def _adf(text: str) -> dict:
    """Wrap plain text in the minimal Atlassian Document Format Jira accepts."""
    return {"type": "doc", "version": 1,
            "content": [{"type": "paragraph",
                         "content": [{"type": "text", "text": text}]}]}


def _explain_rejection(response) -> str:
    if response.status_code == 403:
        return ("Jira accepted the credential but refused the request (403). The account "
                "is authenticated and lacks permission for this project or action.")
    return ("Jira rejected the credential (401). Jira Cloud authenticates the REST API "
            "with your email address and an API token — not your password. Create a "
            "token at id.atlassian.com → Security → API tokens, and use your account "
            "email as the username.")


def project_query(connection, extra: str = "") -> str:
    """The JQL that selects this connection's monitored project.

    Built from configuration — a project key, or a full JQL for anything more
    specific — never from issue text.
    """
    config = connection.config or {}
    explicit = (config.get("jql") or "").strip()
    if explicit:
        clauses = [f"({explicit})"]
    else:
        project = (config.get("project_key") or "").strip()
        clauses = [f"project = {project}"] if project else []
    if extra and extra.strip():
        clauses.append(f"({extra.strip()})")
    if not clauses:
        clauses = ["created >= -7d"]
    return " AND ".join(clauses) + " ORDER BY created DESC"


def client_from(connection) -> JiraClient:
    from crypto import decrypt

    config = connection.config or {}
    secrets = connection.secrets or {}
    base_url = config.get("base_url", "")
    if not base_url:
        raise JiraError(f"connection {connection.name!r} has no Jira URL")
    return JiraClient(base_url, config.get("username") or "",
                      decrypt(secrets.get("api_token")) or "")


def normalise(issue: dict) -> dict:
    """One Jira issue as the fixed shape a workflow expects.

    Narrow on purpose, exactly as ServiceNow.normalise is: the issue becomes
    untrusted model input, so passing through every field Jira returns would
    widen the injection surface for free.
    """
    fields = issue.get("fields", {})
    description = fields.get("description")
    if isinstance(description, dict):
        description = _text_of(description)
    return {
        "number": issue.get("key"),
        "sys_id": issue.get("key"),          # Jira has no separate sys_id
        "short_description": fields.get("summary") or "",
        "description": description or "",
        "priority": (fields.get("priority") or {}).get("name", ""),
        "configuration_item": "",
        "state": (fields.get("status") or {}).get("name", ""),
        "opened_at": fields.get("created") or "",
    }


def _text_of(adf: dict) -> str:
    """Flatten an Atlassian Document Format body to plain text.

    Adjacent text nodes are concatenated with nothing between them — that is how
    Jira splits a run of text across marks — while block nodes (paragraphs, list
    items) are separated by a newline so words do not run together across them.
    """
    parts: list[str] = []

    def walk(node):
        if not isinstance(node, dict):
            return
        node_type = node.get("type")
        if node_type == "text":
            parts.append(node.get("text", ""))
        for child in node.get("content", []) or []:
            walk(child)
        if node_type in ("paragraph", "heading", "listItem", "blockquote"):
            parts.append("\n")

    walk(adf)
    return "".join(parts).strip()


def env_client() -> JiraClient | None:
    url = os.getenv("JIRA_URL", "")
    email = os.getenv("JIRA_EMAIL")
    token = os.getenv("JIRA_API_TOKEN")
    return JiraClient(url, email, token) if url and email and token else None
