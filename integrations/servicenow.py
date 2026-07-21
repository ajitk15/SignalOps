"""ServiceNow integration: ticket sink (outbox) and read-only context source.

Split credentials on purpose: reads (change requests, past incidents) use a
read-only account; writes (incident creation) use a separate account with the
minimum table permissions. Values come from the environment only.

Delivery is an outbox: incidents lacking a ServiceNow ref ARE the queue, so
retry and restart-reconciliation come free and one incident is one ticket by
construction. SERVICENOW_MODE=dry_run (default) logs the exact payload it
would send; "live" creates tickets; "off" disables the worker.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import httpx
import yaml

import store
from events import Event, bus

logger = logging.getLogger("servicenow")

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "servicenow.yaml"
_DEFAULT_CONFIG = {
    "assignment_group": "InfraSupport",
    "category": "Middleware",
    "severity_map": {"P1": {"urgency": 1, "impact": 1}, "P2": {"urgency": 2, "impact": 2},
                     "P3": {"urgency": 3, "impact": 3}, "P4": {"urgency": 3, "impact": 3}},
}
MAX_DELIVERY_ATTEMPTS = 5
_attempts: dict[int, int] = {}


def delivery_config() -> dict:
    if CONFIG_PATH.exists():
        return {**_DEFAULT_CONFIG, **(yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {})}
    return dict(_DEFAULT_CONFIG)


def mode() -> str:
    return os.getenv("SERVICENOW_MODE", "dry_run").lower()


class ServiceNowClient:
    def __init__(self, base_url: str, user: str, password: str):
        self.base_url = base_url.rstrip("/")
        self._auth = (user, password)

    def _get(self, table: str, query: str, fields: str, limit: int) -> list[dict]:
        response = httpx.get(f"{self.base_url}/api/now/table/{table}",
                             params={"sysparm_query": query, "sysparm_fields": fields,
                                     "sysparm_limit": limit},
                             auth=self._auth, timeout=15,
                             headers={"Accept": "application/json"})
        response.raise_for_status()
        return response.json().get("result", [])

    # -- read-only context ---------------------------------------------------
    def recent_changes(self, service: str, hours: int = 24, limit: int = 5) -> list[dict]:
        """Change requests touching this service recently — the 'what changed?'
        evidence. Compact fields only: this text reaches agent prompts."""
        query = (f"short_descriptionLIKE{service}^ORcmdb_ci.nameLIKE{service}"
                 f"^sys_updated_on>=javascript:gs.hoursAgoStart({hours})^ORDERBYDESCsys_updated_on")
        return self._get("change_request", query,
                         "number,short_description,state,sys_updated_on", limit)

    def past_incidents(self, service: str, limit: int = 5) -> list[dict]:
        query = f"short_descriptionLIKE{service}^ORDERBYDESCsys_updated_on"
        return self._get("incident", query, "number,short_description,state,closed_at", limit)

    def test(self) -> None:
        self._get("sys_user", "", "sys_id", 1)

    # -- write path ----------------------------------------------------------
    def create_incident(self, fields: dict) -> dict:
        response = httpx.post(f"{self.base_url}/api/now/table/incident", json=fields,
                              auth=self._auth, timeout=20,
                              headers={"Accept": "application/json"})
        response.raise_for_status()
        result = response.json()["result"]
        return {"number": result.get("number"), "sys_id": result.get("sys_id")}


def reader_from_env() -> ServiceNowClient | None:
    url = os.getenv("SN_INSTANCE_URL", "")
    if url and os.getenv("SN_READ_USER") and os.getenv("SN_READ_PASSWORD"):
        return ServiceNowClient(url, os.environ["SN_READ_USER"], os.environ["SN_READ_PASSWORD"])
    return None


def writer_from_env() -> ServiceNowClient | None:
    url = os.getenv("SN_INSTANCE_URL", "")
    if url and os.getenv("SN_WRITE_USER") and os.getenv("SN_WRITE_PASSWORD"):
        return ServiceNowClient(url, os.environ["SN_WRITE_USER"], os.environ["SN_WRITE_PASSWORD"])
    return None


def ticket_payload(incident: dict) -> dict:
    """Map a stored incident onto ServiceNow incident fields. Config, not code,
    decides assignment and severity mapping."""
    config = delivery_config()
    mapped = config["severity_map"].get(incident.get("severity") or "P4", {"urgency": 3, "impact": 3})
    return {
        "short_description": f"[SignalOps #{incident['id']}] {incident.get('title') or incident['object_name']}",
        "description": incident.get("markdown_report") or "",
        "urgency": mapped["urgency"], "impact": mapped["impact"],
        "assignment_group": config["assignment_group"], "category": config["category"],
        "cmdb_ci": incident.get("object_name", ""),
    }


async def deliver_forever(poll_seconds: int = 30) -> None:
    """Outbox worker. Escalation only, never remediation: it opens tickets, it
    does not act on systems."""
    current_mode = mode()
    if current_mode == "off":
        logger.info("ServiceNow delivery disabled (SERVICENOW_MODE=off)")
        return
    if current_mode == "live" and writer_from_env() is None:
        logger.error("SERVICENOW_MODE=live but SN_INSTANCE_URL/SN_WRITE_USER/SN_WRITE_PASSWORD "
                     "are not all set — falling back to dry_run")
        current_mode = "dry_run"
    logger.info("ServiceNow delivery worker started (mode=%s)", current_mode)
    while True:
        try:
            for incident in store.incidents_missing_ref("servicenow"):
                if _attempts.get(incident["id"], 0) >= MAX_DELIVERY_ATTEMPTS:
                    continue
                _attempts[incident["id"]] = _attempts.get(incident["id"], 0) + 1
                payload = ticket_payload(incident)
                if current_mode == "dry_run":
                    logger.info("DRY RUN would create ServiceNow incident for #%d: %s",
                                incident["id"], json.dumps(payload, ensure_ascii=False))
                    store.set_external_ref(incident["id"], "servicenow",
                                           {"mode": "dry_run", "number": None})
                    continue
                writer = writer_from_env()
                ref = writer.create_incident(payload)
                store.set_external_ref(incident["id"], "servicenow", ref)
                bus.publish(Event("ticket_created", {"incident_id": incident["id"], **ref}))
                logger.info("created ServiceNow incident %s for #%d", ref.get("number"), incident["id"])
        except Exception:
            logger.exception("ServiceNow delivery sweep failed; will retry")
        await asyncio.sleep(poll_seconds)
