"""Incidents de données : déclaration, périmètre, demande d'analyse et résultat.

Un incident nomme un ou plusieurs jeux de données faux pendant une fenêtre de
validité, avec une gravité d'une liste fermée. Sa déclaration écrit aussi une
demande d'analyse, que les workers réclament ; le résultat garde les réponses
touchées, par leur run et leur empreinte, et les pseudonymes de leurs
destinataires. Aucune de ces tables ne porte de texte libre.

Révision : 0010
Précédente : 0009
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | None = None
depends_on: str | None = None

_SEVERITIES = ("low", "medium", "high", "critical")
_INCIDENT_STATES = ("qualified",)
_ANALYSIS_STATES = ("pending", "done", "failed")
_ANALYSIS_ERRORS = ("query_timeout", "analysis_error")
_WINDOW_DAYS = 31


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def _moment(name: str, *, nullable: bool = True, now: bool = False) -> sa.Column[object]:
    default = sa.func.now() if now else None
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable, server_default=default)


def upgrade() -> None:
    op.create_table(
        "incident",
        sa.Column("id", sa.Uuid, nullable=False),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("severity", sa.Text, nullable=False),
        _moment("window_start", nullable=False),
        _moment("window_end", nullable=False),
        _moment("declared_at", nullable=False, now=True),
        sa.PrimaryKeyConstraint("id", name="pk_incident"),
        sa.CheckConstraint(_in("state", _INCIDENT_STATES), name="ck_incident_state"),
        sa.CheckConstraint(_in("severity", _SEVERITIES), name="ck_incident_severity"),
        sa.CheckConstraint("window_start < window_end", name="ck_incident_window"),
        sa.CheckConstraint(
            f"window_end - window_start <= interval '{_WINDOW_DAYS} days'",
            name="ck_incident_window_length",
        ),
    )
    op.create_index("ix_incident_declared_at_id", "incident", ["declared_at", "id"])

    op.create_table(
        "incident_dataset",
        sa.Column(
            "incident_id",
            sa.Uuid,
            sa.ForeignKey("incident.id", name="fk_incident_dataset_incident_id_incident"),
            nullable=False,
        ),
        sa.Column("namespace", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("incident_id", "namespace", "name", name="pk_incident_dataset"),
    )

    op.create_table(
        "incident_analysis",
        sa.Column(
            "incident_id",
            sa.Uuid,
            sa.ForeignKey("incident.id", name="fk_incident_analysis_incident_id_incident"),
            nullable=False,
        ),
        sa.Column("state", sa.Text, nullable=False, server_default=sa.text("'pending'")),
        _moment("requested_at", nullable=False, now=True),
        _moment("available_at", nullable=False, now=True),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error", sa.Text),
        _moment("known_at"),
        _moment("finished_at"),
        sa.Column("answers", sa.BigInteger),
        sa.Column("recipients", sa.BigInteger),
        sa.Column("truncated", sa.Boolean),
        sa.Column("coverage", JSONB),
        sa.Column("unknown_datasets", JSONB),
        sa.PrimaryKeyConstraint("incident_id", name="pk_incident_analysis"),
        sa.CheckConstraint(_in("state", _ANALYSIS_STATES), name="ck_incident_analysis_state"),
        sa.CheckConstraint(
            f"error IS NULL OR {_in('error', _ANALYSIS_ERRORS)}", name="ck_incident_analysis_error"
        ),
        sa.CheckConstraint(
            "state <> 'failed' OR error IS NOT NULL", name="ck_incident_analysis_failure"
        ),
        sa.CheckConstraint(
            "(state = 'pending') = (finished_at IS NULL)", name="ck_incident_analysis_finished"
        ),
        sa.CheckConstraint(
            "(state = 'done') = (known_at IS NOT NULL AND answers IS NOT NULL"
            " AND recipients IS NOT NULL AND truncated IS NOT NULL AND coverage IS NOT NULL"
            " AND unknown_datasets IS NOT NULL)",
            name="ck_incident_analysis_result",
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND answers >= 0 AND recipients >= 0",
            name="ck_incident_analysis_counts",
        ),
    )
    op.create_index(
        "ix_incident_analysis_available_at",
        "incident_analysis",
        ["available_at"],
        postgresql_where=sa.text("state = 'pending'"),
    )

    op.create_table(
        "incident_answer",
        sa.Column(
            "incident_id",
            sa.Uuid,
            sa.ForeignKey("incident.id", name="fk_incident_answer_incident_id_incident"),
            nullable=False,
        ),
        sa.Column("run_id", sa.Uuid, nullable=False),
        sa.Column("fingerprint", sa.Text, nullable=False),
        _moment("produced_at", nullable=False),
        sa.PrimaryKeyConstraint("incident_id", "run_id", "fingerprint", name="pk_incident_answer"),
    )
    op.create_index(
        "ix_incident_answer_incident_id_produced_at_run_id",
        "incident_answer",
        ["incident_id", "produced_at", "run_id"],
    )

    op.create_table(
        "incident_recipient",
        sa.Column("incident_id", sa.Uuid, nullable=False),
        sa.Column("run_id", sa.Uuid, nullable=False),
        sa.Column("fingerprint", sa.Text, nullable=False),
        sa.Column("pseudonym", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint(
            "incident_id", "run_id", "fingerprint", "pseudonym", name="pk_incident_recipient"
        ),
        sa.ForeignKeyConstraint(
            ["incident_id", "run_id", "fingerprint"],
            [
                "incident_answer.incident_id",
                "incident_answer.run_id",
                "incident_answer.fingerprint",
            ],
            name="fk_incident_recipient_incident_id_incident_answer",
        ),
    )


def downgrade() -> None:
    op.drop_table("incident_recipient")
    op.drop_table("incident_answer")
    op.drop_table("incident_analysis")
    op.drop_table("incident_dataset")
    op.drop_table("incident")
