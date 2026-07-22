"""Phase 0b: the permission model, which is real even though the login is not.

These pin the properties that must survive swapping the dummy provider for
OIDC: roles are enforced server-side, cross-workspace access does not leak,
sessions cannot be forged, and audit never claims an identity was verified when
it was not.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _fresh_db(case: unittest.TestCase) -> None:
    """Point the engine at a throwaway file and clean it up afterwards.

    The engine must be disposed before the directory is removed: SQLAlchemy
    keeps the SQLite file open, and Windows refuses to delete an open file.
    """
    temp = tempfile.TemporaryDirectory()
    import db as db_module
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from models import Base
    engine = create_engine(f"sqlite:///{Path(temp.name) / 'test.db'}", future=True,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db_module.engine = engine
    db_module.SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    case.addCleanup(temp.cleanup)
    case.addCleanup(engine.dispose)   # runs first: cleanups pop in reverse


class RoleModelTests(unittest.TestCase):
    def test_roles_are_ordered_not_just_distinct(self):
        from models import ROLE_RANK, Role
        self.assertLess(ROLE_RANK[Role.viewer], ROLE_RANK[Role.operator])
        self.assertLess(ROLE_RANK[Role.operator], ROLE_RANK[Role.approver])
        self.assertLess(ROLE_RANK[Role.approver], ROLE_RANK[Role.admin])

    def test_principal_can_checks_rank_not_equality(self):
        from auth import Principal
        from models import Role, User, Workspace
        workspace = Workspace(id="ws1", name="w")
        admin = Principal(User(id="u", workspace_id="ws1", email="a@b", display_name="A",
                               role=Role.admin), workspace)
        viewer = Principal(User(id="v", workspace_id="ws1", email="v@b", display_name="V",
                                role=Role.viewer), workspace)
        # An admin satisfies every lower requirement, not only its own.
        self.assertTrue(admin.can(Role.viewer))
        self.assertTrue(admin.can(Role.admin))
        self.assertFalse(viewer.can(Role.operator))


class SessionIntegrityTests(unittest.TestCase):
    def test_forged_session_is_rejected(self):
        from auth import _load_session, _serializer
        self.assertIsNone(_load_session("garbage"))
        self.assertIsNone(_load_session(None))
        # A validly signed payload still round-trips.
        self.assertEqual(_load_session(_serializer.dumps({"uid": "1", "ws": "2"}))["uid"], "1")

    def test_session_signed_with_another_secret_is_rejected(self):
        from itsdangerous import URLSafeSerializer
        from auth import _load_session
        attacker = URLSafeSerializer("some-other-secret", salt="signalops-session")
        self.assertIsNone(_load_session(attacker.dumps({"uid": "1", "ws": "2"})))


class ScopingTests(unittest.TestCase):
    def test_cross_workspace_access_is_404_not_403(self):
        """403 would confirm the resource exists elsewhere; 404 does not leak that."""
        from fastapi import HTTPException
        from auth import Principal, scoped
        from models import Role, User, Workspace
        principal = Principal(
            User(id="u", workspace_id="ws1", email="a@b", display_name="A", role=Role.admin),
            Workspace(id="ws1", name="w"))
        scoped(principal, "ws1")  # same workspace: no raise
        with self.assertRaises(HTTPException) as caught:
            scoped(principal, "ws2")
        self.assertEqual(caught.exception.status_code, 404)


class DummyProviderTests(unittest.TestCase):
    def setUp(self):
        _fresh_db(self)

    def test_dummy_provider_never_claims_verified_identity(self):
        from auth import DummyAuthProvider
        from db import session_scope
        from models import Role, Workspace
        provider = DummyAuthProvider()
        self.assertFalse(provider.verifies_identity)
        with session_scope() as session:
            workspace = Workspace(name="w")
            session.add(workspace)
            session.flush()
            user = provider.login(session, workspace.id, display_name="Ajit", role=Role.admin)
            # The stub must not be able to mint a verified identity.
            self.assertFalse(user.identity_verified)

    def test_repeat_login_reuses_the_user_rather_than_duplicating(self):
        from auth import DummyAuthProvider
        from db import session_scope
        from models import Role, User, Workspace
        provider = DummyAuthProvider()
        with session_scope() as session:
            workspace = Workspace(name="w")
            session.add(workspace)
            session.flush()
            first = provider.login(session, workspace.id, display_name="Ajit", role=Role.viewer)
            second = provider.login(session, workspace.id, display_name="Ajit", role=Role.admin)
            self.assertEqual(first.id, second.id)
            # Role is re-asserted each login while there is no directory to read.
            self.assertEqual(second.role, Role.admin)
            self.assertEqual(session.query(User).count(), 1)


class AuditTests(unittest.TestCase):
    def setUp(self):
        _fresh_db(self)

    def test_audit_never_blocks_the_operation_it_records(self):
        """Losing an audit line beats refusing the action it was recording.

        Regression: swallowing the exception was not enough. A failed flush left
        the session rolled back, so the caller's commit died with
        PendingRollbackError and the audit line took the real work with it.
        """
        from db import audit, session_scope
        from models import Workspace
        with session_scope() as session:
            workspace = Workspace(name="survives")
            session.add(workspace)
            audit(session, actor="A", action="x", entity_type="t", entity_id="1",
                  detail={"bad": object()})
            workspace_id = workspace.id
        # The caller's write must have committed despite the bad audit payload.
        with session_scope() as session:
            self.assertIsNotNone(session.get(Workspace, workspace_id))

    def test_unserialisable_detail_is_coerced_rather_than_dropped(self):
        from db import audit, audit_entries, session_scope
        with session_scope() as session:
            audit(session, actor="A", action="x", entity_type="t", entity_id="1",
                  workspace_id="ws1", detail={"when": object()})
        with session_scope() as session:
            entry = audit_entries(session, workspace_id="ws1")[0]
        # The line survives with a stringified payload instead of vanishing.
        self.assertIsNotNone(entry["detail"])

    def test_entries_are_workspace_scoped(self):
        from db import audit, audit_entries, session_scope
        with session_scope() as session:
            audit(session, actor="A", action="a", entity_type="t", entity_id="1",
                  workspace_id="ws1")
            audit(session, actor="B", action="b", entity_type="t", entity_id="2",
                  workspace_id="ws2")
        with session_scope() as session:
            self.assertEqual([e["actor"] for e in audit_entries(session, workspace_id="ws1")],
                             ["A"])


if __name__ == "__main__":
    unittest.main()
