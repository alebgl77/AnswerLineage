"""Trace des reconstructions du graphe depuis les événements bruts.

``graph_rebuild`` tient une ligne par reconstruction : son début, sa fin, la
dernière ligne de la file qu'elle doit voir projetée, les événements remis à la
projection et ceux laissés au rebut, la période de réception des événements
conservés et les lignes du graphe qu'elle ne peut pas rendre. Des heures et des
décomptes, aucun contenu.

La descente retire la table, et la trace avec elle : le graphe et la file ne
changent pas.

Révision : 0012
Précédente : 0011
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | None = None
depends_on: str | None = None


def _moment(name: str, *, nullable: bool = True) -> sa.Column[object]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "graph_rebuild",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        _moment("finished_at"),
        sa.Column("queue_boundary", sa.BigInteger, nullable=False),
        sa.Column("replayed", sa.BigInteger, nullable=False),
        sa.Column("dead_letters", sa.BigInteger, nullable=False),
        _moment("first_received"),
        _moment("last_received"),
        sa.Column("unrecoverable", sa.BigInteger, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_graph_rebuild"),
        sa.CheckConstraint(
            "queue_boundary >= 0 AND replayed >= 0 AND dead_letters >= 0 AND unrecoverable >= 0",
            name="ck_graph_rebuild_counts",
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR started_at <= finished_at", name="ck_graph_rebuild_finished"
        ),
        sa.CheckConstraint(
            "(first_received IS NULL) = (last_received IS NULL)"
            " AND (first_received IS NULL OR first_received <= last_received)",
            name="ck_graph_rebuild_received",
        ),
    )


def downgrade() -> None:
    op.drop_table("graph_rebuild")
