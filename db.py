"""Database engine, session handling and audit writes.

Schema is created with metadata.create_all while the model is still in active
design. Alembic arrives when the schema stabilises — adding migration ceremony
to a schema that changes every phase buys nothing and hides churn.
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from models import AuditLog, Base, Role, User, Workspace

logger = logging.getLogger("db")

DB_PATH = Path(__file__).resolve().parent / "data" / "signalops.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(f"sqlite:///{DB_PATH}", future=True,
                       connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)

DEFAULT_WORKSPACE_NAME = "Default workspace"


@contextmanager
def session_scope() -> Session:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> str:
    """Create the schema and ensure a workspace exists. Returns its id."""
    Base.metadata.create_all(engine)
    with session_scope() as session:
        workspace = session.query(Workspace).first()
        if workspace is None:
            workspace = Workspace(name=DEFAULT_WORKSPACE_NAME)
            session.add(workspace)
            session.flush()
            logger.info("created %s (%s)", DEFAULT_WORKSPACE_NAME, workspace.id)
        return workspace.id


def _json_safe(detail: dict | None) -> dict | None:
    """Coerce a detail payload into something the JSON column can store."""
    if detail is None:
        return None
    try:
        return json.loads(json.dumps(detail, default=str))
    except Exception:
        return {"unserialisable": str(detail)[:500]}


def audit(session: Session, *, actor: str, action: str, entity_type: str,
          entity_id: str, workspace_id: str | None = None,
          actor_verified: bool = False, detail: dict | None = None) -> None:
    """Record an action.

    Never raises, and — the part that is easy to get wrong — never breaks the
    caller's transaction either. Swallowing the exception is not enough: a
    failed flush leaves the session in a rolled-back state, so the caller's
    later commit dies with PendingRollbackError and the audit line takes the
    real operation down with it. The write therefore goes inside a SAVEPOINT,
    so only the audit insert is discarded on failure.
    """
    try:
        with session.begin_nested():
            session.add(AuditLog(ts=time.time(), workspace_id=workspace_id, actor=actor,
                                 actor_verified=actor_verified, action=action,
                                 entity_type=entity_type, entity_id=entity_id,
                                 detail=_json_safe(detail)))
    except Exception:
        logger.exception("audit write failed for %s on %s %s", action, entity_type, entity_id)


def audit_entries(session: Session, workspace_id: str | None = None,
                  entity_type: str | None = None, entity_id: str | None = None,
                  limit: int = 100) -> list[dict]:
    query = session.query(AuditLog)
    if workspace_id:
        query = query.filter(AuditLog.workspace_id == workspace_id)
    if entity_type:
        query = query.filter(AuditLog.entity_type == entity_type)
        if entity_id:
            query = query.filter(AuditLog.entity_id == entity_id)
    rows = query.order_by(AuditLog.id.desc()).limit(limit).all()
    return [{"id": r.id, "ts": r.ts, "actor": r.actor, "actor_verified": r.actor_verified,
             "action": r.action, "entity_type": r.entity_type, "entity_id": r.entity_id,
             "detail": r.detail} for r in rows]
