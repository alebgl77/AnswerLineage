"""Propositions d'incident nées des tests de qualité en échec.

``quality_signal`` tient les signaux d'échec que la projection relève dans les
événements : un par run, jeu de données et colonne, sans texte libre, les classes
d'assertions d'une liste fermée. ``quality_column`` garde la dernière réussite et
le dernier échec connus de chaque colonne testée, ``quality_stats`` compte ce que
les bornes ont écarté, une ligne par étape.

``incident_proposal`` tient les propositions qu'un humain confirme ou écarte, et
``incident_proposal_column`` leurs colonnes en échec. Au plus une proposition
signalée par jeu (``uq_incident_proposal_open``). Les transitions forment une
liste fermée, tenue par une contrainte nommée (``ck_incident_proposal_transition``) :
une ligne naît signalée, seule une proposition signalée change, sans changer de
jeu, d'heure de proposition, de premier échec ni de début de fenêtre, et une
décision est définitive.

La descente retire les tables et la fonction : les propositions et les signaux
sont perdus, les incidents confirmés restent, sans le lien vers leur proposition.

Révision : 0016
Précédente : 0015
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | None = None
depends_on: str | None = None

_CLASSES = (
    "not_null",
    "unique",
    "accepted_values",
    "relationships",
    "row_count",
    "freshness",
    "custom_sql",
    "other",
)
_SEVERITIES = ("unspecified", "warn", "error")
_SIGNAL_STATES = ("pending", "attached", "muted", "refused", "stale")
_PROPOSAL_STATES = ("signaled", "confirmed", "dismissed", "expired")
_BASES = ("last_pass", "lookback")
_STAGES = ("projection", "proposals")

_TRANSITION_FUNCTION = """
CREATE FUNCTION incident_proposal_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (TG_OP = 'INSERT' AND NEW.state = 'signaled')
        OR (TG_OP = 'UPDATE' AND OLD.state = 'signaled' AND NEW.state = 'signaled' AND NEW.namespace = OLD.namespace AND NEW.name = OLD.name AND NEW.proposed_at = OLD.proposed_at AND NEW.first_failed_at = OLD.first_failed_at AND NEW.window_start = OLD.window_start)
        OR (TG_OP = 'UPDATE' AND OLD.state = 'signaled' AND NEW.state = 'confirmed' AND NEW.namespace = OLD.namespace AND NEW.name = OLD.name AND NEW.proposed_at = OLD.proposed_at AND NEW.first_failed_at = OLD.first_failed_at AND NEW.window_start = OLD.window_start)
        OR (TG_OP = 'UPDATE' AND OLD.state = 'signaled' AND NEW.state = 'dismissed' AND NEW.namespace = OLD.namespace AND NEW.name = OLD.name AND NEW.proposed_at = OLD.proposed_at AND NEW.first_failed_at = OLD.first_failed_at AND NEW.window_start = OLD.window_start)
        OR (TG_OP = 'UPDATE' AND OLD.state = 'signaled' AND NEW.state = 'expired' AND NEW.namespace = OLD.namespace AND NEW.name = OLD.name AND NEW.proposed_at = OLD.proposed_at AND NEW.first_failed_at = OLD.first_failed_at AND NEW.window_start = OLD.window_start) THEN
        RETURN NULL;
    END IF;
    RAISE EXCEPTION 'transition de proposition refusée'
        USING ERRCODE = 'check_violation',
              CONSTRAINT = 'ck_incident_proposal_transition',
              TABLE = 'incident_proposal';
END
$$
"""  # noqa: E501 — le corps de la fonction, tel que le schéma déclaré l'écrit.
_TRANSITION_CONSTRAINT = """
CREATE CONSTRAINT TRIGGER ck_incident_proposal_transition
AFTER INSERT OR UPDATE ON incident_proposal
FOR EACH ROW EXECUTE FUNCTION incident_proposal_transition()
"""


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def _classes(column: str) -> str:
    admitted = ", ".join(repr(value) for value in _CLASSES)
    return (
        f"cardinality({column}) BETWEEN 1 AND {len(_CLASSES)}"
        f" AND {column} <@ ARRAY[{admitted}]::text[]"
    )


def _at(name: str, *, nullable: bool = False, now: bool = False) -> sa.Column[object]:
    default = sa.func.now() if now else None
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable, server_default=default)


def upgrade() -> None:
    op.create_table(
        "incident_proposal",
        sa.Column("id", sa.Uuid, nullable=False, server_default=sa.func.gen_random_uuid()),
        sa.Column("state", sa.Text, nullable=False, server_default=sa.text("'signaled'")),
        sa.Column("namespace", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("whole_table", sa.Boolean, nullable=False),
        sa.Column("classes", ARRAY(sa.Text), nullable=False),
        sa.Column("severity", sa.Text, nullable=False),
        _at("first_failed_at"),
        _at("last_failed_at"),
        _at("window_start"),
        sa.Column("window_basis", sa.Text, nullable=False),
        sa.Column("emitter", sa.Text, nullable=False),
        _at("proposed_at", now=True),
        _at("signaled_at", now=True),
        _at("decided_at", nullable=True),
        sa.Column(
            "incident_id",
            sa.Uuid,
            sa.ForeignKey("incident.id", name="fk_incident_proposal_incident_id_incident"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_incident_proposal"),
        sa.UniqueConstraint("incident_id", name="uq_incident_proposal_incident_id"),
        sa.CheckConstraint(_in("state", _PROPOSAL_STATES), name="ck_incident_proposal_state"),
        sa.CheckConstraint(_in("severity", _SEVERITIES), name="ck_incident_proposal_severity"),
        sa.CheckConstraint(_in("window_basis", _BASES), name="ck_incident_proposal_window_basis"),
        sa.CheckConstraint(_classes("classes"), name="ck_incident_proposal_classes"),
        sa.CheckConstraint("first_failed_at <= last_failed_at", name="ck_incident_proposal_failed"),
        sa.CheckConstraint("window_start < first_failed_at", name="ck_incident_proposal_window"),
        sa.CheckConstraint(
            "(state = 'signaled') = (decided_at IS NULL)", name="ck_incident_proposal_decided"
        ),
        sa.CheckConstraint(
            "decided_at IS NULL OR proposed_at <= decided_at",
            name="ck_incident_proposal_decided_after",
        ),
        sa.CheckConstraint(
            "(state = 'confirmed') = (incident_id IS NOT NULL)",
            name="ck_incident_proposal_confirmed",
        ),
    )
    op.create_index(
        "ix_incident_proposal_proposed_at_id", "incident_proposal", ["proposed_at", "id"]
    )
    op.create_index(
        "ix_incident_proposal_namespace_name_decided_at",
        "incident_proposal",
        ["namespace", "name", "decided_at"],
    )
    op.create_index(
        "uq_incident_proposal_open",
        "incident_proposal",
        ["namespace", "name"],
        unique=True,
        postgresql_where=sa.text("state = 'signaled'"),
    )
    op.execute(_TRANSITION_FUNCTION)
    op.execute(_TRANSITION_CONSTRAINT)

    op.create_table(
        "incident_proposal_column",
        sa.Column(
            "proposal_id",
            sa.Uuid,
            sa.ForeignKey(
                "incident_proposal.id",
                name="fk_incident_proposal_column_proposal_id_incident_proposal",
            ),
            nullable=False,
        ),
        sa.Column("column_name", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("proposal_id", "column_name", name="pk_incident_proposal_column"),
        sa.CheckConstraint("column_name <> ''", name="ck_incident_proposal_column_named"),
    )

    op.create_table(
        "quality_signal",
        sa.Column("run_id", sa.Uuid, nullable=False),
        sa.Column("namespace", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("column_name", sa.Text, nullable=False),
        sa.Column("classes", ARRAY(sa.Text), nullable=False),
        sa.Column("severity", sa.Text, nullable=False),
        sa.Column("emitter", sa.Text, nullable=False),
        _at("event_time"),
        _at("recorded_at"),
        sa.Column("state", sa.Text, nullable=False, server_default=sa.text("'pending'")),
        sa.Column(
            "proposal_id",
            sa.Uuid,
            sa.ForeignKey(
                "incident_proposal.id", name="fk_quality_signal_proposal_id_incident_proposal"
            ),
        ),
        sa.PrimaryKeyConstraint(
            "run_id", "namespace", "name", "column_name", name="pk_quality_signal"
        ),
        sa.CheckConstraint(_classes("classes"), name="ck_quality_signal_classes"),
        sa.CheckConstraint(_in("severity", _SEVERITIES), name="ck_quality_signal_severity"),
        sa.CheckConstraint(_in("state", _SIGNAL_STATES), name="ck_quality_signal_state"),
        sa.CheckConstraint(
            "(state IN ('attached', 'muted')) = (proposal_id IS NOT NULL)",
            name="ck_quality_signal_proposal",
        ),
    )
    op.create_index("ix_quality_signal_recorded_at", "quality_signal", ["recorded_at"])
    op.create_index(
        "ix_quality_signal_pending",
        "quality_signal",
        ["event_time", "run_id"],
        postgresql_where=sa.text("state = 'pending'"),
    )
    op.create_index(
        "ix_quality_signal_proposal_id",
        "quality_signal",
        ["proposal_id", "run_id"],
        postgresql_where=sa.text("proposal_id IS NOT NULL"),
    )

    op.create_table(
        "quality_column",
        sa.Column("namespace", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("column_name", sa.Text, nullable=False),
        _at("last_passed_at", nullable=True),
        _at("last_failed_at", nullable=True),
        sa.PrimaryKeyConstraint("namespace", "name", "column_name", name="pk_quality_column"),
        sa.CheckConstraint(
            "last_passed_at IS NOT NULL OR last_failed_at IS NOT NULL",
            name="ck_quality_column_known",
        ),
    )

    op.create_table(
        "quality_stats",
        sa.Column("stage", sa.Text, nullable=False),
        *(
            sa.Column(name, sa.BigInteger, nullable=False, server_default="0")
            for name in ("dropped", "refused", "stale", "invalid_columns", "assertions_truncated")
        ),
        _at("refused_since", nullable=True),
        sa.PrimaryKeyConstraint("stage", name="pk_quality_stats"),
        sa.CheckConstraint(_in("stage", _STAGES), name="ck_quality_stats_stage"),
        sa.CheckConstraint(
            "dropped >= 0 AND refused >= 0 AND stale >= 0 AND invalid_columns >= 0"
            " AND assertions_truncated >= 0",
            name="ck_quality_stats_counts",
        ),
    )


def downgrade() -> None:
    op.drop_table("quality_stats")
    op.drop_table("quality_column")
    op.drop_table("quality_signal")
    op.drop_table("incident_proposal_column")
    op.drop_table("incident_proposal")
    op.execute("DROP FUNCTION incident_proposal_transition()")
