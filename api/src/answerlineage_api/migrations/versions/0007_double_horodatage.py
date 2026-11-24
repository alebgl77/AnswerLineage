"""Double horodatage du graphe : heure de réception et intervalle des lectures.

Chaque ligne du graphe prend pour heure d'enregistrement la réception, par
AnswerLineage, du premier événement qui l'a fait connaître. Une lecture garde
la première et la dernière heure d'événement reçues ; le lien d'une diffusion
à son destinataire reçoit son heure d'événement.

Reprise des lignes existantes : chacune est rattachée aux événements reçus qui
la font connaître (même run, même jeu de données ou même réponse, nommés sous
leur forme NFC comme le fait la projection), et prend la plus ancienne de leurs
réceptions. Une ligne qu'aucun événement reçu ne rattache garde son heure
d'enregistrement, celle du passage du worker : elle suit toujours la réception,
et ne fait donc jamais paraître une ligne connue plus tôt qu'elle ne l'a été.
Un nœud partagé (jeu de données, version, destinataire) prend la plus ancienne
réception de ses arêtes, un agent ou un outil celle des runs qui le nomment.

La descente retire les colonnes ajoutées ; les heures d'enregistrement reprises
restent.

Révision : 0007
Précédente : 0006
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None


def _string(path: str) -> str:
    """Un texte de l'événement, sous sa forme NFC ; nul quand ce n'est pas une chaîne."""
    return f"CASE WHEN jsonb_typeof({path}) = 'string' THEN normalize({path} #>> '{{}}', NFC) END"


def _elements(array: str) -> str:
    return f"jsonb_array_elements(CASE WHEN jsonb_typeof({array}) = 'array' THEN {array} END)"


def _dataset(alias: str) -> str:
    return f"""
        {_string(f"{alias} -> 'namespace'")} AS namespace,
        {_string(f"{alias} -> 'name'")} AS name,
        {_string(f"{alias} -> 'facets' -> 'version' -> 'datasetVersion'")} AS version"""


# Ce que chaque événement reçu fait connaître : ses entrées, ses sorties.
_OBSERVED_INPUTS = f"""
    CREATE TEMPORARY TABLE observed_input ON COMMIT DROP AS
    SELECT e.run_id, e.event_time, e.recorded_at, {_dataset("i")}
    FROM event_raw e CROSS JOIN LATERAL {_elements("e.payload -> 'inputs'")} AS i
    WHERE jsonb_typeof(i) = 'object'
"""  # noqa: S608 — seuls des fragments constants sont interpolés.
_DELIVERY = """COALESCE(
        CASE WHEN jsonb_typeof(o -> 'facets' -> 'answerlineage_delivery') = 'object'
             THEN o -> 'facets' -> 'answerlineage_delivery' END,
        CASE WHEN jsonb_typeof(o -> 'outputFacets' -> 'answerlineage_delivery') = 'object'
             THEN o -> 'outputFacets' -> 'answerlineage_delivery' END)"""
_OBSERVED_OUTPUTS = f"""
    CREATE TEMPORARY TABLE observed_output ON COMMIT DROP AS
    SELECT e.run_id, e.event_time, e.recorded_at, {_dataset("o")},
           {_DELIVERY} AS delivery
    FROM event_raw e CROSS JOIN LATERAL {_elements("e.payload -> 'outputs'")} AS o
    WHERE jsonb_typeof(o) = 'object'
"""  # noqa: S608 — seuls des fragments constants sont interpolés.
_FINGERPRINT = _string("delivery -> 'fingerprint'")
_OBSERVED_ANSWERS = f"""
    CREATE TEMPORARY TABLE observed_answer ON COMMIT DROP AS
    SELECT run_id, event_time, recorded_at,
           {_string("delivery -> 'fingerprint'")} AS fingerprint,
           {_string("delivery -> 'channel'")} AS channel,
           {_string("delivery -> 'messageRef'")} AS message_ref,
           delivery -> 'recipients' AS recipients
    FROM observed_output
    WHERE {_FINGERPRINT} <> ''
"""  # noqa: S608 — seuls des fragments constants sont interpolés.

_RUNS = """
    UPDATE run r SET recorded_at = LEAST(r.recorded_at, e.received)
    FROM (SELECT run_id, min(recorded_at) AS received FROM event_raw GROUP BY run_id) e
    WHERE e.run_id = r.run_id
"""


def _edges(table: str, observed: str, *, extra: str = "") -> str:
    """Une lecture ou une écriture, et les événements de son run qui nomment son jeu de données."""
    return f"""
        UPDATE {table} x SET recorded_at = LEAST(x.recorded_at, m.received){extra}
        FROM (
            SELECT x.id, min(o.recorded_at) AS received, max(o.event_time) AS last_time
            FROM {table} x
            JOIN dataset d ON d.id = x.dataset_id
            LEFT JOIN dataset_version v ON v.id = x.dataset_version_id
            JOIN {observed} o ON o.run_id = x.run_id AND o.namespace = d.namespace
                AND o.name = d.name AND o.version IS NOT DISTINCT FROM v.version
            GROUP BY x.id
        ) m
        WHERE m.id = x.id
    """  # noqa: S608 — seuls des noms constants sont interpolés.


# Une sortie sans réponse lisible est une écriture, comme pour la projection.
_WRITTEN = f"""(
    SELECT * FROM observed_output
    WHERE delivery IS NULL OR {_FINGERPRINT} IS NULL OR {_FINGERPRINT} = ''
)"""  # noqa: S608 — seul un fragment constant est interpolé.

_ANSWERS = """
    UPDATE answer a SET recorded_at = LEAST(a.recorded_at, m.received)
    FROM (
        SELECT a.id, min(o.recorded_at) AS received
        FROM answer a
        JOIN observed_answer o ON o.run_id = a.run_id AND o.fingerprint = a.fingerprint
        GROUP BY a.id
    ) m
    WHERE m.id = a.id
"""
_DELIVERIES = """
    UPDATE delivery d SET recorded_at = LEAST(d.recorded_at, m.received)
    FROM (
        SELECT d.id, min(o.recorded_at) AS received
        FROM delivery d JOIN answer a ON a.id = d.answer_id
        JOIN observed_answer o ON o.run_id = a.run_id AND o.fingerprint = a.fingerprint
            AND o.channel = d.channel AND o.message_ref = d.message_ref
        GROUP BY d.id
    ) m
    WHERE m.id = d.id
"""
_LINKS = f"""
    UPDATE delivery_recipient l
    SET recorded_at = LEAST(l.recorded_at, m.received), event_time = m.first_time
    FROM (
        SELECT l.delivery_id, l.recipient_id,
               min(o.recorded_at) AS received, min(o.event_time) AS first_time
        FROM delivery_recipient l
        JOIN delivery d ON d.id = l.delivery_id
        JOIN answer a ON a.id = d.answer_id
        JOIN recipient c ON c.id = l.recipient_id
        JOIN observed_answer o ON o.run_id = a.run_id AND o.fingerprint = a.fingerprint
            AND o.channel = d.channel AND o.message_ref = d.message_ref
        WHERE EXISTS (
            SELECT 1 FROM {_elements("o.recipients")} AS p
            WHERE {_string("p")} = c.pseudonym
        )
        GROUP BY l.delivery_id, l.recipient_id
    ) m
    WHERE m.delivery_id = l.delivery_id AND m.recipient_id = l.recipient_id
"""  # noqa: S608 — seuls des fragments constants sont interpolés.
# Sans événement qui le nomme, le lien prend l'heure de sa diffusion.
_UNOBSERVED_LINKS = """
    UPDATE delivery_recipient l SET event_time = d.event_time
    FROM delivery d WHERE d.id = l.delivery_id AND l.event_time IS NULL
"""


def _node(table: str, source: str, column: str) -> str:
    """Un nœud partagé : la plus ancienne réception des lignes qui le désignent."""
    return f"""
        UPDATE {table} n SET recorded_at = LEAST(n.recorded_at, m.received)
        FROM (
            SELECT {column} AS id, min(recorded_at) AS received FROM {source}
            WHERE {column} IS NOT NULL GROUP BY {column}
        ) m
        WHERE m.id = n.id
    """  # noqa: S608 — seuls des noms constants sont interpolés.


_EDGES_OF_DATASETS = (
    "(SELECT dataset_id, dataset_version_id, recorded_at FROM read_edge"
    " UNION ALL SELECT dataset_id, dataset_version_id, recorded_at FROM write_edge) AS edges"
)


def upgrade() -> None:
    op.add_column("read_edge", sa.Column("last_event_time", sa.DateTime(timezone=True)))
    op.add_column("delivery_recipient", sa.Column("event_time", sa.DateTime(timezone=True)))
    op.execute("UPDATE read_edge SET last_event_time = event_time")

    for statement in (_OBSERVED_INPUTS, _OBSERVED_OUTPUTS, _OBSERVED_ANSWERS, _RUNS):
        op.execute(statement)
    op.execute(
        _edges(
            "read_edge",
            "observed_input",
            extra=", last_event_time = GREATEST(x.event_time, m.last_time)",
        )
    )
    op.execute(_edges("write_edge", _WRITTEN))
    for statement in (_ANSWERS, _DELIVERIES, _LINKS, _UNOBSERVED_LINKS):
        op.execute(statement)
    op.execute(_node("dataset", _EDGES_OF_DATASETS, "dataset_id"))
    op.execute(_node("dataset_version", _EDGES_OF_DATASETS, "dataset_version_id"))
    op.execute(_node("recipient", "delivery_recipient", "recipient_id"))
    op.execute(_node("agent", "run", "agent_id"))
    op.execute(_node("tool", "run", "tool_id"))

    op.alter_column("read_edge", "last_event_time", nullable=False)
    op.alter_column("delivery_recipient", "event_time", nullable=False)
    op.create_check_constraint(
        "ck_read_edge_event_times", "read_edge", "event_time <= last_event_time"
    )
    op.create_index(
        "ix_read_edge_dataset_id_last_event_time",
        "read_edge",
        ["dataset_id", "last_event_time"],
        postgresql_where=sa.text("last_event_time > event_time"),
    )


def downgrade() -> None:
    op.drop_index("ix_read_edge_dataset_id_last_event_time", table_name="read_edge")
    op.drop_constraint("ck_read_edge_event_times", "read_edge", type_="check")
    op.drop_column("delivery_recipient", "event_time")
    op.drop_column("read_edge", "last_event_time")
