"""rename adapter claude-agent-sdk -> claude-cli (data only)

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-29 16:00:00
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _rename(old: str, new: str) -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, capability_manifest FROM agent_definitions WHERE adapter_type = :t"),
        {"t": old},
    ).all()
    for agent_id, manifest in rows:
        manifest = json.loads(manifest) if isinstance(manifest, str) else dict(manifest)
        manifest["adapter_type"] = new
        conn.execute(
            sa.text(
                "UPDATE agent_definitions SET adapter_type = :t, capability_manifest = :m"
                " WHERE id = :id"
            ).bindparams(sa.bindparam("m", type_=sa.JSON)),
            {"t": new, "m": manifest, "id": agent_id},
        )
    conn.execute(
        sa.text("UPDATE agent_runs SET adapter = :new WHERE adapter = :old"),
        {"new": new, "old": old},
    )


def upgrade() -> None:
    _rename("claude-agent-sdk", "claude-cli")


def downgrade() -> None:
    _rename("claude-cli", "claude-agent-sdk")
