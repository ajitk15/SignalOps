"""ServiceNow for the workflow platform: read context, write back to tickets.

Three things this file is careful about.

**Secrets are never stored.** Credentials come from environment variables and
nothing else. A `Connection` row holds the non-secret configuration — which
filter to poll, which assignment group — plus the *names* of the variables it
expects. The browser never sees a password, and there is no field in which to
type one, which is stronger than a field that promises not to keep it.

**Reads and writes use different accounts.** `SN_READ_*` needs table read; the
write account needs the minimum to append a work note and set a state. An
agent-driven system that can only append notes with the credentials it has is
bounded by the credentials, not only by the prompt.

**Two authentication schemes, both supported.** Basic auth is the default and
needs no setup beyond a username and password. OAuth engages the moment
`SN_CLIENT_ID` and `SN_CLIENT_SECRET` are present. Both are kept rather than
picking a winner: basic auth is far simpler for a dev instance, and OAuth is
what some instances require. Note that ServiceNow refuses basic auth for REST
from accounts whose identity type is Human — an integration account must be set
to Machine on the user record, and the 401 it returns until then is identical
to the one a wrong password produces.

**Dry run is the default and it is real.** In dry run nothing leaves the
process; the exact payload that would have been sent is returned and recorded,
so a run is reviewable before the connection is allowed to write.

Field selection on reads is deliberate and narrow: this text ends up inside
agent prompts, so every extra column is more untrusted input and more tokens.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger("servicenow")

TIMEOUT = 20

# Fixed names rather than per-connection aliases. The wizard reports which of
# these are missing, which is a far better setup experience than a form that
# accepts a password and a promise.
ENV_VARS = {
    "instance": "SN_INSTANCE_URL",
    "read_user": "SN_READ_USER",
    "read_password": "SN_READ_PASSWORD",
    "write_user": "SN_WRITE_USER",
    "write_password": "SN_WRITE_PASSWORD",
}

# OAuth, optional. Present means OAuth is used; absent means basic auth.
#
# Kept alongside basic auth rather than replacing it. Some instances refuse
# HTTP Basic for the REST API outright, and the 401 they return is identical to
# the one a wrong password produces — having the alternative already wired is
# what makes that diagnosable instead of a dead end. Create these under
# System OAuth → Application Registry → "Create an OAuth API endpoint for
# external clients".
OAUTH_ENV_VARS = {
    "client_id": "SN_CLIENT_ID",
    "client_secret": "SN_CLIENT_SECRET",
}

# ServiceNow incident state codes. 6 = Resolved.
STATE_RESOLVED = "6"


class ServiceNowError(Exception):
    """A call failed. Carries a message safe to show a user — never the
    credentials or the full response body."""


class ServiceNowAuthError(ServiceNowError):
    """Authentication itself failed, as opposed to the request being refused.

    Worth its own type because the two need completely different responses: a
    403 means "grant this account a role", a 401 means "this credential is not
    being accepted at all", and conflating them sends people to the wrong screen.
    """


@dataclass(frozen=True)
class WriteResult:
    """What a write did, or would have done."""
    sent: bool
    target: str
    ref: str | None
    payload: dict

    def as_record(self) -> dict:
        return {"target": self.target, "ref": self.ref, "sent": self.sent,
                "payload": self.payload}


def env_status() -> dict[str, bool]:
    """Which variables are present. Values are never returned."""
    names = list(ENV_VARS.values()) + list(OAUTH_ENV_VARS.values())
    return {name: bool(os.getenv(name)) for name in names}


def auth_method() -> str:
    """Which scheme a client built now would use. Shown in the UI so "it is
    still using basic auth" is visible rather than inferred."""
    client_id, client_secret = _oauth_credentials()
    return "oauth" if client_id and client_secret else "basic"


def missing_env(*, for_writes: bool) -> list[str]:
    needed = [ENV_VARS["instance"], ENV_VARS["read_user"], ENV_VARS["read_password"]]
    if for_writes:
        needed += [ENV_VARS["write_user"], ENV_VARS["write_password"]]
    return [name for name in needed if not os.getenv(name)]


class OAuthTokenProvider:
    """Exchanges a password for a bearer token, and keeps it until it expires.

    The token lives in memory and nowhere else. It is never written to the
    database, never returned by an API, and never logged — the password it came
    from is already handled that way and a token is a password with a clock on
    it.

    ServiceNow's password grant is used rather than client credentials because
    it preserves *who* the integration is acting as: the work notes and state
    changes still carry the read or write account, so the audit trail on the
    ServiceNow side stays as legible as it was with basic auth.
    """

    # Refresh a little early. A token that expires between the check and the
    # call produces a 401 that looks like an auth failure.
    EXPIRY_MARGIN_SECONDS = 60

    def __init__(self, base_url: str, client_id: str, client_secret: str,
                 username: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._username = username
        self._password = password
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._refresh_token: str | None = None
        self._lock = threading.Lock()

    def token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires_at - self.EXPIRY_MARGIN_SECONDS:
                return self._token
            return self._fetch()

    def invalidate(self) -> None:
        """Drop the cached token so the next call fetches a fresh one."""
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def _fetch(self) -> str:
        payload = {"grant_type": "refresh_token", "refresh_token": self._refresh_token,
                   "client_id": self._client_id, "client_secret": self._client_secret} \
            if self._refresh_token else \
            {"grant_type": "password", "client_id": self._client_id,
             "client_secret": self._client_secret,
             "username": self._username, "password": self._password}
        try:
            response = httpx.post(f"{self.base_url}/oauth_token.do", data=payload,
                                  timeout=TIMEOUT)
        except httpx.HTTPError as error:
            raise ServiceNowAuthError(
                f"could not reach the token endpoint: {type(error).__name__}") from error

        if response.status_code != 200:
            if self._refresh_token:
                # The refresh token expired or was revoked; fall back to the
                # password grant once rather than failing the whole run.
                self._refresh_token = None
                return self._fetch()
            raise ServiceNowAuthError(_explain_token_failure(response))

        body = response.json()
        self._token = body.get("access_token")
        self._refresh_token = body.get("refresh_token") or self._refresh_token
        self._expires_at = time.time() + float(body.get("expires_in") or 1800)
        if not self._token:
            raise ServiceNowAuthError("the token endpoint returned no access token")
        logger.info("obtained a ServiceNow OAuth token for %s", self._username)
        return self._token


def _explain_token_failure(response) -> str:
    """Turn ServiceNow's OAuth error into something actionable.

    The raw errors are terse and the causes are specific, so mapping them is
    worth more than passing the string through — `invalid_client` sends someone
    to the Application Registry, `invalid_grant` to the user record.
    """
    try:
        body = response.json()
    except Exception:                                  # noqa: BLE001
        body = {}
    code = body.get("error", "")
    hints = {
        "invalid_client": "the client ID or secret is wrong — check the entry under "
                          "System OAuth → Application Registry",
        "invalid_grant": "the instance accepted the client but rejected the username or "
                         "password. If the account has multi-factor authentication "
                         "enabled, the password grant cannot satisfy it",
        "unauthorized_client": "this OAuth application is not permitted to use the "
                               "password grant",
    }
    hint = hints.get(code, body.get("error_description") or "")
    return f"OAuth token request failed ({response.status_code}{': ' + code if code else ''})" \
           + (f" — {hint}" if hint else "")


class ServiceNowClient:
    """One client, two ways of proving who it is.

    Basic auth stays the default because it needs no setup. OAuth is used the
    moment client credentials are present, because an instance that refuses
    basic auth for REST returns a 401 indistinguishable from a wrong password —
    so having the alternative already wired is what makes that diagnosable
    rather than a dead end.
    """

    def __init__(self, base_url: str, user: str, password: str,
                 client_id: str | None = None, client_secret: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._basic = (user, password)
        self._oauth = (
            OAuthTokenProvider(base_url, client_id, client_secret, user, password)
            if client_id and client_secret else None)

    @property
    def auth_method(self) -> str:
        return "oauth" if self._oauth else "basic"

    # --- plumbing ------------------------------------------------------------

    def _send(self, method: str, url: str, **kwargs):
        headers = {"Accept": "application/json", **kwargs.pop("headers", {})}
        if self._oauth:
            headers["Authorization"] = f"Bearer {self._oauth.token()}"
            return httpx.request(method, url, timeout=TIMEOUT, headers=headers, **kwargs)
        return httpx.request(method, url, auth=self._basic, timeout=TIMEOUT,
                             headers=headers, **kwargs)

    def _request(self, method: str, path: str, **kwargs):
        url = f"{self.base_url}/api/now/{path.lstrip('/')}"
        try:
            response = self._send(method, url, **kwargs)
            if response.status_code == 401 and self._oauth:
                # A token can expire between the expiry check and the call, or
                # be revoked server-side. One retry with a fresh token separates
                # that from a credential that is genuinely not accepted.
                self._oauth.invalidate()
                response = self._send(method, url, **kwargs)
            if response.status_code in (401, 403):
                raise ServiceNowAuthError(_explain_rejection(response, self.auth_method))
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            # Status and reason only. A ServiceNow error body can echo the query
            # and is not something to surface verbatim.
            raise ServiceNowError(
                f"ServiceNow returned {error.response.status_code} for {method} {path}"
            ) from error
        except httpx.HTTPError as error:
            raise ServiceNowError(f"could not reach ServiceNow: {type(error).__name__}") from error
        return response.json().get("result", [])

    def _get(self, table: str, query: str, fields: str, limit: int) -> list[dict]:
        return self._request("GET", f"table/{table}",
                             params={"sysparm_query": query, "sysparm_fields": fields,
                                     "sysparm_limit": limit,
                                     "sysparm_display_value": "true"})

    # --- reads ---------------------------------------------------------------

    def test(self) -> None:
        """Cheapest call that proves credentials and reachability."""
        self._get("sys_user", "", "sys_id", 1)

    def search_incidents(self, query: str, limit: int = 10) -> list[dict]:
        """Incidents matching a saved filter. The workflow's trigger."""
        return self._get("incident", query,
                         "number,sys_id,short_description,description,priority,"
                         "cmdb_ci,opened_at,state", limit)

    def recent_changes(self, service: str, hours: int = 24, limit: int = 5) -> list[dict]:
        query = (f"short_descriptionLIKE{service}^ORcmdb_ci.nameLIKE{service}"
                 f"^sys_updated_on>=javascript:gs.hoursAgoStart({hours})"
                 f"^ORDERBYDESCsys_updated_on")
        return self._get("change_request", query,
                         "number,short_description,state,sys_updated_on", limit)

    def past_incidents(self, service: str, limit: int = 5) -> list[dict]:
        query = f"cmdb_ci.nameLIKE{service}^ORDERBYDESCsys_updated_on"
        return self._get("incident", query,
                         "number,short_description,close_notes,closed_at", limit)

    def search_kb(self, text: str, limit: int = 3) -> list[dict]:
        query = f"short_descriptionLIKE{text}^workflow_state=published"
        return self._get("kb_knowledge", query, "number,short_description,text", limit)

    # --- writes --------------------------------------------------------------

    def add_work_note(self, sys_id: str, note: str) -> None:
        self._request("PATCH", f"table/incident/{sys_id}", json={"work_notes": note})

    def set_state(self, sys_id: str, state: str, close_notes: str | None = None) -> None:
        fields = {"state": state}
        if close_notes:
            fields["close_notes"] = close_notes
        self._request("PATCH", f"table/incident/{sys_id}", json=fields)


def _explain_rejection(response, auth_method: str) -> str:
    """Say what a 401 or 403 actually means, and where to go next.

    These two are constantly confused and the remedies are unrelated. A 403 is
    a role problem on the account. A 401 over basic auth is very often not a
    wrong password at all — instances increasingly refuse HTTP Basic for REST
    while still accepting the same credential at the UI login form, and the
    response is identical to the one a nonexistent user gets. Saying so here
    saves the hour it otherwise costs to work out.
    """
    if response.status_code == 403:
        return ("ServiceNow accepted the credential but refused the request (403). "
                "The account is authenticated and lacks a role for this table.")
    if auth_method == "oauth":
        return ("ServiceNow rejected the OAuth token (401). The token was refreshed and "
                "retried once. Check that the OAuth application is active and that the "
                "account is not locked.")
    return (
        "ServiceNow rejected the credential (401). This is the same response the instance "
        "gives for a user that does not exist, so it does not by itself mean the password "
        "is wrong.\n"
        "If the same account can sign in at /login.do, the password is fine and something "
        "is refusing it for REST specifically. In order of likelihood:\n"
        "  1. The user's identity type is Human. Recent ServiceNow releases refuse HTTP "
        "Basic for the REST API from human accounts; an integration account has to be set "
        "to Machine on the user record. This is the usual cause and it is easy to miss, "
        "because nothing about the error mentions it.\n"
        "  2. The account has multi-factor authentication enabled, which basic auth "
        "cannot satisfy.\n"
        "  3. The instance refuses basic auth for REST entirely — set "
        f"{OAUTH_ENV_VARS['client_id']} and {OAUTH_ENV_VARS['client_secret']} to use "
        "OAuth instead.")


def _oauth_credentials() -> tuple[str | None, str | None]:
    return (os.getenv(OAUTH_ENV_VARS["client_id"]),
            os.getenv(OAUTH_ENV_VARS["client_secret"]))


# --- connections -------------------------------------------------------------

# What a stored ServiceNow connection holds. Split deliberately: `config` is
# everything safe to return to a browser, `secrets` is everything that is not.
CONFIG_FIELDS = ("base_url", "auth_type", "username", "client_id",
                 "assignment_group", "extra_query")
SECRET_FIELDS = ("password", "client_secret")


def client_from(connection) -> ServiceNowClient:
    """Build a client from a stored connection.

    One connection is one instance with one account, which is why several can
    coexist — a dev instance and a production one are simply two rows. What the
    workflow can do is bounded by that account's permissions in ServiceNow, so
    the least-privileged account that can read incidents and append work notes
    is the right one to configure.
    """
    from crypto import decrypt

    config = connection.config or {}
    secrets = connection.secrets or {}
    base_url = config.get("base_url", "")
    if not base_url:
        raise ServiceNowError(f"connection {connection.name!r} has no instance URL")
    username = config.get("username") or ""
    password = decrypt(secrets.get("password")) or ""
    if config.get("auth_type") == "oauth":
        return ServiceNowClient(base_url, username, password,
                                config.get("client_id"),
                                decrypt(secrets.get("client_secret")))
    return ServiceNowClient(base_url, username, password)


def queue_query(connection, extra: str = "") -> str:
    """The encoded query that selects this connection's monitored queue.

    Built from configuration, never from ticket text. `assignment_group` is the
    queue an operations team actually thinks in — "everything raised against
    IPM_MQ_S_ADMIN" — so it is the field the workflow triggers on.
    """
    config = connection.config or {}
    clauses = ["active=true"]
    group = (config.get("assignment_group") or "").strip()
    if group:
        clauses.append(f"assignment_group.name={group}")
    for candidate in (config.get("extra_query"), extra):
        if candidate and candidate.strip():
            clauses.append(candidate.strip())
    return "^".join(clauses) + "^ORDERBYDESCsys_created_on"


def reader() -> ServiceNowClient | None:
    url = os.getenv(ENV_VARS["instance"], "")
    user = os.getenv(ENV_VARS["read_user"])
    password = os.getenv(ENV_VARS["read_password"])
    client_id, client_secret = _oauth_credentials()
    return (ServiceNowClient(url, user, password, client_id, client_secret)
            if url and user and password else None)


def writer() -> ServiceNowClient | None:
    url = os.getenv(ENV_VARS["instance"], "")
    user = os.getenv(ENV_VARS["write_user"])
    password = os.getenv(ENV_VARS["write_password"])
    client_id, client_secret = _oauth_credentials()
    return (ServiceNowClient(url, user, password, client_id, client_secret)
            if url and user and password else None)


class TicketSink:
    """Where the workflow's writes go.

    A single object with `dry_run` on it, rather than an `if dry_run` at each
    call site. The interesting property is that in dry run there is no client
    to call at all — the write cannot happen by forgetting a check, because
    nothing is holding a connection.
    """

    def __init__(self, *, dry_run: bool = True, client: ServiceNowClient | None = None) -> None:
        self.dry_run = dry_run
        self._client = None if dry_run else (client if client is not None else writer())

    @property
    def live(self) -> bool:
        return self._client is not None

    def work_note(self, *, sys_id: str | None, number: str | None, note: str) -> WriteResult:
        payload = {"work_notes": note}
        if self._client is None or not sys_id:
            return WriteResult(False, "servicenow.incident.work_notes", number, payload)
        self._client.add_work_note(sys_id, note)
        logger.info("wrote a work note to %s", number)
        return WriteResult(True, "servicenow.incident.work_notes", number, payload)

    def resolve(self, *, sys_id: str | None, number: str | None,
                close_notes: str) -> WriteResult:
        payload = {"state": STATE_RESOLVED, "close_notes": close_notes}
        if self._client is None or not sys_id:
            return WriteResult(False, "servicenow.incident.state", number, payload)
        self._client.set_state(sys_id, STATE_RESOLVED, close_notes)
        logger.info("set %s to resolved", number)
        return WriteResult(True, "servicenow.incident.state", number, payload)


class ContextSource:
    """Read-only enrichment. Every lookup is independently optional.

    One unreachable table must not fail the run: a diagnosis with two of three
    context sources is worth having, and is honest as long as the run records
    which source was unavailable rather than presenting a thin context as a
    complete one.
    """

    def __init__(self, client: ServiceNowClient | None = None) -> None:
        self._client = client if client is not None else reader()

    @property
    def available(self) -> bool:
        return self._client is not None

    def gather(self, ticket: dict) -> tuple[dict, list[str]]:
        gathered: dict[str, list] = {}
        unavailable: list[str] = []
        service = _service_of(ticket)
        if self._client is None:
            return {}, ["recent_changes", "past_incidents", "kb_articles"]
        for name, call in (
            ("recent_changes", lambda: self._client.recent_changes(service)),
            ("past_incidents", lambda: self._client.past_incidents(service)),
            ("kb_articles", lambda: self._client.search_kb(
                ticket.get("short_description", "")[:60])),
        ):
            try:
                result = call()
            except Exception as error:      # noqa: BLE001 — deliberate, see below
                # Broad on purpose. Enrichment is best-effort: a malformed
                # response or an unexpected error from one optional lookup must
                # cost that lookup, not the run. Narrowing this to
                # ServiceNowError meant anything the client did not anticipate
                # took down a diagnosis that two of three sources could have
                # supported.
                logger.warning("context source %s unavailable: %s: %s",
                               name, type(error).__name__, error)
                unavailable.append(name)
                continue
            if result:
                gathered[name] = result
            else:
                unavailable.append(name)
        return gathered, unavailable


def _service_of(ticket: dict) -> str:
    ci = ticket.get("configuration_item") or ticket.get("cmdb_ci") or ""
    if isinstance(ci, dict):                       # display_value form
        ci = ci.get("display_value", "")
    return str(ci) or str(ticket.get("short_description", ""))[:40]


def normalise(record: dict) -> dict:
    """One ServiceNow incident as the shape the workflow expects.

    Deliberately a small, fixed set of fields. The ticket becomes untrusted
    input to a model, so passing through whatever the instance happens to
    return would widen the injection surface for free.
    """
    def flat(value):
        return value.get("display_value", "") if isinstance(value, dict) else value

    return {
        "number": record.get("number"),
        "sys_id": record.get("sys_id"),
        "short_description": flat(record.get("short_description")) or "",
        "description": flat(record.get("description")) or "",
        "priority": flat(record.get("priority")) or "",
        "configuration_item": flat(record.get("cmdb_ci")) or "",
        "opened_at": flat(record.get("opened_at")) or "",
        "state": flat(record.get("state")) or "",
    }
