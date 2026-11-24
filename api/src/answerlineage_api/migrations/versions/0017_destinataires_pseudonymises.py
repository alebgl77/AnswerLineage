"""Destinataires pseudonymisés : le journal des révélations, et rien d'autre qu'un pseudonyme.

``recipient_revelation`` journalise chaque révélation d'un destinataire, en ajout
seul : l'auteur, l'heure posée par la base, le motif d'une liste fermée, le
pseudonyme visé, le nombre d'identités candidates et le résultat ; jamais
l'identité. Une fonction et ses déclencheurs posent l'heure à l'insertion et
refusent toute modification, suppression ou troncature
(``ck_recipient_revelation_append_only``) ; ces droits sont aussi retirés au
propriétaire de la table.

Un destinataire n'entre plus que sous la forme d'un pseudonyme HMAC
(``hmac-sha256:`` suivi de 64 chiffres hexadécimaux). Ce qu'un émetteur a pu
envoyer avant, sous une autre forme, est peut-être une identité en clair, et ne
peut pas être converti sans elle : la montée le retire, des événements bruts
(les destinataires de leurs facettes de diffusion), du graphe (le destinataire et
ses liens) et des résultats d'analyse des incidents. Les décomptes des analyses
passées restent ceux de leur heure.

La descente retire le journal, sa fonction et ses déclencheurs : les
révélations journalisées sont perdues. Les destinataires retirés ne reviennent
pas.

Révision : 0017
Précédente : 0016
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | None = None
depends_on: str | None = None

_REASONS = ("recall", "data_subject_request", "legal_obligation", "incident_investigation")
_RESULTS = ("matched", "no_match", "unknown_recipient")
_PSEUDONYM = "^hmac-sha256:[0-9a-f]{64}$"

_APPEND_ONLY_FUNCTION = """
CREATE FUNCTION recipient_revelation_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.revealed_at := now();
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'le journal des révélations ne se modifie pas'
        USING ERRCODE = 'check_violation',
              CONSTRAINT = 'ck_recipient_revelation_append_only',
              TABLE = 'recipient_revelation';
END
$$
"""
_APPEND_ONLY = (
    """
CREATE TRIGGER recipient_revelation_stamped
BEFORE INSERT ON recipient_revelation
FOR EACH ROW EXECUTE FUNCTION recipient_revelation_append_only()
""",
    """
CREATE TRIGGER recipient_revelation_append_only
BEFORE UPDATE OR DELETE ON recipient_revelation
FOR EACH ROW EXECUTE FUNCTION recipient_revelation_append_only()
""",
    """
CREATE TRIGGER recipient_revelation_not_truncated
BEFORE TRUNCATE ON recipient_revelation
FOR EACH STATEMENT EXECUTE FUNCTION recipient_revelation_append_only()
""",
    "REVOKE UPDATE, DELETE, TRUNCATE ON TABLE recipient_revelation FROM PUBLIC, CURRENT_USER",
)

# Les destinataires d'un événement brut qui n'ont pas la forme d'un pseudonyme : retirés de
# chaque facette de diffusion, sous « facets » comme sous « outputFacets ». Une liste de
# destinataires qui n'en est pas une est vidée.
_PSEUDONYMS_ONLY = """
CREATE FUNCTION answerlineage_pseudonyms_only(event jsonb) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    outputs jsonb := event -> 'outputs';
    output jsonb;
    container text;
    place text[];
    recipients jsonb;
    kept jsonb;
    rank integer;
BEGIN
    IF jsonb_typeof(outputs) IS DISTINCT FROM 'array' THEN
        RETURN event;
    END IF;
    FOR rank IN 0 .. jsonb_array_length(outputs) - 1 LOOP
        output := outputs -> rank;
        FOREACH container IN ARRAY ARRAY['facets', 'outputFacets'] LOOP
            place := ARRAY[container, 'answerlineage_delivery', 'recipients'];
            recipients := output #> place;
            IF recipients IS NULL OR jsonb_typeof(recipients) = 'null' THEN
                CONTINUE;
            END IF;
            IF jsonb_typeof(recipients) = 'array' THEN
                SELECT coalesce(jsonb_agg(item ORDER BY position), '[]'::jsonb) INTO kept
                FROM jsonb_array_elements(recipients) WITH ORDINALITY AS listed(item, position)
                WHERE jsonb_typeof(item) = 'string'
                  AND item #>> '{}' ~ '^hmac-sha256:[0-9a-f]{64}$';
            ELSE
                kept := '[]'::jsonb;
            END IF;
            output := jsonb_set(output, place, kept);
        END LOOP;
        outputs := jsonb_set(outputs, ARRAY[rank::text], output);
    END LOOP;
    RETURN jsonb_set(event, '{outputs}', outputs);
END
$$
"""
_SCRUB = (
    """
    UPDATE event_raw SET payload = answerlineage_pseudonyms_only(payload)
    WHERE payload @? '$.outputs[*].*.answerlineage_delivery.recipients'
      AND answerlineage_pseudonyms_only(payload) IS DISTINCT FROM payload
    """,
    """
    DELETE FROM delivery_recipient WHERE recipient_id IN (
        SELECT id FROM recipient WHERE pseudonym !~ '^hmac-sha256:[0-9a-f]{64}$'
    )
    """,
    "DELETE FROM recipient WHERE pseudonym !~ '^hmac-sha256:[0-9a-f]{64}$'",
    "DELETE FROM incident_recipient WHERE pseudonym !~ '^hmac-sha256:[0-9a-f]{64}$'",
    "DROP FUNCTION answerlineage_pseudonyms_only(jsonb)",
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def upgrade() -> None:
    op.create_table(
        "recipient_revelation",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column(
            "revealed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("author_issuer", sa.Text, nullable=False),
        sa.Column("author_subject", sa.Text, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("pseudonym", sa.Text, nullable=False),
        sa.Column("candidates", sa.Integer, nullable=False),
        sa.Column("result", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_recipient_revelation"),
        sa.CheckConstraint(_in("reason", _REASONS), name="ck_recipient_revelation_reason"),
        sa.CheckConstraint(_in("result", _RESULTS), name="ck_recipient_revelation_result"),
        sa.CheckConstraint(f"pseudonym ~ '{_PSEUDONYM}'", name="ck_recipient_revelation_pseudonym"),
        sa.CheckConstraint(
            "candidates BETWEEN 1 AND 100", name="ck_recipient_revelation_candidates"
        ),
        sa.CheckConstraint(
            "char_length(author_issuer) BETWEEN 1 AND 1024"
            " AND char_length(author_subject) BETWEEN 1 AND 255",
            name="ck_recipient_revelation_author",
        ),
    )
    op.create_index(
        "ix_recipient_revelation_author",
        "recipient_revelation",
        ["author_issuer", "author_subject", "revealed_at"],
    )
    op.execute(_APPEND_ONLY_FUNCTION)
    for statement in _APPEND_ONLY:
        op.execute(statement)
    op.execute(_PSEUDONYMS_ONLY)
    for statement in _SCRUB:
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("recipient_revelation")
    op.execute("DROP FUNCTION recipient_revelation_append_only()")
