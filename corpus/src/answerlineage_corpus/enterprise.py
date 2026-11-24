"""Entreprise synthétique à vérité connue.

``generate(seed)`` invente une entreprise : ses jeux de données, ses outils,
ses agents, deux semaines de demandes et quelques incidents de données. Tout
découle de la graine : deux générations avec la même graine sont identiques,
quelle que soit la version de Python.

La vérité d'un incident ne passe pas par AnswerLineage. Le générateur sait
quelles tables chaque demande a lues, et à quelle heure : il en déduit
lui-même la liste exacte des réponses touchées. C'est à cette liste que se
mesure le moteur d'impact.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

# Premier jour d'activité : un lundi, fixé pour que le jeu ne dépende pas du
# jour où il est généré.
FIRST_DAY = datetime(2026, 11, 2, tzinfo=UTC)
OPENING_HOUR = 8
WORKING_MINUTES = 10 * 60

DEFAULT_REQUESTS = 240
DEFAULT_DAYS = 10
DEFAULT_INCIDENTS = 6


@dataclass(frozen=True)
class Table:
    namespace: str
    name: str
    columns: tuple[str, ...]
    # Une table d'entrepôt est rechargée chaque nuit : sa version est le jour lu.
    versioned: bool = False


@dataclass(frozen=True)
class Tool:
    server: str
    name: str
    # Un outil SQL lit les tables et les colonnes que la requête choisit ; un
    # autre outil lit toujours les mêmes tables, en entier.
    sql: bool = False


@dataclass(frozen=True)
class Agent:
    namespace: str
    name: str
    channel: str
    # Par outil, les tables que l'agent lit à travers lui.
    access: tuple[tuple[Tool, tuple[Table, ...]], ...]
    # Pseudonymes des personnes qui reçoivent ses réponses.
    audience: tuple[str, ...]


@dataclass(frozen=True)
class TableRead:
    table: Table
    # Colonnes lues ; vide quand la table est lue en entier.
    columns: tuple[str, ...] = ()
    version: str | None = None


@dataclass(frozen=True)
class ToolCall:
    run_id: UUID
    tool: Tool
    time: datetime
    reads: tuple[TableRead, ...]


@dataclass(frozen=True)
class Answer:
    # Empreinte de la réponse : son contenu n'existe pas.
    fingerprint: str
    channel: str
    message_ref: str
    recipients: tuple[str, ...]
    time: datetime


@dataclass(frozen=True)
class Request:
    """Une demande faite à un agent : ses appels d'outils, puis sa réponse."""

    run_id: UUID
    agent: Agent
    started_at: datetime
    calls: tuple[ToolCall, ...]
    # Absente quand le run a échoué avant de répondre.
    answer: Answer | None

    @property
    def ended_at(self) -> datetime:
        return self.answer.time if self.answer is not None else self.calls[-1].time


@dataclass(frozen=True)
class Incident:
    """Une table reconnue fausse pendant une fenêtre : ``start`` inclus, ``end`` exclu."""

    name: str
    table: Table
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Enterprise:
    seed: int
    tables: tuple[Table, ...]
    tools: tuple[Tool, ...]
    agents: tuple[Agent, ...]
    recipients: tuple[str, ...]
    requests: tuple[Request, ...]
    incidents: tuple[Incident, ...]

    def touched(self, incident: Incident) -> tuple[Request, ...]:
        """Demandes dont la réponse dépend de la table fausse, dans l'ordre du temps.

        Une réponse est touchée quand un appel de sa demande a lu la table
        pendant la fenêtre. Une demande restée sans réponse n'a rien à rappeler.
        """
        return tuple(
            request
            for request in self.requests
            if request.answer is not None and _read_during(request, incident)
        )


def _read_during(request: Request, incident: Incident) -> bool:
    return any(
        read.table == incident.table and incident.start <= call.time < incident.end
        for call in request.calls
        for read in call.reads
    )


class _Dice:
    """Tirages nommés : chacun ne dépend que de la graine et de son étiquette.

    Ajouter un tirage ne décale donc aucun autre, et le résultat ne dépend pas
    de l'implémentation de ``random``, qui change d'une version de Python à
    l'autre.
    """

    def __init__(self, seed: int) -> None:
        self._seed = seed

    def _digest(self, label: tuple[object, ...]) -> bytes:
        text = "/".join(str(part) for part in (self._seed, *label))
        return hashlib.sha256(text.encode()).digest()

    def bits(self, count: int, *label: object) -> int:
        return int.from_bytes(self._digest(label)) >> (256 - count)

    def below(self, bound: int, *label: object) -> int:
        return self.bits(64, *label) % bound

    def hex(self, length: int, *label: object) -> str:
        return self._digest(label).hex()[:length]

    def pick[T](self, items: Sequence[T], *label: object) -> T:
        return items[self.below(len(items), *label)]

    def sample[T](self, items: Sequence[T], count: int, *label: object) -> tuple[T, ...]:
        remaining = list(items)
        chosen = [
            remaining.pop(self.below(len(remaining), *label, rank))
            for rank in range(min(count, len(remaining)))
        ]
        return tuple(chosen)


_WAREHOUSE = "bigquery"
_CRM = "postgres://crm:5432"
_SUPPORT = "postgres://support:5432"


def _warehouse(name: str, *columns: str) -> Table:
    return Table(_WAREHOUSE, name, columns, versioned=True)


_EXCHANGE_RATES = _warehouse("finance.ref.taux_change", "devise", "taux", "date_valeur")
_INVOICES = _warehouse(
    "finance.compta.factures", "facture_id", "client_id", "montant", "devise", "echeance", "statut"
)
_PAYMENTS = _warehouse(
    "finance.compta.encaissements", "encaissement_id", "facture_id", "montant", "date_encaissement"
)
_ORDERS = _warehouse(
    "ventes.suivi.commandes",
    "commande_id",
    "client_id",
    "montant",
    "devise",
    "date_commande",
    "statut",
)
_CUSTOMERS = _warehouse("ventes.suivi.clients", "client_id", "segment", "pays", "date_creation")
_TARGETS = _warehouse("ventes.pilotage.objectifs", "equipe", "trimestre", "objectif")
_STOCKS = _warehouse(
    "logistique.flux.stocks", "article_id", "entrepot", "quantite", "date_inventaire"
)
_SHIPMENTS = _warehouse(
    "logistique.flux.expeditions",
    "expedition_id",
    "commande_id",
    "transporteur",
    "date_livraison",
    "statut",
)
_CATALOGUE = _warehouse("produit.ref.catalogue", "article_id", "libelle", "prix", "categorie")
_HEADCOUNT = _warehouse("rh.pilotage.effectifs", "equipe", "mois", "effectif")
_ACCOUNTS = Table(_CRM, "crm.public.comptes", ("compte_id", "segment", "pays", "responsable"))
_OPPORTUNITIES = Table(
    _CRM,
    "crm.public.opportunites",
    ("opportunite_id", "compte_id", "montant", "etape", "date_cloture"),
)
_TICKETS = Table(
    _SUPPORT,
    "support.public.tickets",
    ("ticket_id", "compte_id", "priorite", "statut", "date_ouverture"),
)

TABLES: tuple[Table, ...] = (
    _EXCHANGE_RATES,
    _INVOICES,
    _PAYMENTS,
    _ORDERS,
    _CUSTOMERS,
    _TARGETS,
    _STOCKS,
    _SHIPMENTS,
    _CATALOGUE,
    _HEADCOUNT,
    _ACCOUNTS,
    _OPPORTUNITIES,
    _TICKETS,
)

_SQL = Tool("entrepot", "execute_sql", sql=True)
_FIND_ACCOUNTS = Tool("crm", "rechercher_comptes")
_LIST_OPPORTUNITIES = Tool("crm", "lister_opportunites")
_LIST_TICKETS = Tool("support", "lister_tickets")

TOOLS: tuple[Tool, ...] = (_SQL, _FIND_ACCOUNTS, _LIST_OPPORTUNITIES, _LIST_TICKETS)


@dataclass(frozen=True)
class _Profile:
    namespace: str
    name: str
    channel: str
    # Part des demandes de l'entreprise, sur cent.
    share: int
    audience: int
    access: tuple[tuple[Tool, tuple[Table, ...]], ...]


_PROFILES: tuple[_Profile, ...] = (
    _Profile(
        "agent://ventes",
        "assistant-commercial",
        "slack",
        share=30,
        audience=10,
        access=(
            (_SQL, (_ORDERS, _CUSTOMERS, _TARGETS, _CATALOGUE)),
            (_FIND_ACCOUNTS, (_ACCOUNTS,)),
            (_LIST_OPPORTUNITIES, (_OPPORTUNITIES, _ACCOUNTS)),
        ),
    ),
    _Profile(
        "agent://support",
        "assistant-support",
        "teams",
        share=30,
        audience=8,
        access=(
            (_LIST_TICKETS, (_TICKETS,)),
            (_FIND_ACCOUNTS, (_ACCOUNTS,)),
            (_SQL, (_SHIPMENTS, _ORDERS)),
        ),
    ),
    _Profile(
        "agent://finance",
        "analyste-tresorerie",
        "email",
        share=20,
        audience=6,
        access=((_SQL, (_EXCHANGE_RATES, _INVOICES, _PAYMENTS, _ORDERS)),),
    ),
    _Profile(
        "agent://logistique",
        "planificateur-stocks",
        "slack",
        share=12,
        audience=5,
        access=((_SQL, (_STOCKS, _SHIPMENTS, _CATALOGUE, _ORDERS)),),
    ),
    _Profile(
        "agent://direction",
        "synthese-hebdomadaire",
        "email",
        share=8,
        audience=4,
        access=((_SQL, (_INVOICES, _ORDERS, _TARGETS, _HEADCOUNT, _EXCHANGE_RATES)),),
    ),
)

_STAFF = 27
_UNANSWERED_PERCENT = 8
_WHOLE_TABLE_PERCENT = 30
_SECOND_TABLE_PERCENT = 35
# Un incident commence au chargement de nuit et dure jusqu'à sa correction.
_LOAD_HOUR = 6
_INCIDENT_DURATIONS = (
    timedelta(hours=6),
    timedelta(hours=10),
    timedelta(hours=30),
    timedelta(hours=54),
    timedelta(days=3),
)
_WINDOW_ATTEMPTS = 20


def _agents(dice: _Dice) -> tuple[Agent, ...]:
    # Des pseudonymes à la forme d'un HMAC, tirés de la graine : aucune identité derrière.
    staff = ["hmac-sha256:" + dice.hex(64, "recipient", rank) for rank in range(_STAFF)]
    return tuple(
        Agent(
            namespace=profile.namespace,
            name=profile.name,
            channel=profile.channel,
            access=profile.access,
            audience=tuple(sorted(dice.sample(staff, profile.audience, "audience", profile.name))),
        )
        for profile in _PROFILES
    )


def _agent_of(dice: _Dice, agents: Sequence[Agent], index: int) -> Agent:
    ticket = dice.below(sum(profile.share for profile in _PROFILES), "agent", index)
    for agent, profile in zip(agents, _PROFILES, strict=True):
        ticket -= profile.share
        if ticket < 0:
            return agent
    raise AssertionError("les parts couvrent tous les tirages")  # pragma: no cover


def _working_day(rank: int) -> datetime:
    """Le ``rank``-ième jour ouvré à partir du premier lundi."""
    return FIRST_DAY + timedelta(days=7 * (rank // 5) + rank % 5)


def _run_id(dice: _Dice, moment: datetime, *label: object) -> UUID:
    """UUID version 7 : l'heure en tête, le reste tiré de la graine."""
    millis = int(moment.timestamp() * 1000)
    value = (millis << 80) | dice.bits(80, "run", *label)
    value = (value & ~(0xF << 76)) | (0x7 << 76)
    value = (value & ~(0x3 << 62)) | (0x2 << 62)
    return UUID(int=value)


def _reads(
    dice: _Dice, tool: Tool, tables: Sequence[Table], day: str, *label: object
) -> tuple[TableRead, ...]:
    if not tool.sql:
        return tuple(TableRead(table) for table in tables)
    count = 2 if dice.below(100, "tables", *label) < _SECOND_TABLE_PERCENT else 1
    reads = []
    for table in dice.sample(tables, count, "table", *label):
        columns: tuple[str, ...] = ()
        if dice.below(100, "precision", *label, table.name) >= _WHOLE_TABLE_PERCENT:
            wanted = 1 + dice.below(len(table.columns), "width", *label, table.name)
            picked = set(dice.sample(table.columns, wanted, "column", *label, table.name))
            columns = tuple(column for column in table.columns if column in picked)
        reads.append(TableRead(table, columns, day if table.versioned else None))
    return tuple(sorted(reads, key=lambda read: (read.table.namespace, read.table.name)))


def _request(dice: _Dice, agents: Sequence[Agent], index: int, days: int) -> Request:
    agent = _agent_of(dice, agents, index)
    started_at = _working_day(dice.below(days, "day", index)) + timedelta(
        hours=OPENING_HOUR,
        minutes=dice.below(WORKING_MINUTES, "minute", index),
        seconds=dice.below(60, "second", index),
    )
    day = started_at.date().isoformat()

    moment = started_at
    calls = []
    for rank in range(1 + dice.below(3, "calls", index)):
        moment += timedelta(seconds=2 + dice.below(18, "pause", index, rank))
        tool, tables = dice.pick(agent.access, "tool", index, rank)
        calls.append(
            ToolCall(
                run_id=_run_id(dice, moment, index, rank),
                tool=tool,
                time=moment,
                reads=_reads(dice, tool, tables, day, index, rank),
            )
        )

    answer: Answer | None = None
    if dice.below(100, "outcome", index) >= _UNANSWERED_PERCENT:
        answer = Answer(
            fingerprint="sha256:" + dice.hex(64, "answer", index),
            channel=agent.channel,
            message_ref=dice.hex(16, "message", index),
            recipients=tuple(
                sorted(
                    dice.sample(agent.audience, 1 + dice.below(3, "audience", index), "to", index)
                )
            ),
            time=moment + timedelta(seconds=3 + dice.below(28, "writing", index)),
        )
    return Request(
        run_id=_run_id(dice, started_at, index),
        agent=agent,
        started_at=started_at,
        calls=tuple(calls),
        answer=answer,
    )


def _incidents(dice: _Dice, requests: Sequence[Request], count: int) -> tuple[Incident, ...]:
    # Lectures de chaque table par les demandes qui ont répondu.
    readings: dict[Table, list[tuple[datetime, UUID]]] = {}
    for request in requests:
        if request.answer is None:
            continue
        for call in request.calls:
            for read in call.reads:
                readings.setdefault(read.table, []).append((call.time, request.run_id))
    tables = sorted(readings, key=lambda table: (table.namespace, table.name))

    incidents: list[Incident] = []
    for rank in range(count if tables else 0):
        used = {incident.table for incident in incidents}
        table = dice.pick([t for t in tables if t not in used] or tables, "incident", rank)
        readers = {reader for _, reader in readings[table]}
        chosen: Incident | None = None
        # La fenêtre s'ouvre le jour d'une lecture et la contient ; elle est
        # retirée tant qu'elle couvre tous les lecteurs de la table, pour qu'un
        # incident ne touche pas tout le monde.
        for attempt in range(_WINDOW_ATTEMPTS):
            anchor, _ = dice.pick(readings[table], "anchor", rank, attempt)
            start = anchor.replace(hour=_LOAD_HOUR, minute=0, second=0)
            duration = dice.pick(
                [span for span in _INCIDENT_DURATIONS if anchor < start + span],
                "duration",
                rank,
                attempt,
            )
            chosen = Incident(f"incident-{rank + 1:02d}", table, start, start + duration)
            touched = {reader for moment, reader in readings[table] if start <= moment < chosen.end}
            if len(touched) < len(readers):
                break
        if chosen is not None:
            incidents.append(chosen)
    return tuple(incidents)


def generate(
    seed: int,
    *,
    requests: int = DEFAULT_REQUESTS,
    days: int = DEFAULT_DAYS,
    incidents: int = DEFAULT_INCIDENTS,
) -> Enterprise:
    """Génère l'entreprise de cette graine.

    ``requests`` demandes sont réparties sur ``days`` jours ouvrés, et au plus
    ``incidents`` incidents portent chacun sur une table lue par une réponse.
    """
    if requests < 1 or days < 1 or incidents < 0:
        raise ValueError(
            "Il faut au moins une demande et un jour, et un nombre d'incidents positif."
        )
    dice = _Dice(seed)
    agents = _agents(dice)
    generated = sorted(
        (_request(dice, agents, index, days) for index in range(requests)),
        key=lambda request: (request.started_at, request.run_id.int),
    )
    return Enterprise(
        seed=seed,
        tables=TABLES,
        tools=TOOLS,
        agents=agents,
        recipients=tuple(sorted({pseudonym for agent in agents for pseudonym in agent.audience})),
        requests=tuple(generated),
        incidents=_incidents(dice, generated, incidents),
    )
