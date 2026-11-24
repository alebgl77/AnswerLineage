"""Listes de rappel : un message par destinataire et par incident, rangé par exposition.

``recall_list`` tient la liste de rappel d'un incident, que les workers font de son
analyse d'impact ; ``recall_entry`` une entrée par destinataire pseudonymisé, avec son
rang et son exposition ; ``recall_entry_answer`` les réponses touchées de chaque
destinataire. Des pseudonymes, des identifiants, des heures et des nombres, jamais de
texte libre.

Une analyse faite avant cette révision n'a pas de liste : la rejouer en fait une.

La descente retire les trois tables : les listes sont perdues, les incidents et leurs
analyses inchangés.

Révision : 0018
Précédente : 0017
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | None = None
depends_on: str | None = None


def _moment(name: str, *, now: bool = False) -> sa.Column[object]:
    default = sa.func.now() if now else None
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=default)


def upgrade() -> None:
    op.create_table(
        "recall_list",
        sa.Column(
            "incident_id",
            sa.Uuid,
            sa.ForeignKey("incident.id", name="fk_recall_list_incident_id_incident"),
            nullable=False,
        ),
        _moment("built_at", now=True),
        _moment("reference_at"),
        sa.Column("recipients", sa.BigInteger, nullable=False),
        sa.Column("answers", sa.BigInteger, nullable=False),
        sa.PrimaryKeyConstraint("incident_id", name="pk_recall_list"),
        sa.CheckConstraint("recipients >= 0 AND answers >= 0", name="ck_recall_list_counts"),
    )
    op.create_table(
        "recall_entry",
        sa.Column(
            "incident_id",
            sa.Uuid,
            sa.ForeignKey(
                "recall_list.incident_id", name="fk_recall_entry_incident_id_recall_list"
            ),
            nullable=False,
        ),
        sa.Column("pseudonym", sa.Text, nullable=False),
        sa.Column("rank", sa.Integer, nullable=False),
        sa.Column("exposure", sa.BigInteger, nullable=False),
        sa.Column("answers", sa.Integer, nullable=False),
        _moment("latest_answer_at"),
        sa.PrimaryKeyConstraint("incident_id", "pseudonym", name="pk_recall_entry"),
        sa.UniqueConstraint("incident_id", "rank", name="uq_recall_entry_incident_id_rank"),
        sa.CheckConstraint(
            "rank >= 1 AND exposure >= 0 AND answers >= 1", name="ck_recall_entry_counts"
        ),
    )
    op.create_table(
        "recall_entry_answer",
        sa.Column("incident_id", sa.Uuid, nullable=False),
        sa.Column("pseudonym", sa.Text, nullable=False),
        sa.Column("run_id", sa.Uuid, nullable=False),
        sa.Column("fingerprint", sa.Text, nullable=False),
        _moment("produced_at"),
        sa.PrimaryKeyConstraint(
            "incident_id", "pseudonym", "run_id", "fingerprint", name="pk_recall_entry_answer"
        ),
        sa.ForeignKeyConstraint(
            ["incident_id", "pseudonym"],
            ["recall_entry.incident_id", "recall_entry.pseudonym"],
            name="fk_recall_entry_answer_incident_id_recall_entry",
        ),
    )


def downgrade() -> None:
    op.drop_table("recall_entry_answer")
    op.drop_table("recall_entry")
    op.drop_table("recall_list")
