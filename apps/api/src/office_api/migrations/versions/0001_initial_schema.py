"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-28 12:01:53.653068
"""

from collections.abc import Sequence

import office_api.db
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_definitions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("adapter_type", sa.String(length=64), nullable=False),
        sa.Column(
            "capability_manifest",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "config",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("config_ref", sa.String(length=500), nullable=True),
        sa.Column("created_at", office_api.db.UTCDateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "principals",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("created_at", office_api.db.UTCDateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("principal_id", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", office_api.db.UTCDateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", office_api.db.UTCDateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["principal_id"], ["principals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    with op.batch_alter_table("api_tokens", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_api_tokens_principal_id"), ["principal_id"], unique=False
        )

    op.create_table(
        "idempotency_keys",
        sa.Column("principal_id", sa.String(length=64), nullable=False),
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column(
            "response_body",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_at", office_api.db.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["principal_id"], ["principals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("principal_id", "key"),
    )
    op.create_table(
        "projects",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "config",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "budget",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", office_api.db.UTCDateTime(timezone=True), nullable=False),
        sa.Column("event_seq", sa.Integer(), nullable=False),
        sa.CheckConstraint("status IN ('active', 'paused', 'archived')"),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["principals.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("principal", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("resource", sa.String(length=200), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", office_api.db.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("audit_logs", schema=None) as batch_op:
        batch_op.create_index(
            "ix_audit_logs_project_created", ["project_id", "created_at"], unique=False
        )

    op.create_table(
        "events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.String(length=64), nullable=False),
        sa.Column("conversation_id", sa.String(length=64), nullable=True),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("occurred_at", office_api.db.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "seq"),
    )
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.create_index(
            "ix_events_project_occurred", ["project_id", "occurred_at"], unique=False
        )

    op.create_table(
        "project_agents",
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("agent_id", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column(
            "permissions",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", office_api.db.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agent_definitions.id"],
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("project_id", "agent_id"),
    )
    op.create_table(
        "project_members",
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("principal_id", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("created_at", office_api.db.UTCDateTime(timezone=True), nullable=False),
        sa.CheckConstraint("role IN ('owner', 'observer')"),
        sa.ForeignKeyConstraint(["principal_id"], ["principals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("project_id", "principal_id"),
    )
    with op.batch_alter_table("project_members", schema=None) as batch_op:
        batch_op.create_index("ix_project_members_principal", ["principal_id"], unique=False)

    op.create_table(
        "tasks",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("parent_id", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "required_capabilities",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "participants",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("turn_limit", sa.Integer(), nullable=True),
        sa.Column(
            "risk_actions",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("assignee_hint", sa.String(length=64), nullable=True),
        sa.Column("assignee_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("blocked_reason", sa.String(length=200), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("deadline", office_api.db.UTCDateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("retry_at", office_api.db.UTCDateTime(timezone=True), nullable=True),
        sa.Column("result_ref", sa.String(length=500), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", office_api.db.UTCDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", office_api.db.UTCDateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('plan', 'work', 'meeting', 'dm')"),
        sa.CheckConstraint(
            "status IN ('pending', 'waiting', 'awaiting_approval', 'active', 'paused', 'blocked', 'completed', 'failed', 'cancelled')"
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "parent_id"],
            ["tasks.project_id", "tasks.id"],
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "id"),
    )
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.create_index(
            "ix_tasks_project_created", ["project_id", "created_at"], unique=False
        )
        batch_op.create_index("ix_tasks_project_status", ["project_id", "status"], unique=False)

    op.create_table(
        "agent_runs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column(
            "trigger",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("agent_id", sa.String(length=64), nullable=False),
        sa.Column("adapter", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("provider_ref", sa.String(length=200), nullable=True),
        sa.Column("progress", sa.Numeric(precision=4, scale=3, asdecimal=False), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("cost", sa.Numeric(precision=12, scale=6, asdecimal=False), nullable=True),
        sa.Column(
            "error",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("output_ref", sa.String(length=500), nullable=True),
        sa.Column("started_at", office_api.db.UTCDateTime(timezone=True), nullable=True),
        sa.Column("finished_at", office_api.db.UTCDateTime(timezone=True), nullable=True),
        sa.Column("created_at", office_api.db.UTCDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", office_api.db.UTCDateTime(timezone=True), nullable=False),
        sa.CheckConstraint("purpose IN ('task', 'orchestrator_turn', 'conversation_turn')"),
        sa.ForeignKeyConstraint(
            ["project_id", "task_id"],
            ["tasks.project_id", "tasks.id"],
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "id"),
        sa.UniqueConstraint("project_id", "idempotency_key"),
    )
    with op.batch_alter_table("agent_runs", schema=None) as batch_op:
        batch_op.create_index(
            "ix_agent_runs_project_created", ["project_id", "created_at"], unique=False
        )
        batch_op.create_index(
            "ix_agent_runs_project_status", ["project_id", "status"], unique=False
        )

    op.create_table(
        "approvals",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column("action", sa.String(length=200), nullable=False),
        sa.Column(
            "scope",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("requester_id", sa.String(length=64), nullable=False),
        sa.Column("approver_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("expires_at", office_api.db.UTCDateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", office_api.db.UTCDateTime(timezone=True), nullable=True),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", office_api.db.UTCDateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('pending', 'approved', 'rejected', 'expired', 'cancelled')"),
        sa.ForeignKeyConstraint(
            ["project_id", "task_id"],
            ["tasks.project_id", "tasks.id"],
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("approvals", schema=None) as batch_op:
        batch_op.create_index(
            "ix_approvals_project_created", ["project_id", "created_at"], unique=False
        )
        batch_op.create_index("ix_approvals_project_status", ["project_id", "status"], unique=False)

    op.create_table(
        "conversations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("visibility", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", office_api.db.UTCDateTime(timezone=True), nullable=False),
        sa.CheckConstraint("type IN ('owner_chat', 'group_meeting', 'dm', 'task_thread')"),
        sa.CheckConstraint("visibility IN ('project', 'participants')"),
        sa.ForeignKeyConstraint(
            ["project_id", "task_id"],
            ["tasks.project_id", "tasks.id"],
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "id"),
    )
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.create_index(
            "ix_conversations_project_created", ["project_id", "created_at"], unique=False
        )
        batch_op.create_index(
            "ix_conversations_project_status", ["project_id", "status"], unique=False
        )

    op.create_table(
        "interventions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("outcome", sa.String(length=500), nullable=True),
        sa.Column("requested_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", office_api.db.UTCDateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", office_api.db.UTCDateTime(timezone=True), nullable=True),
        sa.CheckConstraint("action IN ('pause', 'resume', 'cancel', 'redirect')"),
        sa.CheckConstraint("status IN ('requested', 'applied', 'rejected')"),
        sa.ForeignKeyConstraint(
            ["project_id", "task_id"],
            ["tasks.project_id", "tasks.id"],
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("interventions", schema=None) as batch_op:
        batch_op.create_index(
            "ix_interventions_project_status", ["project_id", "status"], unique=False
        )

    op.create_table(
        "orchestrator_assignments",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("agent_id", sa.String(length=64), nullable=False),
        sa.Column("active_from", office_api.db.UTCDateTime(timezone=True), nullable=False),
        sa.Column("active_to", office_api.db.UTCDateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "agent_id"],
            ["project_agents.project_id", "project_agents.agent_id"],
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "version"),
    )
    with op.batch_alter_table("orchestrator_assignments", schema=None) as batch_op:
        batch_op.create_index(
            "uq_orchestrator_active",
            ["project_id"],
            unique=True,
            postgresql_where=sa.text("active_to IS NULL"),
            sqlite_where=sa.text("active_to IS NULL"),
        )

    op.create_table(
        "task_dependencies",
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("depends_on_id", sa.String(length=64), nullable=False),
        sa.CheckConstraint("task_id <> depends_on_id"),
        sa.ForeignKeyConstraint(
            ["project_id", "depends_on_id"],
            ["tasks.project_id", "tasks.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "task_id"],
            ["tasks.project_id", "tasks.id"],
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("project_id", "task_id", "depends_on_id"),
    )
    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("uri", sa.String(length=1000), nullable=False),
        sa.Column("mime_type", sa.String(length=200), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("checksum", sa.String(length=100), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", office_api.db.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "run_id"],
            ["agent_runs.project_id", "agent_runs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "task_id"],
            ["tasks.project_id", "tasks.id"],
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("artifacts", schema=None) as batch_op:
        batch_op.create_index(
            "ix_artifacts_project_created", ["project_id", "created_at"], unique=False
        )

    op.create_table(
        "conversation_participants",
        sa.Column("conversation_id", sa.String(length=64), nullable=False),
        sa.Column("agent_id", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("conversation_id", "agent_id"),
    )
    op.create_table(
        "meetings",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("conversation_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column("agenda", sa.Text(), nullable=False),
        sa.Column("moderator_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("turn_limit", sa.Integer(), nullable=True),
        sa.Column("time_limit_s", sa.Integer(), nullable=True),
        sa.Column("token_budget", sa.Integer(), nullable=True),
        sa.Column("minutes_ref", sa.String(length=500), nullable=True),
        sa.Column("decisions_ref", sa.String(length=500), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=True),
        sa.Column("created_at", office_api.db.UTCDateTime(timezone=True), nullable=False),
        sa.Column("closed_at", office_api.db.UTCDateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('proposed', 'scheduled', 'active', 'decision_recorded', 'action_items_created', 'closed')"
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "conversation_id"],
            ["conversations.project_id", "conversations.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "task_id"],
            ["tasks.project_id", "tasks.id"],
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("meetings", schema=None) as batch_op:
        batch_op.create_index("ix_meetings_project_status", ["project_id", "status"], unique=False)

    op.create_table(
        "messages",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("conversation_id", sa.String(length=64), nullable=False),
        sa.Column("sender_id", sa.String(length=64), nullable=False),
        sa.Column("content_ref", sa.String(length=500), nullable=False),
        sa.Column("parent_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", office_api.db.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "conversation_id"],
            ["conversations.project_id", "conversations.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "parent_id"],
            ["messages.project_id", "messages.id"],
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "id"),
    )
    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.create_index(
            "ix_messages_conversation_created",
            ["project_id", "conversation_id", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.drop_index("ix_messages_conversation_created")

    op.drop_table("messages")
    with op.batch_alter_table("meetings", schema=None) as batch_op:
        batch_op.drop_index("ix_meetings_project_status")

    op.drop_table("meetings")
    op.drop_table("conversation_participants")
    with op.batch_alter_table("artifacts", schema=None) as batch_op:
        batch_op.drop_index("ix_artifacts_project_created")

    op.drop_table("artifacts")
    op.drop_table("task_dependencies")
    with op.batch_alter_table("orchestrator_assignments", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_orchestrator_active",
            postgresql_where=sa.text("active_to IS NULL"),
            sqlite_where=sa.text("active_to IS NULL"),
        )

    op.drop_table("orchestrator_assignments")
    with op.batch_alter_table("interventions", schema=None) as batch_op:
        batch_op.drop_index("ix_interventions_project_status")

    op.drop_table("interventions")
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.drop_index("ix_conversations_project_status")
        batch_op.drop_index("ix_conversations_project_created")

    op.drop_table("conversations")
    with op.batch_alter_table("approvals", schema=None) as batch_op:
        batch_op.drop_index("ix_approvals_project_status")
        batch_op.drop_index("ix_approvals_project_created")

    op.drop_table("approvals")
    with op.batch_alter_table("agent_runs", schema=None) as batch_op:
        batch_op.drop_index("ix_agent_runs_project_status")
        batch_op.drop_index("ix_agent_runs_project_created")

    op.drop_table("agent_runs")
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_index("ix_tasks_project_status")
        batch_op.drop_index("ix_tasks_project_created")

    op.drop_table("tasks")
    with op.batch_alter_table("project_members", schema=None) as batch_op:
        batch_op.drop_index("ix_project_members_principal")

    op.drop_table("project_members")
    op.drop_table("project_agents")
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_index("ix_events_project_occurred")

    op.drop_table("events")
    with op.batch_alter_table("audit_logs", schema=None) as batch_op:
        batch_op.drop_index("ix_audit_logs_project_created")

    op.drop_table("audit_logs")
    op.drop_table("projects")
    op.drop_table("idempotency_keys")
    with op.batch_alter_table("api_tokens", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_api_tokens_principal_id"))

    op.drop_table("api_tokens")
    op.drop_table("principals")
    op.drop_table("agent_definitions")
