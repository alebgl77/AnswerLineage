"""Événements bruts reçus par l'API d'ingestion (US-0301, EF-EVT-01).

Révision : 0002
Précédente : 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "event_raw",
        sa.Column("event_id", sa.Text, nullable=False),
        sa.Column("run_id", sa.Uuid, nullable=False),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("producer", sa.Text, nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("event_id", name="pk_event_raw"),
    )
    op.create_index("ix_event_raw_run_id", "event_raw", ["run_id"])
    op.create_index("ix_event_raw_recorded_at", "event_raw", ["recorded_at"])


def downgrade() -> None:
    op.drop_table("event_raw")
