"""File de réémission vers un consommateur OpenLineage externe.

``reemission_queue`` désigne chaque événement projeté qui attend d'être envoyé
à la destination des workers, par son identifiant et son mois de réception,
comme la file de traitement, avec l'heure de sa mise en file, ses envois en
échec et la classe du dernier. ``reemission_stats`` compte, en une ligne, les
événements remis, refusés par la destination et abandonnés.

La file de traitement dit désormais si un événement doit partir une fois
projeté (``reemit``) : vrai pour tout événement qu'elle tient à la montée, qui
n'est jamais entré au graphe ; la reconstruction du graphe le met à faux pour
les événements qu'elle remet en file.

La descente retire les deux tables et la colonne : ce qui attendait d'être
réémis ne l'est pas, le graphe et la file de traitement ne changent pas.

Révision : 0013
Précédente : 0012
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "ingest_queue",
        sa.Column("reemit", sa.Boolean, nullable=False, server_default=sa.text("true")),
    )

    op.create_table(
        "reemission_queue",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), nullable=False),
        sa.Column("event_id", sa.Text, nullable=False),
        sa.Column("recorded_month", sa.Date, nullable=False),
        sa.Column(
            "enqueued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text),
        sa.PrimaryKeyConstraint("id", name="pk_reemission_queue"),
        sa.UniqueConstraint("event_id", name="uq_reemission_queue_event_id"),
        sa.CheckConstraint("attempts >= 0", name="ck_reemission_queue_attempts"),
        sa.CheckConstraint(
            "(attempts = 0) = (last_error IS NULL)", name="ck_reemission_queue_last_error"
        ),
    )
    op.create_index("ix_reemission_queue_enqueued_at", "reemission_queue", ["enqueued_at"])
    op.create_index(
        "ix_reemission_queue_retrying",
        "reemission_queue",
        ["id"],
        postgresql_where=sa.text("attempts > 0"),
    )

    op.create_table(
        "reemission_stats",
        sa.Column("id", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("sent", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("rejected", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("dropped", sa.BigInteger, nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id", name="pk_reemission_stats"),
        sa.CheckConstraint("id", name="ck_reemission_stats_single_row"),
        sa.CheckConstraint(
            "sent >= 0 AND rejected >= 0 AND dropped >= 0", name="ck_reemission_stats_counts"
        ),
    )


def downgrade() -> None:
    op.drop_table("reemission_stats")
    op.drop_table("reemission_queue")
    op.drop_column("ingest_queue", "reemit")
