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

**Dry run is the default and it is real.** In dry run nothing leaves the
process; the exact payload that would have been sent is returned and recorded,
so a run is reviewable before the connection is allowed to write.

Field selection on reads is deliberate and narrow: this text ends up inside
agent prompts, so every extra column is more untrusted input and more tokens.
"""
from __future__ import annotations

import logging
import os
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

# ServiceNow incident state codes. 6 = Resolved.
STATE_RESOLVED = "6"


class ServiceNowError(Exception):
    """A call failed. Carries a message safe to show a user — never the
    credentials or the full response body."""


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
    return {name: bool(os.getenv(name)) for name in ENV_VARS.values()}


def missing_env(*, for_writes: bool) -> list[str]:
    needed = [ENV_VARS["instance"], ENV_VARS["read_user"], ENV_VARS["read_password"]]
    if for_writes:
        needed += [ENV_VARS["write_user"], ENV_VARS["write_password"]]
    return [name for name in needed if not os.getenv(name)]


class ServiceNowClient:
    def __init__(self, base_url: str, user: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._auth = (user, password)

    # --- plumbing ------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs):
        url = f"{self.base_url}/api/now/{path.lstrip('/')}"
        try:
            response = httpx.request(method, url, auth=self._auth, timeout=TIMEOUT,
                                     headers={"Accept": "application/json"}, **kwargs)
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


def reader() -> ServiceNowClient | None:
    url = os.getenv(ENV_VARS["instance"], "")
    user = os.getenv(ENV_VARS["read_user"])
    password = os.getenv(ENV_VARS["read_password"])
    return ServiceNowClient(url, user, password) if url and user and password else None


def writer() -> ServiceNowClient | None:
    url = os.getenv(ENV_VARS["instance"], "")
    user = os.getenv(ENV_VARS["write_user"])
    password = os.getenv(ENV_VARS["write_password"])
    return ServiceNowClient(url, user, password) if url and user and password else None


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
