"""Index des réponses par heure, pour lire les plus récentes d'abord.

Révision : 0004
Précédente : 0003
"""

from __future__ import annotations

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_index("ix_answer_event_time", "answer", ["event_time"])


def downgrade() -> None:
    op.drop_index("ix_answer_event_time", table_name="answer")
