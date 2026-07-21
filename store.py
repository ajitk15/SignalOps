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


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(SCHEMA)
    # Phase 2 added trigger_source after some DBs already existed —
    # ALTER TABLE ADD COLUMN for anyone with a pre-existing incidents.db.
    try:
        conn.execute("ALTER TABLE incidents ADD COLUMN trigger_source TEXT NOT NULL DEFAULT 'poll'")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
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
                total_cost_usd, trigger_source, time.time(),
            ),
        )
        return cur.lastrowid
    finally:
        conn.close()


def list_incidents(limit: int = 50) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, object_name, object_type, severity, title, total_cost_usd, trigger_source, created_at "
            "FROM incidents ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()
    finally:
        conn.close()
    cols = ["id", "object_name", "object_type", "severity", "title", "total_cost_usd", "trigger_source", "created_at"]
    return [dict(zip(cols, row)) for row in rows]


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
