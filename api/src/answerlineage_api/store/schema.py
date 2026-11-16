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

Les incidents déclarés, leur périmètre et le résultat de leur analyse sont
rangés à côté du graphe (``incident`` et les tables qui en dépendent) : des
identifiants, des heures et des décomptes, jamais de texte libre.

Les événements que la projection n'a pas pu intégrer au graphe attendent dans
la file de rebut (``dead_letter``), qui les désigne sans les recopier.

Les tables du graphe (``GRAPH``) se déduisent toutes des événements bruts : la
reconstruction les vide et les rejoue, et garde la trace de chaque
reconstruction (``graph_rebuild``).

Les migrations d'Alembic font foi pour la base ; un test vérifie qu'elles
produisent exactement ce schéma, partitions du mois courant et des deux
suivants comprises.
"""

from __future__ import annotations

from typing import Final, Literal, get_args

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
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

# Gravité d'un incident, de la plus faible à la plus forte.
Severity = Literal["low", "medium", "high", "critical"]
SEVERITIES: Final[tuple[str, ...]] = get_args(Severity)
# État d'un incident. La liste est fermée et s'allonge avec le cycle de vie de
# l'incident : une révision de la base étend la contrainte qui la tient.
IncidentState = Literal["qualified"]
INCIDENT_STATES: Final[tuple[str, ...]] = get_args(IncidentState)
# État de l'analyse d'impact d'un incident, et code de son dernier échec.
AnalysisState = Literal["pending", "done", "failed"]
ANALYSIS_STATES: Final[tuple[str, ...]] = get_args(AnalysisState)
AnalysisError = Literal["query_timeout", "analysis_error"]
ANALYSIS_ERRORS: Final[tuple[str, ...]] = get_args(AnalysisError)
# Durée maximale de la fenêtre de validité d'un incident.
INCIDENT_WINDOW_DAYS: Final = 31
# Motif de la mise au rebut d'un événement, d'une liste fermée : une classe
# d'erreur, jamais le message d'une exception. Les quatre premiers ne se
# corrigent pas en réessayant ; les deux derniers sont réessayés d'abord.
DeadLetterReason = Literal[
    "invalid_identifier",
    "invalid_event",
    "constraint_violation",
    "invalid_value",
    "database_error",
    "projection_error",
]
DEAD_LETTER_REASONS: Final[tuple[str, ...]] = get_args(DeadLetterReason)

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

# Les tables du graphe : celles qu'écrit la projection des événements, et
# seulement elles. Tout ce qu'elles tiennent se déduit des événements bruts : la
# reconstruction les vide et les remplit de nouveau en rejouant les événements.
# Leurs identifiants numériques sont tirés à l'écriture : rien hors du graphe ne
# les cite, et l'API ne les rend jamais.
GRAPH: Final[tuple[Table, ...]] = (
    agent,
    tool,
    dataset,
    dataset_version,
    run,
    read_edge,
    write_edge,
    answer,
    delivery,
    recipient,
    delivery_recipient,
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
# celle qui le projette, ou qui le met au rebut : aucun événement n'est perdu
# ni traité deux fois. L'événement est désigné par son identifiant et son mois
# de réception, sans clé étrangère : elle empêcherait de supprimer une
# partition purgée. La purge retire de la file les événements du mois qu'elle
# supprime.
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
    # Premier essai en échec : nul tant qu'aucun n'a échoué.
    Column("first_failed_at", DateTime(timezone=True)),
    UniqueConstraint("event_id"),
    CheckConstraint("(attempts = 0) = (first_failed_at IS NULL)", name="first_failure"),
    Index(None, "available_at", "id"),
)

# File de rebut : les événements que la projection n'a pas pu intégrer au
# graphe. Un worker y met un événement dans la transaction qui le retire de la
# file ; un rejeu l'en retire dans celle qui le remet en file. Comme la file, le
# rebut désigne l'événement brut par son identifiant et son mois de réception,
# sans le recopier, et la purge retire les événements du mois qu'elle supprime.
# L'émetteur est celui d'``event_raw`` ; le motif, un code de la liste fermée.
dead_letter = Table(
    "dead_letter",
    metadata,
    Column("event_id", Text, primary_key=True),
    Column("recorded_month", Date, nullable=False),
    Column("emitter", Text, nullable=False),
    Column("reason", Text, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("first_failed_at", DateTime(timezone=True), nullable=False),
    Column("last_failed_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(_in("reason", DEAD_LETTER_REASONS), name="reason"),
    CheckConstraint("attempts >= 1", name="attempts"),
    CheckConstraint("first_failed_at <= last_failed_at", name="failures"),
    # Les plus récemment mis au rebut d'abord, page par page.
    Index(None, "last_failed_at", "event_id"),
    # Les décomptes par motif d'un émetteur.
    Index(None, "emitter", "reason"),
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

# Incident déclaré par un responsable de données : un jeu de données ou plusieurs
# (``incident_dataset``), faux pendant une fenêtre de validité, d'une gravité
# donnée. Aucun texte libre : ni titre, ni description.
incident = Table(
    "incident",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("state", Text, nullable=False),
    Column("severity", Text, nullable=False),
    Column("window_start", DateTime(timezone=True), nullable=False),
    Column("window_end", DateTime(timezone=True), nullable=False),
    Column("declared_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(_in("state", INCIDENT_STATES), name="state"),
    CheckConstraint(_in("severity", SEVERITIES), name="severity"),
    CheckConstraint("window_start < window_end", name="window"),
    CheckConstraint(
        f"window_end - window_start <= interval '{INCIDENT_WINDOW_DAYS} days'",
        name="window_length",
    ),
    # Les incidents les plus récents d'abord, pour la liste.
    Index(None, "declared_at", "id"),
)

# Périmètre d'un incident : les jeux de données faux, par espace de noms et nom.
incident_dataset = Table(
    "incident_dataset",
    metadata,
    Column("incident_id", Uuid, ForeignKey("incident.id"), nullable=False),
    Column("namespace", Text, nullable=False),
    Column("name", Text, nullable=False),
    PrimaryKeyConstraint("incident_id", "namespace", "name"),
)

# Demande d'analyse d'un incident et son résultat : écrite avec l'incident, dans
# la même transaction, réclamée par un worker (``FOR UPDATE SKIP LOCKED``). Les
# colonnes du résultat ne sont remplies que pour une analyse faite.
incident_analysis = Table(
    "incident_analysis",
    metadata,
    Column("incident_id", Uuid, ForeignKey("incident.id"), primary_key=True),
    Column("state", Text, nullable=False, server_default=text("'pending'")),
    Column("requested_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("available_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("attempts", Integer, nullable=False, server_default="0"),
    # Code du dernier échec, d'une liste fermée : jamais un message.
    Column("error", Text),
    # Le graphe est lu tel qu'il était connu à cette heure, fixée au premier essai.
    Column("known_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    Column("answers", BigInteger),
    Column("recipients", BigInteger),
    Column("truncated", Boolean),
    # Couverture du lignage sur la fenêtre et ses trous : des émetteurs, des heures, des nombres.
    Column("coverage", JSONB),
    # Jeux du périmètre que le graphe ne connaissait pas : espace de noms et nom.
    Column("unknown_datasets", JSONB),
    CheckConstraint(_in("state", ANALYSIS_STATES), name="state"),
    CheckConstraint(f"error IS NULL OR {_in('error', ANALYSIS_ERRORS)}", name="error"),
    CheckConstraint("state <> 'failed' OR error IS NOT NULL", name="failure"),
    CheckConstraint("(state = 'pending') = (finished_at IS NULL)", name="finished"),
    CheckConstraint(
        "(state = 'done') = (known_at IS NOT NULL AND answers IS NOT NULL"
        " AND recipients IS NOT NULL AND truncated IS NOT NULL AND coverage IS NOT NULL"
        " AND unknown_datasets IS NOT NULL)",
        name="result",
    ),
    CheckConstraint("attempts >= 0 AND answers >= 0 AND recipients >= 0", name="counts"),
)
# Les demandes en attente, par heure de reprise : ce que les workers réclament.
Index(
    "ix_incident_analysis_available_at",
    incident_analysis.c.available_at,
    postgresql_where=incident_analysis.c.state == "pending",
)

# Réponses touchées par un incident, désignées comme dans le graphe : run et
# empreinte, jamais le contenu. Leur identifiant stable s'en déduit.
incident_answer = Table(
    "incident_answer",
    metadata,
    Column("incident_id", Uuid, ForeignKey("incident.id"), nullable=False),
    Column("run_id", Uuid, nullable=False),
    Column("fingerprint", Text, nullable=False),
    Column("produced_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint("incident_id", "run_id", "fingerprint"),
    # Les réponses d'un incident, les plus récentes d'abord, page par page.
    Index(None, "incident_id", "produced_at", "run_id"),
)

# Destinataires de chaque réponse touchée, par leur pseudonyme.
incident_recipient = Table(
    "incident_recipient",
    metadata,
    Column("incident_id", Uuid, nullable=False),
    Column("run_id", Uuid, nullable=False),
    Column("fingerprint", Text, nullable=False),
    Column("pseudonym", Text, nullable=False),
    PrimaryKeyConstraint("incident_id", "run_id", "fingerprint", "pseudonym"),
    ForeignKeyConstraint(
        ["incident_id", "run_id", "fingerprint"],
        [incident_answer.c.incident_id, incident_answer.c.run_id, incident_answer.c.fingerprint],
    ),
)

# Reconstructions du graphe depuis les événements bruts, une ligne chacune : tant
# qu'elle tourne, puis pour mémoire. La reconstruction remet en file les
# événements conservés ; ``queue_boundary`` est la dernière ligne de la file
# qu'elle doit voir projetée : celles qu'elle y a mises, et celles qui y
# attendaient. Des heures et des décomptes, aucun contenu.
graph_rebuild = Table(
    "graph_rebuild",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("finished_at", DateTime(timezone=True)),
    Column("queue_boundary", BigInteger, nullable=False),
    # Événements remis à la projection : remis en file, ou qui y attendaient.
    Column("replayed", BigInteger, nullable=False),
    # Événements au rebut, laissés tels quels : ils n'étaient pas dans le graphe.
    Column("dead_letters", BigInteger, nullable=False),
    # Réceptions du premier et du dernier événement brut conservé : ce que la
    # conservation a purgé avant le premier ne peut pas être rejoué.
    Column("first_received", DateTime(timezone=True)),
    Column("last_received", DateTime(timezone=True)),
    # Lignes du graphe enregistrées avant le premier événement conservé : la
    # reconstruction ne les rend pas telles qu'elles étaient.
    Column("unrecoverable", BigInteger, nullable=False),
    CheckConstraint(
        "queue_boundary >= 0 AND replayed >= 0 AND dead_letters >= 0 AND unrecoverable >= 0",
        name="counts",
    ),
    CheckConstraint("finished_at IS NULL OR started_at <= finished_at", name="finished"),
    CheckConstraint(
        "(first_received IS NULL) = (last_received IS NULL)"
        " AND (first_received IS NULL OR first_received <= last_received)",
        name="received",
    ),
)

# Une table partitionnée naît avec la partition du mois courant et des mois à venir.
for _partitioned in (event_raw, coverage_report, ingest_duplicate):
    event.listen(_partitioned, "after_create", create_partitions_ahead)
