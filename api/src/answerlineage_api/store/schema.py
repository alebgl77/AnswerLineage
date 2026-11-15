"""Schéma du graphe de lignage.

Le graphe va du jeu de données au destinataire :
``dataset`` → ``read_edge`` → ``run`` → ``answer`` → ``delivery`` → ``recipient``.
Chaque arête porte l'heure de l'événement (``event_time``, déclarée par
l'émetteur) et l'heure d'enregistrement (``recorded_at``, posée par la base).

Les migrations d'Alembic font foi pour la base ; un test vérifie qu'elles
produisent exactement ce schéma.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

RUN_KINDS = ("agent", "tool", "job")
RUN_STATES = ("START", "RUNNING", "COMPLETE", "ABORT", "FAIL", "OTHER")
DATASET_KINDS = ("table", "document", "api", "memory", "answer")
READ_PRECISIONS = ("table", "columns", "subset", "keys")
READ_ORIGINS = ("analysis", "reconciliation")

READ_EDGE_KEY = "uq_read_edge_run_id_dataset_id_dataset_version_id"
WRITE_EDGE_KEY = "uq_write_edge_run_id_dataset_id_dataset_version_id"

metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def _recorded_at() -> Column[object]:
    return Column("recorded_at", DateTime(timezone=True), nullable=False, server_default=func.now())


agent = Table(
    "agent",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("namespace", Text, nullable=False),
    Column("name", Text, nullable=False),
    _recorded_at(),
    UniqueConstraint("namespace", "name"),
)

tool = Table(
    "tool",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("server", Text, nullable=False),
    Column("name", Text, nullable=False),
    _recorded_at(),
    UniqueConstraint("server", "name"),
)

dataset = Table(
    "dataset",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("namespace", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("kind", Text, nullable=False),
    _recorded_at(),
    UniqueConstraint("namespace", "name"),
    CheckConstraint(_in("kind", DATASET_KINDS), name="kind"),
)

dataset_version = Table(
    "dataset_version",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("dataset_id", BigInteger, ForeignKey("dataset.id"), nullable=False),
    Column("version", Text, nullable=False),
    _recorded_at(),
    UniqueConstraint("dataset_id", "version"),
)

run = Table(
    "run",
    metadata,
    Column("run_id", Uuid, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("agent_id", BigInteger, ForeignKey("agent.id")),
    Column("tool_id", BigInteger, ForeignKey("tool.id")),
    # Pas de clé étrangère : le run parent peut arriver après son enfant.
    Column("parent_run_id", Uuid),
    Column("trace_id", Text),
    Column("span_id", Text),
    Column("started_at", DateTime(timezone=True)),
    Column("ended_at", DateTime(timezone=True)),
    Column("state", Text, nullable=False),
    # Heure du dernier événement pris en compte pour ``state``.
    Column("state_time", DateTime(timezone=True), nullable=False),
    _recorded_at(),
    CheckConstraint(_in("kind", RUN_KINDS), name="kind"),
    CheckConstraint(_in("state", RUN_STATES), name="state"),
    Index(None, "parent_run_id"),
    Index(None, "trace_id"),
    Index(None, "agent_id"),
    Index(None, "tool_id"),
)

read_edge = Table(
    "read_edge",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("run_id", Uuid, ForeignKey("run.run_id"), nullable=False),
    Column("dataset_id", BigInteger, ForeignKey("dataset.id"), nullable=False),
    Column("dataset_version_id", BigInteger, ForeignKey("dataset_version.id")),
    Column("precision", Text, nullable=False),
    Column("columns", ARRAY(Text), nullable=False, server_default="{}"),
    Column("origin", Text, nullable=False),
    Column("event_time", DateTime(timezone=True), nullable=False),
    _recorded_at(),
    UniqueConstraint(
        "run_id", "dataset_id", "dataset_version_id", postgresql_nulls_not_distinct=True
    ),
    CheckConstraint(_in("precision", READ_PRECISIONS), name="precision"),
    CheckConstraint(_in("origin", READ_ORIGINS), name="origin"),
    Index(None, "dataset_id", "event_time"),
    Index(None, "dataset_version_id"),
)

write_edge = Table(
    "write_edge",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("run_id", Uuid, ForeignKey("run.run_id"), nullable=False),
    Column("dataset_id", BigInteger, ForeignKey("dataset.id"), nullable=False),
    Column("dataset_version_id", BigInteger, ForeignKey("dataset_version.id")),
    Column("event_time", DateTime(timezone=True), nullable=False),
    _recorded_at(),
    UniqueConstraint(
        "run_id", "dataset_id", "dataset_version_id", postgresql_nulls_not_distinct=True
    ),
    Index(None, "dataset_id", "event_time"),
    Index(None, "dataset_version_id"),
)

answer = Table(
    "answer",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("run_id", Uuid, ForeignKey("run.run_id"), nullable=False),
    # Empreinte de la réponse : le contenu n'est jamais stocké.
    Column("fingerprint", Text, nullable=False),
    Column("event_time", DateTime(timezone=True), nullable=False),
    _recorded_at(),
    UniqueConstraint("run_id", "fingerprint"),
    # Les réponses les plus récentes d'abord, pour l'explorateur.
    Index(None, "event_time"),
)

delivery = Table(
    "delivery",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("answer_id", BigInteger, ForeignKey("answer.id"), nullable=False),
    Column("channel", Text, nullable=False),
    Column("message_ref", Text, nullable=False),
    Column("event_time", DateTime(timezone=True), nullable=False),
    _recorded_at(),
    UniqueConstraint("answer_id", "channel", "message_ref"),
    Index(None, "channel", "message_ref"),
)

recipient = Table(
    "recipient",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    # Pseudonyme HMAC du destinataire : aucune table de correspondance.
    Column("pseudonym", Text, nullable=False),
    _recorded_at(),
    UniqueConstraint("pseudonym"),
)

delivery_recipient = Table(
    "delivery_recipient",
    metadata,
    Column("delivery_id", BigInteger, ForeignKey("delivery.id"), nullable=False),
    Column("recipient_id", BigInteger, ForeignKey("recipient.id"), nullable=False),
    _recorded_at(),
    PrimaryKeyConstraint("delivery_id", "recipient_id"),
    Index(None, "recipient_id"),
)

# Événement reçu tel quel, rejouable. Son identifiant est l'empreinte de sa
# forme canonique : un événement rejoué ne s'enregistre qu'une fois.
event_raw = Table(
    "event_raw",
    metadata,
    Column("event_id", Text, primary_key=True),
    Column("run_id", Uuid, nullable=False),
    Column("event_type", Text, nullable=False),
    Column("event_time", DateTime(timezone=True), nullable=False),
    Column("producer", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    # Émetteur : l'espace de noms du job, sous sa forme normalisée.
    Column("emitter", Text, nullable=False),
    # Vrai pour l'événement d'un appel d'outil (facette ``answerlineage_toolCall``).
    Column("tool_call", Boolean, nullable=False),
    _recorded_at(),
    Index(None, "run_id"),
    Index(None, "recorded_at"),
)
# Les appels d'un émetteur par heure, comptés pour mesurer la couverture du lignage.
Index(
    "ix_event_raw_emitter_event_time",
    event_raw.c.emitter,
    event_raw.c.event_time,
    postgresql_where=event_raw.c.tool_call,
)

# File de traitement : une ligne par événement à projeter dans le graphe. Elle
# est écrite dans la transaction qui enregistre l'événement et retirée dans
# celle qui le projette : aucun événement n'est perdu ni traité deux fois.
ingest_queue = Table(
    "ingest_queue",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("event_id", Text, ForeignKey("event_raw.event_id"), nullable=False),
    Column("enqueued_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("available_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("attempts", Integer, nullable=False, server_default="0"),
    # Classe de la dernière erreur, jamais son message.
    Column("last_error", Text),
    UniqueConstraint("event_id"),
    Index(None, "available_at", "id"),
)

# Doublons reçus, par émetteur et par jour (date UTC de réception). Une ligne
# n'existe qu'à partir du premier doublon : un envoi sans doublon n'y touche pas.
ingest_duplicate = Table(
    "ingest_duplicate",
    metadata,
    Column("emitter", Text, nullable=False),
    Column("day", Date, nullable=False),
    Column("duplicates", BigInteger, nullable=False),
    PrimaryKeyConstraint("emitter", "day"),
    CheckConstraint("duplicates > 0", name="positive"),
)
# Les jours les plus récents d'abord, pour la route de lecture.
Index("ix_ingest_duplicate_day_emitter", ingest_duplicate.c.day.desc(), ingest_duplicate.c.emitter)

# Relevés de couverture : ce qu'un émetteur a vu pendant une fenêtre. Deux
# relevés de la même fenêtre se fusionnent compteur par compteur, au plus grand :
# l'écriture est idempotente et commutative.
coverage_report = Table(
    "coverage_report",
    metadata,
    Column("emitter", Text, nullable=False),
    Column("window_start", DateTime(timezone=True), nullable=False),
    Column("window_end", DateTime(timezone=True), nullable=False),
    Column("calls_seen", BigInteger, nullable=False),
    Column("events_emitted", BigInteger, nullable=False),
    Column("events_lost", BigInteger, nullable=False),
    # Requêtes relayées sans pouvoir être inspectées : elles cachent peut-être des appels.
    Column("uninspected", BigInteger, nullable=False),
    _recorded_at(),
    PrimaryKeyConstraint("emitter", "window_start", "window_end"),
    CheckConstraint("window_start < window_end", name="window"),
    CheckConstraint(
        "calls_seen >= 0 AND events_emitted >= 0 AND events_lost >= 0 AND uninspected >= 0",
        name="counts",
    ),
)

# Émetteurs connus : ceux qui ont publié au moins un relevé. ``last_report_at``
# est l'heure de réception du dernier relevé qui a changé quelque chose ; le
# contrôle des émetteurs muets pose ``silent_since`` à cette heure quand il
# signale un silence, une fois par épisode.
coverage_emitter = Table(
    "coverage_emitter",
    metadata,
    Column("emitter", Text, primary_key=True),
    Column("last_report_at", DateTime(timezone=True), nullable=False),
    Column("silent_since", DateTime(timezone=True)),
    Column("signalled_at", DateTime(timezone=True)),
)
