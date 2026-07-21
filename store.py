"""SQLite persistence for incidents (all 3 pipeline stages' outputs)."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent / "data" / "incidents.db"

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
"""


# Ordered migrations for pre-existing databases. Each runs once per process;
# "duplicate column" errors mean the DB is already current and are ignored.
MIGRATIONS = [
    "ALTER TABLE incidents ADD COLUMN trigger_source TEXT NOT NULL DEFAULT 'poll'",
    "ALTER TABLE incidents ADD COLUMN external_refs TEXT",
]
# Keyed by DB path (not a bool) so tests pointing DB_PATH at a fresh temp file
# get their schema created too.
_migrated: set[str] = set()


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    if str(DB_PATH) not in _migrated:
        conn.execute(SCHEMA)
        for statement in MIGRATIONS:
            try:
                conn.execute(statement)
            except sqlite3.OperationalError:
                pass  # column already exists
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
) -> int:
    conn = _connect()
    try:
      with conn:
        cur = conn.execute(
            """INSERT INTO incidents
               (object_name, object_type, severity, title, markdown_report,
                watcher_json, diagnosis_json, report_json, total_cost_usd,
                trigger_source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                object_name, object_type, severity, title, markdown_report,
                json.dumps(watcher_json), json.dumps(diagnosis_json), json.dumps(report_json),
                total_cost_usd, trigger_source,
                # Callers that also announce the incident pass their own value so
                # the event and the stored row report the same creation time.
                time.time() if created_at is None else created_at,
            ),
        )
        return cur.lastrowid
    finally:
        conn.close()


def list_incidents(limit: int = 50) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, object_name, object_type, severity, title, total_cost_usd, trigger_source, created_at, external_refs "
            "FROM incidents ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()
    finally:
        conn.close()
    cols = ["id", "object_name", "object_type", "severity", "title", "total_cost_usd", "trigger_source", "created_at", "external_refs"]
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
