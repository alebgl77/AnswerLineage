"""File des webhooks : les changements d'état des incidents, à annoncer au récepteur.

``webhook_queue`` tient chaque message à envoyer, figé à l'heure du changement
d'état : son identifiant de livraison, la sorte d'événement, l'incident, son
état, sa gravité, l'état de son analyse, son code d'échec et ses décomptes, avec
l'heure de sa mise en file, ses envois en échec et la classe du dernier. Des
identifiants, des codes de listes fermées, des heures et des nombres : aucun
texte libre, aucun pseudonyme. ``webhook_stats`` dit, en une ligne, jusqu'à quand
un webhook est en service, et compte les messages remis, refusés par le
récepteur et abandonnés.

La descente retire les deux tables : ce qui attendait d'être annoncé ne l'est
pas, les incidents ne changent pas.

Révision : 0014
Précédente : 0013
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | None = None
depends_on: str | None = None

# Chaque événement et l'état de l'analyse qu'il annonce.
_EVENTS = (
    ("incident.declared", "pending"),
    ("incident.analysed", "done"),
    ("incident.analysis_failed", "failed"),
)
_SEVERITIES = ("low", "medium", "high", "critical")
_INCIDENT_STATES = ("qualified",)
_ANALYSIS_ERRORS = ("query_timeout", "analysis_error")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def upgrade() -> None:
    op.create_table(
        "webhook_queue",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), nullable=False),
        sa.Column("delivery_id", sa.Uuid, nullable=False, server_default=sa.func.gen_random_uuid()),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column(
            "incident_id",
            sa.Uuid,
            sa.ForeignKey("incident.id", name="fk_webhook_queue_incident_id_incident"),
            nullable=False,
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("incident_state", sa.Text, nullable=False),
        sa.Column("severity", sa.Text, nullable=False),
        sa.Column("analysis_state", sa.Text, nullable=False),
        sa.Column("analysis_error", sa.Text),
        sa.Column("answers", sa.BigInteger),
        sa.Column("recipients", sa.BigInteger),
        sa.Column("lower_bound", sa.Boolean),
        sa.Column(
            "enqueued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text),
        sa.PrimaryKeyConstraint("id", name="pk_webhook_queue"),
        sa.UniqueConstraint("delivery_id", name="uq_webhook_queue_delivery_id"),
        sa.CheckConstraint(
            _in("event_type", tuple(event for event, _ in _EVENTS)),
            name="ck_webhook_queue_event_type",
        ),
        sa.CheckConstraint(
            " OR ".join(
                f"(event_type = '{event}' AND analysis_state = '{state}')"
                for event, state in _EVENTS
            ),
            name="ck_webhook_queue_analysis_state",
        ),
        sa.CheckConstraint(
            _in("incident_state", _INCIDENT_STATES), name="ck_webhook_queue_incident_state"
        ),
        sa.CheckConstraint(_in("severity", _SEVERITIES), name="ck_webhook_queue_severity"),
        sa.CheckConstraint(
            f"analysis_error IS NULL OR {_in('analysis_error', _ANALYSIS_ERRORS)}",
            name="ck_webhook_queue_analysis_error",
        ),
        sa.CheckConstraint(
            "(analysis_state = 'failed') = (analysis_error IS NOT NULL)",
            name="ck_webhook_queue_failure",
        ),
        sa.CheckConstraint(
            "(analysis_state = 'done') = (answers IS NOT NULL AND recipients IS NOT NULL"
            " AND lower_bound IS NOT NULL)",
            name="ck_webhook_queue_counts_given",
        ),
        sa.CheckConstraint(
            "answers >= 0 AND recipients >= 0 AND attempts >= 0", name="ck_webhook_queue_counts"
        ),
        sa.CheckConstraint(
            "(attempts = 0) = (last_error IS NULL)", name="ck_webhook_queue_last_error"
        ),
    )
    op.create_index("ix_webhook_queue_enqueued_at", "webhook_queue", ["enqueued_at"])
    op.create_index(
        "ix_webhook_queue_retrying",
        "webhook_queue",
        ["id"],
        postgresql_where=sa.text("attempts > 0"),
    )

    op.create_table(
        "webhook_stats",
        sa.Column("id", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("in_service_until", sa.DateTime(timezone=True)),
        sa.Column("sent", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("rejected", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("dropped", sa.BigInteger, nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id", name="pk_webhook_stats"),
        sa.CheckConstraint("id", name="ck_webhook_stats_single_row"),
        sa.CheckConstraint(
            "sent >= 0 AND rejected >= 0 AND dropped >= 0", name="ck_webhook_stats_counts"
        ),
    )


def downgrade() -> None:
    op.drop_table("webhook_stats")
    op.drop_table("webhook_queue")
