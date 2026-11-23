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
la file de rebut (``dead_letter``), qui les désigne sans les recopier. Ceux
qu'elle a intégrés attendent d'être réémis vers un consommateur OpenLineage
externe dans la file de réémission (``reemission_queue``), quand les workers en
ont un ; ``reemission_stats`` compte ce qui est parti et ce qui s'est perdu.

La quarantaine d'un incident (``incident_quarantine``) est un objet à part,
rattaché à l'incident : une ligne par activation, active puis levée. Ses états
et ses transitions forment une liste fermée que la base tient elle-même.

Chaque changement d'état d'un incident attend d'être annoncé au récepteur de
webhooks dans la file des webhooks (``webhook_queue``), quand un webhook est en
service ; ``webhook_stats`` dit jusqu'à quand il l'est, et compte ce qui est
parti et ce qui s'est perdu.

Un test de qualité en échec, que la projection relève dans un événement
(``quality_signal``), propose un incident (``incident_proposal``) qu'un humain
confirme ou écarte : des identifiants, des classes d'assertions d'une liste
fermée, des heures, jamais le texte libre d'une assertion. ``quality_column``
garde la dernière réussite et le dernier échec connus de chaque colonne testée,
``quality_stats`` compte ce que les bornes ont écarté.

Chaque révélation d'un destinataire est journalisée (``recipient_revelation``), en
ajout seul : son auteur, son heure, son motif d'une liste fermée, le pseudonyme visé
et le résultat, jamais l'identité. Aucune table ne relie un pseudonyme à une personne.

La liste de rappel d'un incident (``recall_list``) tient une entrée par destinataire
(``recall_entry``), rangée par exposition, avec ses réponses touchées
(``recall_entry_answer``) : un instantané de l'analyse, des pseudonymes et des nombres.

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
from sqlalchemy.engine import Connection

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
# Mode d'une quarantaine, d'une liste fermée : ce que les proxys feront des lectures
# de son périmètre. « warn » : les signaler. Un mode nouveau s'ajoute par une
# révision de la base et du contrat.
QuarantineMode = Literal["warn"]
QUARANTINE_MODES: Final[tuple[str, ...]] = get_args(QuarantineMode)
# État d'une quarantaine : active, puis levée. Une quarantaine levée ne revient pas :
# une nouvelle activation est une nouvelle quarantaine.
QuarantineState = Literal["active", "lifted"]
QUARANTINE_STATES: Final[tuple[str, ...]] = get_args(QuarantineState)
# Les transitions admises, d'une liste fermée : la naissance d'une quarantaine
# active, puis sa levée. La base refuse toute autre (``ck_incident_quarantine_transition``).
QUARANTINE_TRANSITIONS: Final[tuple[tuple[str | None, str], ...]] = (
    (None, "active"),
    ("active", "lifted"),
)
# Quarantaines actives au plus, tous incidents confondus : les proxys les reçoivent
# toutes, d'une seule lecture bornée.
MAX_ACTIVE_QUARANTINES: Final = 100
# Événement d'incident annoncé par webhook, d'une liste fermée : un par
# changement d'état qui existe. La déclaration ; l'analyse faite ; l'analyse en
# échec pour de bon ; la quarantaine activée ; la quarantaine levée. Une analyse
# remise à plus tard ne change pas d'état.
WebhookEvent = Literal[
    "incident.declared",
    "incident.analysed",
    "incident.analysis_failed",
    "incident.quarantined",
    "incident.quarantine_lifted",
]
WEBHOOK_EVENTS: Final[tuple[str, ...]] = get_args(WebhookEvent)
# L'état de l'analyse que chaque événement de l'analyse annonce.
WEBHOOK_EVENT_ANALYSIS: Final[dict[str, str]] = {
    "incident.declared": "pending",
    "incident.analysed": "done",
    "incident.analysis_failed": "failed",
}
# L'état de la quarantaine que chaque événement de la quarantaine annonce.
WEBHOOK_EVENT_QUARANTINE: Final[dict[str, str]] = {
    "incident.quarantined": "active",
    "incident.quarantine_lifted": "lifted",
}
# Classe d'une assertion de qualité, d'une liste fermée : le nom que l'émetteur donne
# à son test y est ramené, jamais recopié. « other » pour tout le reste.
AssertionClass = Literal[
    "not_null",
    "unique",
    "accepted_values",
    "relationships",
    "row_count",
    "freshness",
    "custom_sql",
    "other",
]
ASSERTION_CLASSES: Final[tuple[str, ...]] = get_args(AssertionClass)
# Gravité d'une assertion telle que l'émetteur l'a réglée, de la plus faible à la plus
# forte : « unspecified » quand il n'en dit rien ou rien de connu.
QualitySeverity = Literal["unspecified", "warn", "error"]
QUALITY_SEVERITIES: Final[tuple[str, ...]] = get_args(QualitySeverity)
# Sort d'un signal d'échec : en attente d'un worker ; rattaché à une proposition
# ouverte ; mis en sourdine, rattaché à une proposition décidée il y a peu ; refusé,
# la borne des propositions ouvertes atteinte ; trop ancien pour proposer.
SignalState = Literal["pending", "attached", "muted", "refused", "stale"]
SIGNAL_STATES: Final[tuple[str, ...]] = get_args(SignalState)
# État d'une proposition d'incident : signalée, en attente d'un humain ; confirmée,
# l'incident déclaré ; écartée par un humain ; expirée, sans nouvel échec.
ProposalState = Literal["signaled", "confirmed", "dismissed", "expired"]
PROPOSAL_STATES: Final[tuple[str, ...]] = get_args(ProposalState)
# Les transitions admises : une proposition naît signalée, se complète tant qu'elle
# l'est, puis est confirmée, écartée ou expire, définitivement. La base refuse toute
# autre (``ck_incident_proposal_transition``).
PROPOSAL_TRANSITIONS: Final[tuple[tuple[str | None, str], ...]] = (
    (None, "signaled"),
    ("signaled", "signaled"),
    ("signaled", "confirmed"),
    ("signaled", "dismissed"),
    ("signaled", "expired"),
)
# Ce que dit le début de la fenêtre déduite : la dernière réussite connue avant le
# premier échec, ou, à défaut, un repli fixe avant lui.
WindowBasis = Literal["last_pass", "lookback"]
WINDOW_BASES: Final[tuple[str, ...]] = get_args(WindowBasis)
# Qui compte : la projection des événements, ou le pas qui fait les propositions.
QualityStage = Literal["projection", "proposals"]
QUALITY_STAGES: Final[tuple[str, ...]] = get_args(QualityStage)
# Propositions ouvertes au plus, tous jeux confondus ; colonnes nommées au plus par
# proposition, au-delà desquelles elle porte sur la table entière.
MAX_OPEN_PROPOSALS: Final = 100
MAX_PROPOSAL_COLUMNS: Final = 100
# Classes d'assertions au plus par signal : toute la liste fermée.
MAX_SIGNAL_CLASSES: Final = len(ASSERTION_CLASSES)
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

# Motif de la révélation d'un destinataire, d'une liste fermée, jamais un texte libre :
# prévenir le destinataire d'un rappel ; répondre à une demande de la personne
# concernée ; répondre à une obligation légale ; instruire un incident.
RevealReason = Literal[
    "recall",
    "data_subject_request",
    "legal_obligation",
    "incident_investigation",
]
REVEAL_REASONS: Final[tuple[str, ...]] = get_args(RevealReason)
# Ce qu'a donné une révélation : une identité candidate a ce pseudonyme ; aucune ;
# aucun destinataire connu ne porte ce pseudonyme.
RevealResult = Literal["matched", "no_match", "unknown_recipient"]
REVEAL_RESULTS: Final[tuple[str, ...]] = get_args(RevealResult)
# Identités candidates au plus par révélation.
MAX_REVEAL_CANDIDATES: Final = 100
# La forme d'un pseudonyme, que la base tient pour le journal des révélations.
PSEUDONYM_FORM: Final = "^hmac-sha256:[0-9a-f]{64}$"
# Bornes de l'auteur d'une révélation : l'émetteur OIDC, tel qu'il est réglé, et le
# sujet de son jeton, qu'OpenID Connect borne à 255 caractères.
AUTHOR_ISSUER_MAX: Final = 1024
AUTHOR_SUBJECT_MAX: Final = 255

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
    # Vrai quand l'événement n'est jamais entré au graphe : projeté, il part vers la
    # destination de la réémission, si les workers en ont une. Faux pour un événement
    # que la reconstruction du graphe remet en file : il a déjà été projeté.
    Column("reemit", Boolean, nullable=False, server_default=text("true")),
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

# File de réémission : les événements projetés, à envoyer au consommateur
# OpenLineage externe que règlent les workers. Un worker y met un événement dans
# la transaction qui le projette et le retire de la file de traitement ; un autre
# l'en retire dans celle qui enregistre l'issue de son envoi. Comme la file, elle
# désigne l'événement brut sans le recopier, sans clé étrangère, et la purge
# retire les événements du mois qu'elle supprime. Bornée en âge et en nombre par
# les workers : ce qui déborde est compté dans ``reemission_stats``.
reemission_queue = Table(
    "reemission_queue",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("event_id", Text, nullable=False),
    Column("recorded_month", Date, nullable=False),
    Column("enqueued_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # Envois en échec : la destination n'a pas dit si elle avait pris l'événement.
    Column("attempts", Integer, nullable=False, server_default="0"),
    # Classe de la dernière erreur, ou statut de la réponse : jamais un message.
    Column("last_error", Text),
    UniqueConstraint("event_id"),
    CheckConstraint("attempts >= 0", name="attempts"),
    CheckConstraint("(attempts = 0) = (last_error IS NULL)", name="last_error"),
    # Les plus anciens : l'âge de la file, et ce qui dépasse sa borne d'âge.
    Index(None, "enqueued_at"),
)
# Les événements en reprise, que la route de santé compte.
Index(
    "ix_reemission_queue_retrying",
    reemission_queue.c.id,
    postgresql_where=reemission_queue.c.attempts > 0,
)

# Compteurs de la réémission depuis la mise en service, en une seule ligne :
# événements remis à la destination, refusés par elle, abandonnés par la borne
# de la file ou par la purge. Ils grandissent dans la transaction qui retire les
# événements de la file : la file et ses compteurs s'accordent.
reemission_stats = Table(
    "reemission_stats",
    metadata,
    Column("id", Boolean, primary_key=True, server_default=text("true")),
    Column("sent", BigInteger, nullable=False, server_default="0"),
    Column("rejected", BigInteger, nullable=False, server_default="0"),
    Column("dropped", BigInteger, nullable=False, server_default="0"),
    CheckConstraint("id", name="single_row"),
    CheckConstraint("sent >= 0 AND rejected >= 0 AND dropped >= 0", name="counts"),
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

# Quarantaine d'un incident : une ligne par activation, rattachée à l'incident, sans
# texte libre. Son périmètre est celui de l'incident (``incident_dataset``), que rien
# ne modifie. Elle naît active, avec son mode, à l'heure de la base ; sa levée pose
# l'heure de la levée, et la ligne ne change plus : une nouvelle activation est une
# nouvelle ligne. Au plus une quarantaine active par incident, et
# ``MAX_ACTIVE_QUARANTINES`` en tout, que l'activation vérifie sous verrou.
incident_quarantine = Table(
    "incident_quarantine",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("incident_id", Uuid, ForeignKey("incident.id"), nullable=False),
    Column("mode", Text, nullable=False),
    Column("state", Text, nullable=False, server_default=text("'active'")),
    Column("activated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("lifted_at", DateTime(timezone=True)),
    CheckConstraint(_in("mode", QUARANTINE_MODES), name="mode"),
    CheckConstraint(_in("state", QUARANTINE_STATES), name="state"),
    CheckConstraint("(state = 'lifted') = (lifted_at IS NOT NULL)", name="lifted"),
    CheckConstraint("lifted_at IS NULL OR activated_at <= lifted_at", name="lifted_after"),
    # Les quarantaines d'un incident, la dernière d'abord.
    Index(None, "incident_id", "id"),
)
# Une quarantaine active au plus par incident : deux activations concurrentes n'en
# font qu'une.
Index(
    "uq_incident_quarantine_active",
    incident_quarantine.c.incident_id,
    unique=True,
    postgresql_where=incident_quarantine.c.state == "active",
)


def _admitted_transitions() -> str:
    """Les transitions admises, en PL/pgSQL, tirées de ``QUARANTINE_TRANSITIONS``."""
    admitted = []
    for before, after in QUARANTINE_TRANSITIONS:
        if before is None:
            admitted.append(f"(TG_OP = 'INSERT' AND NEW.state = '{after}')")
        else:
            admitted.append(
                f"(TG_OP = 'UPDATE' AND OLD.state = '{before}' AND NEW.state = '{after}'"
                " AND NEW.incident_id = OLD.incident_id AND NEW.mode = OLD.mode"
                " AND NEW.activated_at = OLD.activated_at)"
            )
    return "\n        OR ".join(admitted)


# Les transitions d'une quarantaine, tenues par une contrainte nommée que la base
# vérifie à chaque écriture : une ligne naît active ; seule une quarantaine active se
# lève, sans changer d'incident, de mode ni d'heure d'activation ; une levée est
# définitive. La fonction et la contrainte sont créées avec la table.
QUARANTINE_TRANSITION_FUNCTION: Final = f"""
CREATE FUNCTION incident_quarantine_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF {_admitted_transitions()} THEN
        RETURN NULL;
    END IF;
    RAISE EXCEPTION 'transition de quarantaine refusée'
        USING ERRCODE = 'check_violation',
              CONSTRAINT = 'ck_incident_quarantine_transition',
              TABLE = 'incident_quarantine';
END
$$
"""
QUARANTINE_TRANSITION_CONSTRAINT: Final = """
CREATE CONSTRAINT TRIGGER ck_incident_quarantine_transition
AFTER INSERT OR UPDATE ON incident_quarantine
FOR EACH ROW EXECUTE FUNCTION incident_quarantine_transition()
"""


def create_quarantine_transitions(target: Table, connection: Connection, **_: object) -> None:
    """Crée, avec la table, la fonction et la contrainte qui tiennent ses transitions."""
    connection.exec_driver_sql(QUARANTINE_TRANSITION_FUNCTION)
    connection.exec_driver_sql(QUARANTINE_TRANSITION_CONSTRAINT)


event.listen(incident_quarantine, "after_create", create_quarantine_transitions)

# File des webhooks : chaque changement d'état d'un incident, à annoncer au
# récepteur que règlent les workers. Le message y entre dans la transaction qui
# change l'état, quand un webhook est en service, et en sort dans celle qui
# enregistre l'issue de son envoi. Il y est tel qu'il partira, figé à l'heure du
# changement : des identifiants, des codes de listes fermées, des heures et des
# décomptes, jamais un texte libre ni un pseudonyme. La quarantaine y est celle de
# l'incident à cette heure : la dernière, active ou levée, ou aucune. ``delivery_id`` reste le
# même d'un envoi à l'autre : le récepteur s'en sert pour écarter un doublon.
# Bornée en âge et en nombre par les workers : ce qui déborde est compté dans
# ``webhook_stats``.
webhook_queue = Table(
    "webhook_queue",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("delivery_id", Uuid, nullable=False, server_default=func.gen_random_uuid()),
    Column("event_type", Text, nullable=False),
    Column("incident_id", Uuid, ForeignKey("incident.id"), nullable=False),
    # L'heure du changement d'état : la déclaration, ou la fin de l'analyse.
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("incident_state", Text, nullable=False),
    Column("severity", Text, nullable=False),
    Column("analysis_state", Text, nullable=False),
    Column("analysis_error", Text),
    # Les décomptes d'une analyse faite : réponses touchées, destinataires distincts.
    Column("answers", BigInteger),
    Column("recipients", BigInteger),
    Column("lower_bound", Boolean),
    Column("enqueued_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # Envois en échec : le récepteur n'a pas dit s'il avait pris le message.
    Column("attempts", Integer, nullable=False, server_default="0"),
    # Classe de la dernière erreur, ou statut de la réponse : jamais un message.
    Column("last_error", Text),
    # La dernière quarantaine de l'incident à l'heure du changement : son état et son
    # mode, ou rien quand l'incident n'en a jamais eu.
    Column("quarantine_state", Text),
    Column("quarantine_mode", Text),
    UniqueConstraint("delivery_id"),
    CheckConstraint(_in("event_type", WEBHOOK_EVENTS), name="event_type"),
    CheckConstraint(
        " OR ".join(
            [
                *(
                    f"(event_type = '{kind}' AND analysis_state = '{state}')"
                    for kind, state in WEBHOOK_EVENT_ANALYSIS.items()
                ),
                _in("event_type", tuple(WEBHOOK_EVENT_QUARANTINE)),
            ]
        ),
        name="analysis_state",
    ),
    CheckConstraint(
        " AND ".join(
            f"(event_type <> '{kind}' OR quarantine_state = '{state}')"
            for kind, state in WEBHOOK_EVENT_QUARANTINE.items()
        ),
        name="quarantine_event",
    ),
    CheckConstraint(
        f"quarantine_state IS NULL OR {_in('quarantine_state', QUARANTINE_STATES)}",
        name="quarantine_state",
    ),
    CheckConstraint(
        f"quarantine_mode IS NULL OR {_in('quarantine_mode', QUARANTINE_MODES)}",
        name="quarantine_mode",
    ),
    CheckConstraint(
        "(quarantine_state IS NULL) = (quarantine_mode IS NULL)", name="quarantine_given"
    ),
    CheckConstraint(_in("incident_state", INCIDENT_STATES), name="incident_state"),
    CheckConstraint(_in("severity", SEVERITIES), name="severity"),
    CheckConstraint(
        f"analysis_error IS NULL OR {_in('analysis_error', ANALYSIS_ERRORS)}",
        name="analysis_error",
    ),
    CheckConstraint("(analysis_state = 'failed') = (analysis_error IS NOT NULL)", name="failure"),
    CheckConstraint(
        "(analysis_state = 'done') = (answers IS NOT NULL AND recipients IS NOT NULL"
        " AND lower_bound IS NOT NULL)",
        name="counts_given",
    ),
    CheckConstraint("answers >= 0 AND recipients >= 0 AND attempts >= 0", name="counts"),
    CheckConstraint("(attempts = 0) = (last_error IS NULL)", name="last_error"),
    # Les plus anciens : l'âge de la file, et ce qui dépasse sa borne d'âge.
    Index(None, "enqueued_at"),
)
# Les messages en reprise, que la route de santé compte.
Index(
    "ix_webhook_queue_retrying",
    webhook_queue.c.id,
    postgresql_where=webhook_queue.c.attempts > 0,
)

# État et compteurs des webhooks, en une seule ligne. ``in_service_until`` : un
# worker qui a un webhook le repousse à chaque passage, de la borne d'âge de sa
# file ; tant qu'elle n'est pas passée, un changement d'état met son message en
# file. Les compteurs, depuis la mise en service : messages remis au récepteur,
# refusés par lui, abandonnés par la borne de la file. Ils grandissent dans la
# transaction qui retire les messages de la file.
webhook_stats = Table(
    "webhook_stats",
    metadata,
    Column("id", Boolean, primary_key=True, server_default=text("true")),
    Column("in_service_until", DateTime(timezone=True)),
    Column("sent", BigInteger, nullable=False, server_default="0"),
    Column("rejected", BigInteger, nullable=False, server_default="0"),
    Column("dropped", BigInteger, nullable=False, server_default="0"),
    CheckConstraint("id", name="single_row"),
    CheckConstraint("sent >= 0 AND rejected >= 0 AND dropped >= 0", name="counts"),
)


def _classes(column: str) -> str:
    """Une liste de classes d'assertions : de une à toutes, prises dans la liste fermée."""
    admitted = ", ".join(repr(value) for value in ASSERTION_CLASSES)
    return (
        f"cardinality({column}) BETWEEN 1 AND {MAX_SIGNAL_CLASSES}"
        f" AND {column} <@ ARRAY[{admitted}]::text[]"
    )


# Proposition d'incident, née d'un test de qualité en échec : un jeu de données, ses
# colonnes en échec (``incident_proposal_column``) ou la table entière, les classes
# d'assertions et la plus forte gravité vues, les heures du premier et du dernier
# échec, le début de la fenêtre déduite et ce qui le fonde, l'émetteur. Aucun texte
# libre. Elle attend un humain : confirmée, elle devient un incident déclaré
# (``incident_id``) ; écartée ou expirée, elle n'a aucun effet. Au plus une
# proposition signalée par jeu, et ``MAX_OPEN_PROPOSALS`` en tout, que les workers
# vérifient sous verrou. ``signaled_at`` : le dernier échec rattaché, par l'heure de
# la base, d'où part l'expiration.
incident_proposal = Table(
    "incident_proposal",
    metadata,
    Column("id", Uuid, primary_key=True, server_default=func.gen_random_uuid()),
    Column("state", Text, nullable=False, server_default=text("'signaled'")),
    Column("namespace", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("whole_table", Boolean, nullable=False),
    Column("classes", ARRAY(Text), nullable=False),
    Column("severity", Text, nullable=False),
    Column("first_failed_at", DateTime(timezone=True), nullable=False),
    Column("last_failed_at", DateTime(timezone=True), nullable=False),
    Column("window_start", DateTime(timezone=True), nullable=False),
    Column("window_basis", Text, nullable=False),
    Column("emitter", Text, nullable=False),
    Column("proposed_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("signaled_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("decided_at", DateTime(timezone=True)),
    Column("incident_id", Uuid, ForeignKey("incident.id")),
    UniqueConstraint("incident_id"),
    CheckConstraint(_in("state", PROPOSAL_STATES), name="state"),
    CheckConstraint(_in("severity", QUALITY_SEVERITIES), name="severity"),
    CheckConstraint(_in("window_basis", WINDOW_BASES), name="window_basis"),
    CheckConstraint(_classes("classes"), name="classes"),
    CheckConstraint("first_failed_at <= last_failed_at", name="failed"),
    CheckConstraint("window_start < first_failed_at", name="window"),
    CheckConstraint("(state = 'signaled') = (decided_at IS NULL)", name="decided"),
    CheckConstraint("decided_at IS NULL OR proposed_at <= decided_at", name="decided_after"),
    CheckConstraint("(state = 'confirmed') = (incident_id IS NOT NULL)", name="confirmed"),
    # Les propositions les plus récentes d'abord, pour la liste.
    Index(None, "proposed_at", "id"),
    # Les décisions récentes d'un jeu, pour la sourdine.
    Index(None, "namespace", "name", "decided_at"),
)
# Une proposition signalée au plus par jeu de données : deux workers n'en font qu'une.
Index(
    "uq_incident_proposal_open",
    incident_proposal.c.namespace,
    incident_proposal.c.name,
    unique=True,
    postgresql_where=incident_proposal.c.state == "signaled",
)


def _admitted_proposal_transitions() -> str:
    """Les transitions admises, en PL/pgSQL, tirées de ``PROPOSAL_TRANSITIONS``."""
    admitted = []
    for before, after in PROPOSAL_TRANSITIONS:
        if before is None:
            admitted.append(f"(TG_OP = 'INSERT' AND NEW.state = '{after}')")
        else:
            admitted.append(
                f"(TG_OP = 'UPDATE' AND OLD.state = '{before}' AND NEW.state = '{after}'"
                " AND NEW.namespace = OLD.namespace AND NEW.name = OLD.name"
                " AND NEW.proposed_at = OLD.proposed_at"
                " AND NEW.first_failed_at = OLD.first_failed_at"
                " AND NEW.window_start = OLD.window_start)"
            )
    return "\n        OR ".join(admitted)


# Les transitions d'une proposition, tenues par une contrainte nommée que la base
# vérifie à chaque écriture : une ligne naît signalée ; seule une proposition signalée
# change, sans changer de jeu, d'heure de proposition, de premier échec ni de début de
# fenêtre ; une décision est définitive. La fonction et la contrainte sont créées avec
# la table.
PROPOSAL_TRANSITION_FUNCTION: Final = f"""
CREATE FUNCTION incident_proposal_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF {_admitted_proposal_transitions()} THEN
        RETURN NULL;
    END IF;
    RAISE EXCEPTION 'transition de proposition refusée'
        USING ERRCODE = 'check_violation',
              CONSTRAINT = 'ck_incident_proposal_transition',
              TABLE = 'incident_proposal';
END
$$
"""
PROPOSAL_TRANSITION_CONSTRAINT: Final = """
CREATE CONSTRAINT TRIGGER ck_incident_proposal_transition
AFTER INSERT OR UPDATE ON incident_proposal
FOR EACH ROW EXECUTE FUNCTION incident_proposal_transition()
"""


def create_proposal_transitions(target: Table, connection: Connection, **_: object) -> None:
    """Crée, avec la table, la fonction et la contrainte qui tiennent ses transitions."""
    connection.exec_driver_sql(PROPOSAL_TRANSITION_FUNCTION)
    connection.exec_driver_sql(PROPOSAL_TRANSITION_CONSTRAINT)


event.listen(incident_proposal, "after_create", create_proposal_transitions)

# Les colonnes en échec d'une proposition, nommées, ``MAX_PROPOSAL_COLUMNS`` au plus.
# Une assertion sur la table entière, ou sur une colonne qui n'a pas la forme d'un
# nom, ne nomme rien : la proposition porte alors sur la table (``whole_table``).
incident_proposal_column = Table(
    "incident_proposal_column",
    metadata,
    Column("proposal_id", Uuid, ForeignKey("incident_proposal.id"), nullable=False),
    Column("column_name", Text, nullable=False),
    PrimaryKeyConstraint("proposal_id", "column_name"),
    CheckConstraint("column_name <> ''", name="named"),
)

# Signal d'échec d'un test de qualité, relevé par la projection d'un événement : un
# par run, jeu de données et colonne (``''`` : la table). Les classes d'assertions et
# la plus forte gravité réunissent les assertions en échec du run sur cette colonne.
# L'écriture est idempotente et commutative : rejouer l'événement, ou recevoir ceux
# d'un run dans le désordre, ne relève rien deux fois. Un worker réclame les signaux
# en attente (``FOR UPDATE SKIP LOCKED``) pour en faire des propositions. Hors du
# graphe : une reconstruction ne les vide pas. Purgés après leur borne d'âge.
quality_signal = Table(
    "quality_signal",
    metadata,
    Column("run_id", Uuid, nullable=False),
    Column("namespace", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("column_name", Text, nullable=False),
    Column("classes", ARRAY(Text), nullable=False),
    Column("severity", Text, nullable=False),
    Column("emitter", Text, nullable=False),
    Column("event_time", DateTime(timezone=True), nullable=False),
    # La réception de l'événement par l'API (``event_raw.recorded_at``).
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    Column("state", Text, nullable=False, server_default=text("'pending'")),
    Column("proposal_id", Uuid, ForeignKey("incident_proposal.id")),
    PrimaryKeyConstraint("run_id", "namespace", "name", "column_name"),
    CheckConstraint(_classes("classes"), name="classes"),
    CheckConstraint(_in("severity", QUALITY_SEVERITIES), name="severity"),
    CheckConstraint(_in("state", SIGNAL_STATES), name="state"),
    CheckConstraint(
        "(state IN ('attached', 'muted')) = (proposal_id IS NOT NULL)", name="proposal"
    ),
    # Les plus anciens d'abord, pour la purge.
    Index(None, "recorded_at"),
)
# Les signaux en attente, les plus anciens d'abord : ce que les workers réclament.
Index(
    "ix_quality_signal_pending",
    quality_signal.c.event_time,
    quality_signal.c.run_id,
    postgresql_where=quality_signal.c.state == "pending",
)
# Les signaux d'une proposition, pour compter ses runs en échec.
Index(
    "ix_quality_signal_proposal_id",
    quality_signal.c.proposal_id,
    quality_signal.c.run_id,
    postgresql_where=quality_signal.c.proposal_id.is_not(None),
)

# Dernière réussite et dernier échec connus des assertions de chaque colonne d'un jeu
# de données (``''`` : la table), par l'heure des événements. Écrite au plus grand :
# l'ordre d'arrivée ne compte pas. Elle fonde le début de la fenêtre déduite d'une
# proposition, et dit quand toutes ses colonnes sont de nouveau en réussite.
quality_column = Table(
    "quality_column",
    metadata,
    Column("namespace", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("column_name", Text, nullable=False),
    Column("last_passed_at", DateTime(timezone=True)),
    Column("last_failed_at", DateTime(timezone=True)),
    PrimaryKeyConstraint("namespace", "name", "column_name"),
    CheckConstraint("last_passed_at IS NOT NULL OR last_failed_at IS NOT NULL", name="known"),
)

# Compteurs des signaux de qualité depuis la mise en service, une ligne par étape :
# celle de la projection, celle des propositions. Deux lignes, pour que la projection
# et les propositions ne s'attendent pas l'une l'autre. Signaux perdus par une borne,
# refusés par la borne des propositions ouvertes, trop anciens pour proposer ;
# colonnes qui n'ont pas la forme d'un nom ; assertions non lues au-delà de leur
# borne. ``refused_since`` : le début de l'épisode de refus en cours, dit une fois au
# journal.
quality_stats = Table(
    "quality_stats",
    metadata,
    Column("stage", Text, primary_key=True),
    Column("dropped", BigInteger, nullable=False, server_default="0"),
    Column("refused", BigInteger, nullable=False, server_default="0"),
    Column("stale", BigInteger, nullable=False, server_default="0"),
    Column("invalid_columns", BigInteger, nullable=False, server_default="0"),
    Column("assertions_truncated", BigInteger, nullable=False, server_default="0"),
    Column("refused_since", DateTime(timezone=True)),
    CheckConstraint(_in("stage", QUALITY_STAGES), name="stage"),
    CheckConstraint(
        "dropped >= 0 AND refused >= 0 AND stale >= 0 AND invalid_columns >= 0"
        " AND assertions_truncated >= 0",
        name="counts",
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

# Journal des révélations de destinataires, en ajout seul : qui a révélé (l'émetteur
# OIDC et le sujet de son jeton), quand (l'heure de la base), pourquoi (un motif de la
# liste fermée), quel pseudonyme, avec combien d'identités candidates, et ce qu'il en
# est sorti. Jamais l'identité révélée ni les candidates : aucune ligne ne relie un
# pseudonyme à une personne. Le pseudonyme est une valeur, sans clé étrangère : le
# journal survit à la reconstruction du graphe, qui vide ``recipient``.
#
# La base tient l'ajout seul : l'heure est posée par elle à l'insertion, quoi que
# l'insertion porte ; une modification, une suppression ou une troncature est refusée
# comme une contrainte (``ck_recipient_revelation_append_only``), même pour le
# propriétaire de la table, à qui ces droits sont aussi retirés.
recipient_revelation = Table(
    "recipient_revelation",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("revealed_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("author_issuer", Text, nullable=False),
    Column("author_subject", Text, nullable=False),
    Column("reason", Text, nullable=False),
    Column("pseudonym", Text, nullable=False),
    # Le nombre d'identités candidates, jamais les candidates.
    Column("candidates", Integer, nullable=False),
    Column("result", Text, nullable=False),
    CheckConstraint(_in("reason", REVEAL_REASONS), name="reason"),
    CheckConstraint(_in("result", REVEAL_RESULTS), name="result"),
    CheckConstraint(f"pseudonym ~ '{PSEUDONYM_FORM}'", name="pseudonym"),
    CheckConstraint(f"candidates BETWEEN 1 AND {MAX_REVEAL_CANDIDATES}", name="candidates"),
    CheckConstraint(
        f"char_length(author_issuer) BETWEEN 1 AND {AUTHOR_ISSUER_MAX}"
        f" AND char_length(author_subject) BETWEEN 1 AND {AUTHOR_SUBJECT_MAX}",
        name="author",
    ),
    # Les révélations d'un auteur dans l'heure : ce que compte la borne de débit.
    Index("ix_recipient_revelation_author", "author_issuer", "author_subject", "revealed_at"),
)

REVELATION_APPEND_ONLY_FUNCTION: Final = """
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
REVELATION_APPEND_ONLY_TRIGGERS: Final = (
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
    # Même le propriétaire de la table, celui de l'API dans la démonstration, n'a plus le
    # droit de modifier, supprimer ni tronquer : il lui faudrait se le rendre d'abord.
    "REVOKE UPDATE, DELETE, TRUNCATE ON TABLE recipient_revelation FROM PUBLIC, CURRENT_USER",
)


def create_revelation_append_only(target: Table, connection: Connection, **_: object) -> None:
    """Crée, avec la table, ce qui la tient en ajout seul : la fonction, ses déclencheurs, et
    le retrait des droits de modifier."""
    connection.exec_driver_sql(REVELATION_APPEND_ONLY_FUNCTION)
    for statement in REVELATION_APPEND_ONLY_TRIGGERS:
        connection.exec_driver_sql(statement)


event.listen(recipient_revelation, "after_create", create_revelation_append_only)

# Liste de rappel d'un incident : un message par destinataire et par incident. Les workers
# la font de l'analyse d'impact, dans la transaction qui enregistre une analyse faite et non
# tronquée, et la refont quand l'analyse est rejouée. Elle tient ses propres réponses : un
# instantané de l'analyse, des pseudonymes, des identifiants et des nombres, sans texte libre.
# ``reference_at`` est l'heure à laquelle l'exposition des destinataires est calculée :
# celle du graphe lu par l'analyse.
recall_list = Table(
    "recall_list",
    metadata,
    Column("incident_id", Uuid, ForeignKey("incident.id"), primary_key=True),
    Column("built_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("reference_at", DateTime(timezone=True), nullable=False),
    # Destinataires de la liste, et réponses distinctes qu'ils ont reçues.
    Column("recipients", BigInteger, nullable=False),
    Column("answers", BigInteger, nullable=False),
    CheckConstraint("recipients >= 0 AND answers >= 0", name="counts"),
)

# Une entrée de la liste : un destinataire, par son pseudonyme, une fois par incident. Son
# rang suit son exposition (``exposure``, en millionièmes), puis son nombre de réponses
# touchées, sa réponse la plus récente et son pseudonyme : un ordre total.
recall_entry = Table(
    "recall_entry",
    metadata,
    Column("incident_id", Uuid, ForeignKey("recall_list.incident_id"), nullable=False),
    Column("pseudonym", Text, nullable=False),
    Column("rank", Integer, nullable=False),
    Column("exposure", BigInteger, nullable=False),
    Column("answers", Integer, nullable=False),
    Column("latest_answer_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint("incident_id", "pseudonym"),
    UniqueConstraint("incident_id", "rank"),
    CheckConstraint("rank >= 1 AND exposure >= 0 AND answers >= 1", name="counts"),
)

# Les réponses touchées d'un destinataire, désignées comme dans l'analyse : run et empreinte.
recall_entry_answer = Table(
    "recall_entry_answer",
    metadata,
    Column("incident_id", Uuid, nullable=False),
    Column("pseudonym", Text, nullable=False),
    Column("run_id", Uuid, nullable=False),
    Column("fingerprint", Text, nullable=False),
    Column("produced_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint("incident_id", "pseudonym", "run_id", "fingerprint"),
    ForeignKeyConstraint(
        ["incident_id", "pseudonym"], [recall_entry.c.incident_id, recall_entry.c.pseudonym]
    ),
)

# Une table partitionnée naît avec la partition du mois courant et des mois à venir.
for _partitioned in (event_raw, coverage_report, ingest_duplicate):
    event.listen(_partitioned, "after_create", create_partitions_ahead)
