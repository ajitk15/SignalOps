"""SQLAlchemy models for the SignalOps workflow platform.

Everything is workspace-scoped. Queries must filter by workspace_id rather than
relying on the caller to behave — cross-workspace reads are the kind of bug that
looks harmless in a single-tenant demo and is a breach the moment it is not.
"""
from __future__ import annotations

import enum
import time
import uuid

from sqlalchemy import (JSON, Boolean, Column, Enum, Float, ForeignKey, Index,
                        Integer, String, Text)
from sqlalchemy.orm import DeclarativeBase, relationship


def _uuid() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class Role(str, enum.Enum):
    """Ordered least to most privileged; compare with ROLE_RANK, not equality."""
    viewer = "viewer"
    operator = "operator"
    approver = "approver"
    admin = "admin"


ROLE_RANK = {Role.viewer: 0, Role.operator: 1, Role.approver: 2, Role.admin: 3}


class RunStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    awaiting_approval = "awaiting_approval"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


class CustomAgentStatus(str, enum.Enum):
    """Lifecycle of a user-authored agent.

    An admin's own agent goes straight to `approved`. Anyone else's lands in
    `pending_review` and cannot run until an admin approves it — the review is
    where a human confirms the prompt and the granted tools are sane before the
    agent is ever executed.
    """
    pending_review = "pending_review"
    approved = "approved"
    rejected = "rejected"


class ApprovalStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    expired = "expired"


class Workspace(Base):
    __tablename__ = "workspace"
    id = Column(String, primary_key=True, default=_uuid)
    name = Column(String, nullable=False)
    created_at = Column(Float, nullable=False, default=time.time)
    # Hard stop for everything in this workspace; checked before any run starts.
    killswitch = Column(Boolean, nullable=False, default=False)
    # Cost ceiling across all runs; None means no workspace-level cap.
    budget_usd = Column(Float, nullable=True)

    users = relationship("User", back_populates="workspace")


class User(Base):
    __tablename__ = "user"
    id = Column(String, primary_key=True, default=_uuid)
    workspace_id = Column(String, ForeignKey("workspace.id"), nullable=False)
    email = Column(String, nullable=False)
    display_name = Column(String, nullable=False)
    role = Column(Enum(Role), nullable=False, default=Role.viewer)
    # Argon2 hash. Nullable because accounts created before passwords existed,
    # and because an invited user has none until they set one.
    password_hash = Column(String, nullable=True)
    # Deactivating rather than deleting: a removed user would orphan the audit
    # entries and approvals that name them, which is the opposite of what an
    # audit trail is for.
    active = Column(Boolean, nullable=False, default=True)
    must_change_password = Column(Boolean, nullable=False, default=False)
    failed_logins = Column(Integer, nullable=False, default=0)
    locked_until = Column(Float, nullable=True)
    created_at = Column(Float, nullable=False, default=time.time)
    last_login_at = Column(Float, nullable=True)
    # True once a password has actually been checked. Carried into audit
    # entries so a claimed identity is never displayed as a verified one.
    identity_verified = Column(Boolean, nullable=False, default=False)

    workspace = relationship("Workspace", back_populates="users")


Index("ix_user_email", User.workspace_id, User.email, unique=True)


class Connection(Base):
    """One configured external system, such as a ticketing or telemetry source.

    Several of a kind are expected: a dev instance and a production one are
    different connections, and a workflow names the one it uses. That is why
    credentials live here rather than in the environment, where a single
    SN_READ_USER could only ever describe one instance.

    `secrets` holds encrypted values (see crypto.py) and is never returned by
    any endpoint — the API reports which secrets are set, never what they are.
    `config` holds everything non-secret: instance URL, auth type, the queue to
    poll.
    """
    __tablename__ = "connection"
    id = Column(String, primary_key=True, default=_uuid)
    workspace_id = Column(String, ForeignKey("workspace.id"), nullable=False)
    kind = Column(String, nullable=False)  # servicenow | jira | splunk | datadog | dynatrace
    name = Column(String, nullable=False)
    config = Column(JSON, nullable=False, default=dict)
    secrets = Column(JSON, nullable=True)          # encrypted at rest
    enabled = Column(Boolean, nullable=False, default=True)
    last_tested_at = Column(Float, nullable=True)
    last_test_ok = Column(Boolean, nullable=True)
    last_test_detail = Column(Text, nullable=True)
    created_at = Column(Float, nullable=False, default=time.time)
    created_by = Column(String, ForeignKey("user.id"), nullable=True)


Index("ix_connection_name", Connection.workspace_id, Connection.kind,
      Connection.name, unique=True)


class Workflow(Base):
    """A template instantiated with config — not a free-form graph."""
    __tablename__ = "workflow"
    id = Column(String, primary_key=True, default=_uuid)
    workspace_id = Column(String, ForeignKey("workspace.id"), nullable=False)
    template = Column(String, nullable=False)      # incident_remediation | ticket_to_pr
    name = Column(String, nullable=False)
    config = Column(JSON, nullable=False, default=dict)
    enabled = Column(Boolean, nullable=False, default=False)
    # Onboarding will not offer Enable until a dry-run has succeeded.
    dry_run_passed_at = Column(Float, nullable=True)
    created_by = Column(String, ForeignKey("user.id"), nullable=True)
    created_at = Column(Float, nullable=False, default=time.time)


class Run(Base):
    __tablename__ = "run"
    id = Column(String, primary_key=True, default=_uuid)
    workspace_id = Column(String, ForeignKey("workspace.id"), nullable=False)
    workflow_id = Column(String, ForeignKey("workflow.id"), nullable=False)
    # External ticket this run is for; unique per workflow so a poller that sees
    # the same ticket twice cannot start a second run.
    trigger_ref = Column(String, nullable=True)
    status = Column(Enum(RunStatus), nullable=False, default=RunStatus.pending)
    dry_run = Column(Boolean, nullable=False, default=True)
    started_at = Column(Float, nullable=False, default=time.time)
    finished_at = Column(Float, nullable=True)
    cost_usd = Column(Float, nullable=False, default=0.0)
    error = Column(Text, nullable=True)

    steps = relationship("RunStep", back_populates="run", order_by="RunStep.started_at")


Index("ix_run_workflow_trigger", Run.workflow_id, Run.trigger_ref, unique=True,
      sqlite_where=Run.trigger_ref.isnot(None))


class RunStep(Base):
    __tablename__ = "run_step"
    id = Column(String, primary_key=True, default=_uuid)
    run_id = Column(String, ForeignKey("run.id"), nullable=False)
    node = Column(String, nullable=False)
    agent_id = Column(String, nullable=True)       # set when the node is agentic
    status = Column(String, nullable=False, default="running")
    started_at = Column(Float, nullable=False, default=time.time)
    finished_at = Column(Float, nullable=True)
    output = Column(JSON, nullable=True)
    input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)
    cost_usd = Column(Float, nullable=False, default=0.0)
    error = Column(Text, nullable=True)

    run = relationship("Run", back_populates="steps")


class Approval(Base):
    """A human gate. payload_hash pins exactly what was approved, so approving a
    plan cannot silently authorise a plan that changed afterwards."""
    __tablename__ = "approval"
    id = Column(String, primary_key=True, default=_uuid)
    run_id = Column(String, ForeignKey("run.id"), nullable=False)
    node = Column(String, nullable=False)
    summary = Column(Text, nullable=False)
    payload = Column(JSON, nullable=False, default=dict)
    payload_hash = Column(String, nullable=False)
    status = Column(Enum(ApprovalStatus), nullable=False, default=ApprovalStatus.pending)
    requested_at = Column(Float, nullable=False, default=time.time)
    decided_at = Column(Float, nullable=True)
    decided_by = Column(String, ForeignKey("user.id"), nullable=True)
    note = Column(Text, nullable=True)


class AgentConfig(Base):
    """Per-workspace customisation of a catalogue agent.

    Only the fields here are customisable. The tool allowlist and risk tier are
    deliberately absent: they live in code, so customisation cannot widen what
    an agent is able to do.
    """
    __tablename__ = "agent_config"
    id = Column(String, primary_key=True, default=_uuid)
    workspace_id = Column(String, ForeignKey("workspace.id"), nullable=False)
    agent_id = Column(String, nullable=False)
    model = Column(String, nullable=True)
    # Full replacement for the agent's task prompt. The safety preamble is
    # always prepended in code and is not part of this, so rewriting the task
    # cannot remove the injection defences.
    custom_prompt = Column(Text, nullable=True)
    extra_guidance = Column(Text, nullable=True)
    confidence_threshold = Column(Float, nullable=True)
    requires_approval = Column(Boolean, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    updated_at = Column(Float, nullable=False, default=time.time)
    updated_by = Column(String, ForeignKey("user.id"), nullable=True)


Index("ix_agent_config_ws_agent", AgentConfig.workspace_id, AgentConfig.agent_id, unique=True)


class CustomAgent(Base):
    """A user-authored agent, reviewed before it can run.

    Unlike the catalogue agents (declared in code), these are created from the
    UI — but the safety envelope is not thereby handed to the UI. `tools` may
    only contain Claude Agent SDK tools the platform is willing to grant, and
    `tier` is *derived* from those tools on the server, never accepted from the
    client. Tools the platform never grants — a shell, network fetch — cannot be
    selected at all, so even a fully custom agent cannot escalate past read and
    write-code.
    """
    __tablename__ = "custom_agent"
    id = Column(String, primary_key=True, default=_uuid)
    workspace_id = Column(String, ForeignKey("workspace.id"), nullable=False)
    name = Column(String, nullable=False)
    purpose = Column(Text, nullable=False)
    explanation = Column(Text, nullable=False, default="")
    workflow = Column(String, nullable=False, default="both")
    model = Column(String, nullable=False)
    # The task prompt. The safety preamble is prepended in code and is not part
    # of this, so a rewritten task still runs under the injection defences.
    system_prompt = Column(Text, nullable=False)
    tools = Column(JSON, nullable=False, default=list)     # Agent SDK tool names
    tier = Column(String, nullable=False, default="read")  # derived, never from the client
    output_schema = Column(JSON, nullable=True)
    status = Column(Enum(CustomAgentStatus), nullable=False,
                    default=CustomAgentStatus.pending_review)
    enabled = Column(Boolean, nullable=False, default=True)
    created_by = Column(String, ForeignKey("user.id"), nullable=True)
    created_at = Column(Float, nullable=False, default=time.time)
    reviewed_by = Column(String, ForeignKey("user.id"), nullable=True)
    reviewed_at = Column(Float, nullable=True)
    review_note = Column(Text, nullable=True)


Index("ix_custom_agent_ws_name", CustomAgent.workspace_id, CustomAgent.name, unique=True)


class AuditLog(Base):
    """Carried over from v1 unchanged in spirit: append-only, and honest about
    whether the actor was actually verified."""
    __tablename__ = "audit_log"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(Float, nullable=False, default=time.time)
    workspace_id = Column(String, nullable=True)
    actor = Column(String, nullable=False)
    actor_verified = Column(Boolean, nullable=False, default=False)
    action = Column(String, nullable=False)
    entity_type = Column(String, nullable=False)
    entity_id = Column(String, nullable=False)
    detail = Column(JSON, nullable=True)


Index("ix_audit_entity", AuditLog.entity_type, AuditLog.entity_id)
