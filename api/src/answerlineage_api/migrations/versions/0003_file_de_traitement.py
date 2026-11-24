"""File de traitement des événements reçus.

Révision : 0003
Précédente : 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "ingest_queue",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), nullable=False),
        sa.Column(
            "event_id",
            sa.Text,
            sa.ForeignKey("event_raw.event_id", name="fk_ingest_queue_event_id_event_raw"),
            nullable=False,
        ),
        sa.Column(
            "enqueued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text),
        sa.PrimaryKeyConstraint("id", name="pk_ingest_queue"),
        sa.UniqueConstraint("event_id", name="uq_ingest_queue_event_id"),
    )
    op.create_index("ix_ingest_queue_available_at_id", "ingest_queue", ["available_at", "id"])


def downgrade() -> None:
    op.drop_table("ingest_queue")
