"""Compteur des événements reçus en double, par émetteur et par jour.

Révision : 0005
Précédente : 0004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "ingest_duplicate",
        sa.Column("emitter", sa.Text, nullable=False),
        sa.Column("day", sa.Date, nullable=False),
        sa.Column("duplicates", sa.BigInteger, nullable=False),
        sa.PrimaryKeyConstraint("emitter", "day", name="pk_ingest_duplicate"),
        sa.CheckConstraint("duplicates > 0", name="ck_ingest_duplicate_positive"),
    )
    op.create_index(
        "ix_ingest_duplicate_day_emitter", "ingest_duplicate", [sa.text("day DESC"), "emitter"]
    )


def downgrade() -> None:
    op.drop_table("ingest_duplicate")
