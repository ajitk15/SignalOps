"""SQLite persistence for incidents, their lifecycle, and the audit trail."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("store")

DB_PATH = Path(__file__).resolve().parent / "data" / "incidents.db"

# An incident is "active" while it still needs attention; terminal states mean
# a human has finished with it. Recurrence behaviour keys off this split.
ACTIVE_STATUSES = ("open", "acknowledged")
TERMINAL_STATUSES = ("resolved", "closed", "false_positive")
STATUSES = ACTIVE_STATUSES + TERMINAL_STATUSES

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_name TEXT NOT NULL,
    object_type TEXT NOT NULL,
    severity TEXT,
    title TEXT,
    markdown_report TEXT,
    watcher_json TEXT,
    diagnosis_json TEXT,
    report_json TEXT,
    total_cost_usd REAL,
    trigger_source TEXT NOT NULL DEFAULT 'poll',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    detail TEXT
);
"""


# Ordered migrations for pre-existing databases. Each runs once per process;
# "duplicate column" errors mean the DB is already current and are ignored.
MIGRATIONS = [
    "ALTER TABLE incidents ADD COLUMN trigger_source TEXT NOT NULL DEFAULT 'poll'",
    "ALTER TABLE incidents ADD COLUMN external_refs TEXT",
    "ALTER TABLE incidents ADD COLUMN status TEXT NOT NULL DEFAULT 'open'",
    "ALTER TABLE incidents ADD COLUMN assignee TEXT",
    "ALTER TABLE incidents ADD COLUMN resolution_note TEXT",
    "ALTER TABLE incidents ADD COLUMN resolved_at REAL",
    "ALTER TABLE incidents ADD COLUMN closed_at REAL",
    "ALTER TABLE incidents ADD COLUMN previous_incident_id INTEGER",
    "ALTER TABLE incidents ADD COLUMN reopen_count INTEGER NOT NULL DEFAULT 0",
    # Promoted out of watcher_json: recurrence looks this up on every finding,
    # and a JSON scan per observation is the wrong shape.
    "ALTER TABLE incidents ADD COLUMN fingerprint TEXT",
    "CREATE INDEX IF NOT EXISTS idx_incidents_fingerprint ON incidents (fingerprint, id DESC)",
    # Backfill for rows written before the column existed.
    "UPDATE incidents SET fingerprint = json_extract(watcher_json, '$.fingerprint') "
    "WHERE fingerprint IS NULL AND watcher_json IS NOT NULL",
]
# Keyed by DB path (not a bool) so tests pointing DB_PATH at a fresh temp file
# get their schema created too.
_migrated: set[str] = set()


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    if str(DB_PATH) not in _migrated:
        conn.executescript(SCHEMA)
        for statement in MIGRATIONS:
            try:
                conn.execute(statement)
            except sqlite3.OperationalError:
                pass  # column/index already exists
        conn.commit()
        _migrated.add(str(DB_PATH))
    return conn


def save_incident(
    *,
    object_name: str,
    object_type: str,
    severity: str,
    title: str,
    markdown_report: str,
    watcher_json: dict,
    diagnosis_json: dict,
    report_json: dict,
    total_cost_usd: float,
    trigger_source: str = "poll",
    created_at: float | None = None,
    fingerprint: str | None = None,
    previous_incident_id: int | None = None,
) -> int:
    conn = _connect()
    try:
      with conn:
        cur = conn.execute(
            """INSERT INTO incidents
               (object_name, object_type, severity, title, markdown_report,
                watcher_json, diagnosis_json, report_json, total_cost_usd,
                trigger_source, created_at, fingerprint, previous_incident_id, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')""",
            (
                object_name, object_type, severity, title, markdown_report,
                json.dumps(watcher_json), json.dumps(diagnosis_json), json.dumps(report_json),
                total_cost_usd, trigger_source,
                # Callers that also announce the incident pass their own value so
                # the event and the stored row report the same creation time.
                time.time() if created_at is None else created_at,
                fingerprint, previous_incident_id,
            ),
        )
        return cur.lastrowid
    finally:
        conn.close()


# --- lifecycle ---------------------------------------------------------------

_LIFECYCLE_COLS = ["id", "object_name", "severity", "title", "status", "assignee",
                   "resolution_note", "created_at", "resolved_at", "closed_at",
                   "previous_incident_id", "reopen_count", "fingerprint", "external_refs"]


def latest_incident_for_fingerprint(fingerprint: str) -> dict[str, Any] | None:
    """Most recent incident for a fingerprint — the input to the recurrence
    decision (suppress / reopen / new)."""
    conn = _connect()
    try:
        row = conn.execute(
            f"SELECT {', '.join(_LIFECYCLE_COLS)} FROM incidents "
            "WHERE fingerprint = ? ORDER BY id DESC LIMIT 1", (fingerprint,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    record = dict(zip(_LIFECYCLE_COLS, row))
    record["external_refs"] = json.loads(record["external_refs"]) if record["external_refs"] else None
    return record


def set_incident_status(incident_id: int, status: str, *, actor: str = "dashboard",
                        note: str | None = None, assignee: str | None = None) -> dict | None:
    """Move an incident to a new status, stamping the matching timestamp.

    Reopening clears the resolution timestamps so MTTR is measured from the
    reopen, and bumps reopen_count so the row can show it recurred.
    """
    if status not in STATUSES:
        raise ValueError(f"unknown status '{status}'")
    now = time.time()
    conn = _connect()
    try:
        with conn:
            fields = {"status": status}
            if status == "resolved":
                fields["resolved_at"] = now
            elif status in ("closed", "false_positive"):
                fields["closed_at"] = now
                # Closing straight from open is the common path; without this
                # MTTR would silently ignore every incident that skipped the
                # explicit resolve step. A false positive was never a real
                # incident, so it stays out of the measure.
                if status == "closed":
                    row = conn.execute("SELECT resolved_at FROM incidents WHERE id = ?",
                                       (incident_id,)).fetchone()
                    if row and row[0] is None:
                        fields["resolved_at"] = now
            elif status == "open":  # reopen
                fields["resolved_at"] = None
                fields["closed_at"] = None
            if note is not None:
                fields["resolution_note"] = note
            if assignee is not None:
                fields["assignee"] = assignee
            assignments = ", ".join(f"{key} = ?" for key in fields)
            conn.execute(f"UPDATE incidents SET {assignments} WHERE id = ?",
                         (*fields.values(), incident_id))
            if status == "open":
                conn.execute("UPDATE incidents SET reopen_count = reopen_count + 1 WHERE id = ?",
                             (incident_id,))
    finally:
        conn.close()
    audit(actor=actor, action=f"incident_{status}", entity_type="incident",
          entity_id=str(incident_id), detail={"note": note} if note else None)
    return get_incident(incident_id)


# --- audit -------------------------------------------------------------------

def audit(*, actor: str, action: str, entity_type: str, entity_id: str,
          detail: dict | None = None) -> None:
    """Record an action. Never raises: losing an audit line is strictly better
    than refusing the legitimate operation it was recording."""
    try:
        conn = _connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO audit_log (ts, actor, action, entity_type, entity_id, detail) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (time.time(), actor or "unknown", action, entity_type, entity_id,
                     json.dumps(detail) if detail else None))
        finally:
            conn.close()
    except Exception:
        logger.exception("audit write failed for %s on %s %s", action, entity_type, entity_id)


def audit_entries(entity_type: str | None = None, entity_id: str | None = None,
                  limit: int = 100) -> list[dict[str, Any]]:
    query = "SELECT id, ts, actor, action, entity_type, entity_id, detail FROM audit_log"
    params: list[Any] = []
    if entity_type:
        query += " WHERE entity_type = ?"
        params.append(entity_type)
        if entity_id:
            query += " AND entity_id = ?"
            params.append(entity_id)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    conn = _connect()
    try:
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()
    cols = ["id", "ts", "actor", "action", "entity_type", "entity_id", "detail"]
    entries = [dict(zip(cols, row)) for row in rows]
    for entry in entries:
        entry["detail"] = json.loads(entry["detail"]) if entry["detail"] else None
    return entries


def incident_metrics() -> dict[str, Any]:
    """Counts by status plus MTTR over resolved incidents."""
    conn = _connect()
    try:
        counts = dict(conn.execute("SELECT status, COUNT(*) FROM incidents GROUP BY status").fetchall())
        mttr = conn.execute(
            "SELECT AVG(resolved_at - created_at) FROM incidents WHERE resolved_at IS NOT NULL"
        ).fetchone()[0]
    finally:
        conn.close()
    return {"counts": counts, "mttr_seconds": mttr}


def list_incidents(limit: int = 50) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, object_name, object_type, severity, title, total_cost_usd, trigger_source, created_at, external_refs, "
            "status, assignee, resolved_at, closed_at, previous_incident_id, reopen_count "
            "FROM incidents ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()
    finally:
        conn.close()
    cols = ["id", "object_name", "object_type", "severity", "title", "total_cost_usd", "trigger_source", "created_at", "external_refs",
            "status", "assignee", "resolved_at", "closed_at", "previous_incident_id", "reopen_count"]
    records = [dict(zip(cols, row)) for row in rows]
    for record in records:
        record["external_refs"] = json.loads(record["external_refs"]) if record["external_refs"] else None
    return records


def set_external_ref(incident_id: int, system: str, ref: dict) -> None:
    """Record an external system's reference (e.g. a ServiceNow ticket) on an
    incident. One ref per system — delivery idempotency hangs off this."""
    conn = _connect()
    try:
        with conn:
            row = conn.execute("SELECT external_refs FROM incidents WHERE id = ?", (incident_id,)).fetchone()
            if row is None:
                return
            refs = json.loads(row[0]) if row[0] else {}
            refs[system] = ref
            conn.execute("UPDATE incidents SET external_refs = ? WHERE id = ?",
                         (json.dumps(refs), incident_id))
    finally:
        conn.close()


def incidents_missing_ref(system: str, limit: int = 20) -> list[dict[str, Any]]:
    """Oldest-first incidents with no reference for the given system — the
    delivery outbox. JSON key check is done in Python; volumes are small."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, object_name, severity, title, markdown_report, external_refs "
            "FROM incidents ORDER BY id ASC",
        ).fetchall()
    finally:
        conn.close()
    cols = ["id", "object_name", "severity", "title", "markdown_report", "external_refs"]
    pending = []
    for row in rows:
        record = dict(zip(cols, row))
        refs = json.loads(record["external_refs"]) if record["external_refs"] else {}
        if system not in refs:
            pending.append(record)
            if len(pending) >= limit:
                break
    return pending


def get_incident(incident_id: int) -> dict[str, Any] | None:
    conn = _connect()
    try:
        cursor = conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,))
        row = cursor.fetchone()
        cols = [d[0] for d in cursor.description]
    finally:
        conn.close()
    if row is None: return None
    record = dict(zip(cols, row))
    for key in ("watcher_json", "diagnosis_json", "report_json"):
        record[key] = json.loads(record[key]) if record[key] else None
    return record
