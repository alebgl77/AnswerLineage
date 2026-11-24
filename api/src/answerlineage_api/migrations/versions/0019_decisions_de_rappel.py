"""Décisions de rappel : la validation humaine, son journal, et aucun envoi sans elle.

``recall_decision`` journalise, en ajout seul, la décision d'un responsable d'agent sur la
liste de rappel d'un incident (validée, modifiée par le choix d'un gabarit, refusée avec un
motif, de listes fermées), son auteur OIDC et l'heure de la base ; et la remontée d'une
liste restée sans décision, posée par les workers. Une fonction et ses déclencheurs posent
l'heure à l'insertion et refusent toute modification, suppression ou troncature
(``ck_recall_decision_append_only``) ; ces droits sont aussi retirés au propriétaire.

``recall_dispatch`` tient les messages de rappel mis à l'envoi. Une contrainte de
déclencheur refuse toute écriture sans une liste validée ou modifiée
(``ck_recall_dispatch_validated``), quelle que soit la connexion. Une autre fonction fige
une liste décidée, ses entrées et leurs réponses (``ck_recall_list_frozen``).

La descente retire les deux tables, leurs fonctions et leurs déclencheurs : les décisions
journalisées sont perdues, les listes inchangées.

Révision : 0019
Précédente : 0018
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | None = None
depends_on: str | None = None

_DECISIONS = ("validated", "modified", "refused", "escalated")
_APPROVALS = ("validated", "modified")
_TEMPLATES = ("standard", "information", "decision_review")
_REFUSALS = ("not_affected", "already_informed", "no_impact", "duplicate_incident")
_FROZEN = ("recall_list", "recall_entry", "recall_entry_answer")

_APPEND_ONLY_FUNCTION = """
CREATE FUNCTION recall_decision_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.decided_at := now();
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'le journal des décisions de rappel ne se modifie pas'
        USING ERRCODE = 'check_violation',
              CONSTRAINT = 'ck_recall_decision_append_only',
              TABLE = 'recall_decision';
END
$$
"""
_APPEND_ONLY = (
    """
CREATE TRIGGER recall_decision_stamped
BEFORE INSERT ON recall_decision
FOR EACH ROW EXECUTE FUNCTION recall_decision_append_only()
""",
    """
CREATE TRIGGER recall_decision_append_only
BEFORE UPDATE OR DELETE ON recall_decision
FOR EACH ROW EXECUTE FUNCTION recall_decision_append_only()
""",
    """
CREATE TRIGGER recall_decision_not_truncated
BEFORE TRUNCATE ON recall_decision
FOR EACH STATEMENT EXECUTE FUNCTION recall_decision_append_only()
""",
    "REVOKE UPDATE, DELETE, TRUNCATE ON TABLE recall_decision FROM PUBLIC, CURRENT_USER",
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


_VALIDATED_FUNCTION = f"""
CREATE FUNCTION recall_dispatch_validated() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM recall_decision
        WHERE incident_id = NEW.incident_id AND {_in("decision", _APPROVALS)}
    ) THEN
        RETURN NULL;
    END IF;
    RAISE EXCEPTION 'aucun rappel ne part sans validation humaine'
        USING ERRCODE = 'check_violation',
              CONSTRAINT = 'ck_recall_dispatch_validated',
              TABLE = 'recall_dispatch';
END
$$
"""  # noqa: S608 — seule une liste fermée est interpolée.
_VALIDATED = """
CREATE CONSTRAINT TRIGGER ck_recall_dispatch_validated
AFTER INSERT OR UPDATE ON recall_dispatch
FOR EACH ROW EXECUTE FUNCTION recall_dispatch_validated()
"""

_FROZEN_FUNCTION = """
CREATE FUNCTION recall_list_frozen() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    touched uuid[];
BEGIN
    IF TG_OP = 'INSERT' THEN
        touched := ARRAY[NEW.incident_id];
    ELSIF TG_OP = 'DELETE' THEN
        touched := ARRAY[OLD.incident_id];
    ELSE
        touched := ARRAY[OLD.incident_id, NEW.incident_id];
    END IF;
    IF EXISTS (
        SELECT 1 FROM recall_decision
        WHERE incident_id = ANY(touched) AND decision <> 'escalated'
    ) THEN
        RAISE EXCEPTION 'une liste de rappel décidée ne change plus'
            USING ERRCODE = 'check_violation',
                  CONSTRAINT = 'ck_recall_list_frozen',
                  TABLE = TG_TABLE_NAME;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$$
"""


def upgrade() -> None:
    op.create_table(
        "recall_decision",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), nullable=False),
        sa.Column(
            "incident_id",
            sa.Uuid,
            sa.ForeignKey("incident.id", name="fk_recall_decision_incident_id_incident"),
            nullable=False,
        ),
        sa.Column(
            "decided_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("decision", sa.Text, nullable=False),
        sa.Column("author_issuer", sa.Text),
        sa.Column("author_subject", sa.Text),
        sa.Column("template", sa.Text),
        sa.Column("reason", sa.Text),
        sa.PrimaryKeyConstraint("id", name="pk_recall_decision"),
        sa.CheckConstraint(_in("decision", _DECISIONS), name="ck_recall_decision_decision"),
        sa.CheckConstraint(
            f"template IS NULL OR {_in('template', _TEMPLATES)}",
            name="ck_recall_decision_template",
        ),
        sa.CheckConstraint(
            f"reason IS NULL OR {_in('reason', _REFUSALS)}", name="ck_recall_decision_reason"
        ),
        sa.CheckConstraint(
            "(decision = 'escalated') = (author_issuer IS NULL)"
            " AND (author_issuer IS NULL) = (author_subject IS NULL)"
            " AND (author_issuer IS NULL OR char_length(author_issuer) BETWEEN 1 AND 1024)"
            " AND (author_subject IS NULL OR char_length(author_subject) BETWEEN 1 AND 255)",
            name="ck_recall_decision_author",
        ),
        sa.CheckConstraint(
            f"(template IS NOT NULL) = ({_in('decision', _APPROVALS)})"
            " AND (reason IS NOT NULL) = (decision = 'refused')",
            name="ck_recall_decision_given",
        ),
    )
    op.create_index(
        "uq_recall_decision_decided",
        "recall_decision",
        ["incident_id"],
        unique=True,
        postgresql_where=sa.text("decision <> 'escalated'"),
    )
    op.create_index(
        "uq_recall_decision_escalated",
        "recall_decision",
        ["incident_id"],
        unique=True,
        postgresql_where=sa.text("decision = 'escalated'"),
    )
    op.execute(_APPEND_ONLY_FUNCTION)
    for statement in _APPEND_ONLY:
        op.execute(statement)

    op.create_table(
        "recall_dispatch",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), nullable=False),
        sa.Column("incident_id", sa.Uuid, nullable=False),
        sa.Column("pseudonym", sa.Text, nullable=False),
        sa.Column(
            "enqueued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_recall_dispatch"),
        sa.UniqueConstraint(
            "incident_id", "pseudonym", name="uq_recall_dispatch_incident_id_pseudonym"
        ),
        sa.ForeignKeyConstraint(
            ["incident_id", "pseudonym"],
            ["recall_entry.incident_id", "recall_entry.pseudonym"],
            name="fk_recall_dispatch_incident_id_recall_entry",
        ),
    )
    op.execute(_VALIDATED_FUNCTION)
    op.execute(_VALIDATED)

    op.execute(_FROZEN_FUNCTION)
    for table in _FROZEN:
        op.execute(
            f"CREATE TRIGGER {table}_frozen BEFORE INSERT OR UPDATE OR DELETE ON {table}"
            " FOR EACH ROW EXECUTE FUNCTION recall_list_frozen()"
        )


def downgrade() -> None:
    for table in _FROZEN:
        op.execute(f"DROP TRIGGER {table}_frozen ON {table}")
    op.execute("DROP FUNCTION recall_list_frozen()")
    op.drop_table("recall_dispatch")
    op.execute("DROP FUNCTION recall_dispatch_validated()")
    op.drop_table("recall_decision")
    op.execute("DROP FUNCTION recall_decision_append_only()")
