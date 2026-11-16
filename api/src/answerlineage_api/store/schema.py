"""Schéma du graphe de lignage.

Le graphe va du jeu de données au destinataire :
``dataset`` → ``read_edge`` → ``run`` → ``answer`` → ``delivery`` → ``recipient``.
Chaque arête et chaque nœud daté porte l'heure de l'événement (``event_time``,
déclarée par l'émetteur) et l'heure d'enregistrement (``recorded_at``). Dans le
graphe, l'heure d'enregistrement d'une ligne est la réception, par AnswerLineage,
du premier événement qui l'a fait connaître (``event_raw.recorded_at``) : ni
l'heure de passage du worker, ni celle d'un rejeu.

Les événements bruts, les relevés de couverture et le compteur de doublons
sont partitionnés par mois de réception (``partitions.py``) ; le graphe ne
l'est pas.

Les migrations d'Alembic font foi pour la base ; un test vérifie qu'elles
produisent exactement ce schéma, partitions du mois courant et des deux
suivants comprises.
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
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from answerlineage_api.identifiers import INSTANCE_MAX
from answerlineage_api.store.partitions import RECEPTION_MONTH, create_partitions_ahead

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


def _recorded_month() -> Column[object]:
    """Clé de partition : le mois UTC de réception, posé par la base avec ``recorded_at``."""
    return Column(
        "recorded_month",
        Date,
        nullable=False,
        server_default=text(RECEPTION_MONTH.format(column="now()")),
    )


def _follows_reception() -> CheckConstraint:
    return CheckConstraint(
        f"recorded_month = {RECEPTION_MONTH.format(column='recorded_at')}", name="recorded_month"
    )


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
    # Première et dernière heure d'événement reçues pour cette lecture : un
    # événement antidaté élargit l'intervalle, il ne le déplace pas.
    Column("event_time", DateTime(timezone=True), nullable=False),
    Column("last_event_time", DateTime(timezone=True), nullable=False),
    _recorded_at(),
    UniqueConstraint(
        "run_id", "dataset_id", "dataset_version_id", postgresql_nulls_not_distinct=True
    ),
    CheckConstraint(_in("precision", READ_PRECISIONS), name="precision"),
    CheckConstraint(_in("origin", READ_ORIGINS), name="origin"),
    CheckConstraint("event_time <= last_event_time", name="event_times"),
    Index(None, "dataset_id", "event_time"),
    Index(None, "dataset_version_id"),
)
# Les lectures étalées sur plusieurs heures, seules : la propagation y cherche
# celles qui commencent avant une fenêtre et la recoupent.
Index(
    "ix_read_edge_dataset_id_last_event_time",
    read_edge.c.dataset_id,
    read_edge.c.last_event_time,
    postgresql_where=read_edge.c.last_event_time > read_edge.c.event_time,
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
    Column("event_time", DateTime(timezone=True), nullable=False),
    _recorded_at(),
    PrimaryKeyConstraint("delivery_id", "recipient_id"),
    Index(None, "recipient_id"),
)

# Événement reçu tel quel, rejouable. Son identifiant est l'empreinte de sa
# forme canonique : un événement rejoué ne s'enregistre qu'une fois. La table
# est partitionnée par mois de réception ; la clé primaire contient donc ce
# mois, et l'ingestion cherche un événement déjà reçu dans toutes les partitions.
event_raw = Table(
    "event_raw",
    metadata,
    Column("event_id", Text, nullable=False),
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
    _recorded_month(),
    PrimaryKeyConstraint("event_id", "recorded_month"),
    _follows_reception(),
    Index(None, "run_id"),
    Index(None, "recorded_at"),
    postgresql_partition_by="RANGE (recorded_month)",
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
# L'événement est désigné par son identifiant et son mois de réception, sans
# clé étrangère : elle empêcherait de supprimer une partition purgée. La purge
# retire de la file les événements du mois qu'elle supprime.
ingest_queue = Table(
    "ingest_queue",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("event_id", Text, nullable=False),
    Column("recorded_month", Date, nullable=False),
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
# Partitionnée par mois du jour de réception, qui est dans sa clé.
ingest_duplicate = Table(
    "ingest_duplicate",
    metadata,
    Column("emitter", Text, nullable=False),
    Column("day", Date, nullable=False),
    Column("duplicates", BigInteger, nullable=False),
    PrimaryKeyConstraint("emitter", "day"),
    CheckConstraint("duplicates > 0", name="positive"),
    postgresql_partition_by="RANGE (day)",
)
# Les jours les plus récents d'abord, pour la route de lecture.
Index("ix_ingest_duplicate_day_emitter", ingest_duplicate.c.day.desc(), ingest_duplicate.c.emitter)

# Relevés de couverture : ce qu'un émetteur a vu pendant une fenêtre. Deux
# relevés de la même fenêtre se fusionnent compteur par compteur, au plus grand :
# l'écriture est idempotente et commutative. La table est partitionnée par mois
# de première réception de la fenêtre : un relevé republié un autre mois se
# fusionne dans la ligne existante, sans changer de partition.
coverage_report = Table(
    "coverage_report",
    metadata,
    Column("emitter", Text, nullable=False),
    # Instance de l'émetteur ; vide pour un relevé qui n'en porte pas, une instance à part.
    Column("instance", Text, nullable=False, server_default=text("''")),
    Column("window_start", DateTime(timezone=True), nullable=False),
    Column("window_end", DateTime(timezone=True), nullable=False),
    Column("calls_seen", BigInteger, nullable=False),
    Column("events_emitted", BigInteger, nullable=False),
    Column("events_lost", BigInteger, nullable=False),
    # Requêtes relayées sans pouvoir être inspectées : elles cachent peut-être des appels.
    Column("uninspected", BigInteger, nullable=False),
    _recorded_at(),
    _recorded_month(),
    PrimaryKeyConstraint("emitter", "instance", "window_start", "window_end", "recorded_month"),
    CheckConstraint("window_start < window_end", name="window"),
    CheckConstraint(f"char_length(instance) <= {INSTANCE_MAX}", name="instance"),
    CheckConstraint(
        "calls_seen >= 0 AND events_emitted >= 0 AND events_lost >= 0 AND uninspected >= 0",
        name="counts",
    ),
    _follows_reception(),
    postgresql_partition_by="RANGE (recorded_month)",
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

# Partitions manquantes signalées par le contrôle des workers : une ligne par
# table et par mois, tant que la partition manque. Un épisode se signale une fois.
partition_alert = Table(
    "partition_alert",
    metadata,
    Column("table_name", Text, nullable=False),
    Column("month", Date, nullable=False),
    Column("signalled_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint("table_name", "month"),
)

# Une table partitionnée naît avec la partition du mois courant et des mois à venir.
for _partitioned in (event_raw, coverage_report, ingest_duplicate):
    event.listen(_partitioned, "after_create", create_partitions_ahead)
