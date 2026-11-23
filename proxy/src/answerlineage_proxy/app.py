"""Capture des appels d'outils, et relais ASGI transparent devant un serveur MCP.

``CallCapture`` est la part commune aux deux transports : une fois un échange
terminé, elle décrit l'appel d'outil (empreintes, signaux de qualité de
service, lectures déclarées ou analyse du SQL), émet son événement et tient les
compteurs et les relevés de couverture. ``McpProxy`` relaie HTTP ; le relais
stdio est dans ``stdio.py``.

Le proxy relaie chaque requête HTTP à l'identique et rend la réponse du
serveur octet pour octet, en flux : il ne décompresse rien, ne réécrit rien et
ne retient aucun fragment. Seuls les en-têtes propres à une connexion (RFC 9110,
section 7.6.1) ne sont pas transmis.

À côté du relais, il lit le corps des requêtes POST pour reconnaître un appel
d'outil et émet un événement par appel, une fois l'échange terminé. Aucun
en-tête, argument ou résultat n'est conservé ni journalisé.

Sur le chemin de l'appel, le relais ne fait que prendre des horodatages et
garder une copie bornée de la réponse : latence, latence ajoutée, issue et
reprises se calculent après l'échange (``signals.py``).

Exception à la transparence : un appel d'outil qui ne porte aucun contexte de
trace W3C reçoit celui que le proxy crée, dans un en-tête ``traceparent`` ajouté
à la requête relayée, sauf si son corps est trop grand pour être lu en entier.
Le corps n'est modifié que sur option, pour un outil SQL déclaré
(``--sql-run-comment``) : sa requête commence alors par un commentaire qui porte
l'identifiant de run (``tagging.py``), et rien d'autre du corps ne change.

Seconde exception, pendant une quarantaine active en mode avertir dont la
diffusion est vérifiée : le résultat d'un appel d'un outil qui lit la zone
reçoit en tête de son contenu un avertissement à gabarit fixe
(``annotation.py``). La rétention se décide à l'arrivée de la requête, par une
recherche dans la zone : un appel qui n'y touche pas est relayé comme toujours,
sans attendre. Pour un outil SQL, l'analyse de la requête part dès son arrivée,
dans un processus d'analyse dédié, à côté de l'exécution par le serveur ; la
réponse attend la décision au plus jusqu'à l'arrivée plus l'attente réglée
(50 ms par défaut, 0 pour n'attendre jamais), puis passe, annotée comme
incertaine si la décision manque.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, MutableMapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from urllib.parse import unquote_to_bytes

import httpx

from answerlineage_proxy.analyzer import AnalyzerResult, Origin, SqlAnalyzer
from answerlineage_proxy.annotation import (
    ANALYSIS_BUDGET_MS,
    JsonInsertion,
    NotAnnotatedReason,
    QuarantineNote,
    Retention,
    SseInsertion,
    Verdict,
    Zone,
    content_block,
    retention,
    sql_verdict,
    warning_text,
)
from answerlineage_proxy.config import CaptureSettings, ProxySettings, SqlTool
from answerlineage_proxy.coverage import (
    DEFAULT_WINDOW,
    CoverageWindows,
    Report,
    drawn_instance,
    report_periodically,
)
from answerlineage_proxy.events import (
    UNDESCRIBED_TOOL,
    Outcome,
    Transport,
    build_tool_call_event,
)
from answerlineage_proxy.fingerprint import RESULT_LABEL, Digest, arguments_fingerprint
from answerlineage_proxy.inspection import (
    HEADER_METHOD,
    HEADER_NAME,
    HEADER_PROTOCOL_VERSION,
    Inspection,
    inspect_prefix,
    inspect_request,
)
from answerlineage_proxy.prefix import PREFIX_BYTES, read_message_prefix
from answerlineage_proxy.quarantine import KnownQuarantines, QuarantineStatus, QuarantineSync
from answerlineage_proxy.rules import DeclaredReads, declared_reads
from answerlineage_proxy.signals import (
    CallOutcome,
    CallSignals,
    CallTiming,
    Failure,
    ReplySample,
    RetryKey,
    RetryMemory,
    assess,
    milliseconds,
)
from answerlineage_proxy.sink import DeferredSink, EventSink, receipt
from answerlineage_proxy.sql import SqlAnalysis
from answerlineage_proxy.tagging import comment_query
from answerlineage_proxy.tracecontext import TRACEPARENT, CallTrace, resolve_trace

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
Header = tuple[bytes, bytes]

logger = logging.getLogger("answerlineage_proxy")

# En-têtes propres à une connexion : ils ne franchissent pas un intermédiaire.
_HOP_BY_HOP = frozenset(
    {
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"proxy-connection",
        b"te",
        b"trailer",
        b"transfer-encoding",
        b"upgrade",
    }
)
# Seuls en-têtes dont la valeur est lue, pour être comparée au corps.
_INSPECTED_HEADERS = frozenset(
    name.encode("ascii") for name in (HEADER_METHOD, HEADER_NAME, HEADER_PROTOCOL_VERSION)
)
_TRACEPARENT = TRACEPARENT.encode("ascii")
# Session MCP (révision 2025-11-25) : lue pour reconnaître une reprise, jamais conservée.
_SESSION = b"mcp-session-id"
_CONTENT_LENGTH = b"content-length"
# En-têtes qu'un corps modifié démentirait : un codage du contenu que le proxy ne déploie
# pas, une empreinte ou une signature du message (RFC 9530, RFC 9421). Une requête qui en
# porte un n'est pas étiquetée ; seul le nom de l'en-tête est regardé.
_BODY_BOUND = frozenset(
    {
        b"content-encoding",
        b"content-digest",
        b"repr-digest",
        b"digest",
        b"content-md5",
        b"signature",
        b"signature-input",
    }
)
# En-têtes qui recopient des arguments d'outil, que le serveur peut comparer au corps.
_PARAM_HEADERS = b"mcp-param-"
# Une longueur de corps annoncée que le proxy sait relire, puis augmenter.
_LENGTH = re.compile(r"[0-9]{1,15}")
_JSON = "application/json"
_EVENT_STREAM = "text/event-stream"
_NS_PER_MS = 1_000_000
_BAD_GATEWAY_BODY = b'{"error":"upstream_unreachable"}'
_BAD_REQUEST_BODY = b'{"error":"invalid_request_target"}'
_FORBIDDEN_BODIES = {
    "host_not_allowed": b'{"error":"host_not_allowed"}',
    "origin_not_allowed": b'{"error":"origin_not_allowed"}',
    "cross_site_not_allowed": b'{"error":"cross_site_not_allowed"}',
}
_REFUSED_HEADERS = {
    "host_not_allowed": "Host",
    "origin_not_allowed": "Origin",
    "cross_site_not_allowed": "Sec-Fetch-Site",
}
# Segments « . » et « .. », même encodés : le client HTTP les résoudrait, et une
# requête sortirait du préfixe du serveur MCP.
_DOT_SEGMENTS = frozenset({b".", b".."})
# Début gardé d'un corps trop grand pour être lu : la méthode et le nom de
# l'outil y sont cherchés. Borné pour que la lecture, faite sur la boucle qui
# relaie les appels, ne prenne que quelques millisecondes, même sur un début hostile.
_PREFIX_WINDOW = PREFIX_BYTES
# Compteur de ProxyStats de chaque origine d'une analyse du SQL qui en a un.
# Noms d'outils sans règle de lecture journalisés, une fois chacun : au-delà, plus aucun.
_UNDESCRIBED_LOGGED = 1_000
_ORIGIN_COUNTERS: dict[Origin, str] = {
    "cache": "sql_from_cache",
    "over_budget": "sql_over_budget",
    "refused": "sql_refused",
    "failed": "sql_analyzer_failures",
}
_MISSING_ARGUMENT = SqlAnalysis(resolved=False, reason="missing_argument")
_TOO_LARGE = SqlAnalysis(resolved=False, reason="request_too_large")


def _has_dot_segment(raw_path: bytes) -> bool:
    return any(segment in _DOT_SEGMENTS for segment in unquote_to_bytes(raw_path).split(b"/"))


# httpx journalise l'URL complète de chaque requête, paramètres compris, et
# httpcore les en-têtes des réponses en mode débogage.
_TRANSPORT_LOGGERS = ("httpx", "httpcore")


def quiet_transport_logs() -> None:
    """Relève le niveau des journaux du client HTTP, qui citeraient URL et en-têtes.

    Le réglage vaut pour tout le processus et résiste à un niveau DEBUG posé
    sur le journal racine.
    """
    for name in _TRANSPORT_LOGGERS:
        transport_logger = logging.getLogger(name)
        if transport_logger.level == logging.NOTSET or transport_logger.level < logging.WARNING:
            transport_logger.setLevel(logging.WARNING)


class _ClientGoneError(Exception):
    """Le client a fermé la connexion avant la fin de sa requête."""


@dataclass
class ProxyStats:
    """Compteurs du proxy, sans aucune valeur issue des requêtes."""

    relayed: int = 0
    tool_calls: int = 0
    events_emitted: int = 0
    emit_failures: int = 0
    uninspected: int = 0
    header_mismatches: int = 0
    upstream_failures: int = 0
    client_disconnects: int = 0
    # Requêtes refusées pour un en-tête Host ou Origin inattendu, jamais relayées.
    refused: int = 0
    # Appels d'outil décrits d'après le seul début d'un corps trop grand pour être lu.
    inspected_from_prefix: int = 0
    # Analyses du SQL servies par le cache, abandonnées au budget, refusées faute
    # de place dans la file, ou perdues par un processus d'analyse en échec.
    sql_from_cache: int = 0
    sql_over_budget: int = 0
    sql_refused: int = 0
    sql_analyzer_failures: int = 0
    # Appels d'un outil sans règle de lecture ni SQL déclaré : leurs lectures ne sont
    # pas décrites.
    undescribed_calls: int = 0
    # Transport stdio : appels d'outil dont l'issue n'a pas pu être établie (réponse
    # illisible, identifiant en double, mémoire des requêtes en attente pleine), dont
    # l'événement est donc perdu ; réponses du serveur rattachées à aucune requête ;
    # octets relayés sans être inspectés parce que l'inspection avait pris du retard.
    unmatched_calls: int = 0
    unidentified_replies: int = 0
    lagged_bytes: int = 0
    # Outils dont la requête relayée porte l'identifiant de run en commentaire : appels dont
    # la requête l'a reçu ; appels relayés sans lui, et, l'option active, requêtes qui n'ont
    # pas pu être lues sans ambiguïté et qui pouvaient en cacher un.
    tagged_queries: int = 0
    untagged_queries: int = 0
    # Transport HTTP : appels coupés à l'arrêt du proxy, au-delà du délai de grâce.
    cut_at_shutdown: int = 0
    # Quarantaine en mode avertir, en HTTP : appels retenus ; résultats qui ont reçu
    # l'avertissement ; résultats qui devaient le recevoir et ne l'ont pas reçu, le motif dit
    # par l'événement de l'appel.
    retained_calls: int = 0
    annotated_results: int = 0
    unannotated_results: int = 0


@dataclass
class _Retained:
    """Un appel retenu par une quarantaine : ce qui le retient, la zone à son arrivée, la
    décision, et ce que le relais en a fait."""

    retention: Retention
    zone: Zone
    # Échéance de la décision, sur l'horloge monotone en nanosecondes : l'arrivée de la
    # requête plus l'attente réglée, ``budget_ms``.
    deadline: int
    budget_ms: int
    identifier: str | int | None = field(default=None, repr=False)
    # Outil SQL : l'analyse de sa requête, lancée dès l'arrivée, à côté de l'appel.
    analysis: asyncio.Task[AnalyzerResult] | None = None
    # Statique pour une règle de lecture ; prise par le relais pour un outil SQL, ou, s'il
    # n'a rien eu à annoter, une fois l'échange fini.
    verdict: Verdict | None = None
    # Temps passé à attendre la décision, en nanosecondes.
    waited: int = 0
    settled: bool = False
    annotated: bool = False
    reason: NotAnnotatedReason | None = None

    def settle(self, *, added: int = 0, reason: NotAnnotatedReason | None = None) -> None:
        """Le relais a conclu : le bloc est inséré, ou rien, pour ``reason``. Seule la
        première conclusion compte."""
        if not self.settled:
            self.settled = True
            self.annotated = added > 0
            self.reason = None if self.annotated else reason

    def note(self, verdict: Verdict) -> QuarantineNote:
        """Ce que l'événement dit de l'appel : la décision, et ce que le relais en a fait."""
        reason = None
        if verdict.warns and not self.annotated:
            reason = self.reason if self.reason is not None else "not_whole"
        wait_ms = min(milliseconds(self.waited), float(self.budget_ms))
        return QuarantineNote(
            verdict.decision,
            verdict.incidents,
            annotated=self.annotated,
            wait_ms=wait_ms,
            uncertain_reason=verdict.uncertain_reason,
            not_annotated_reason=reason,
        )


@dataclass
class _Exchange:
    """Issue d'un échange relayé, en HTTP ou en stdio."""

    outcome: Outcome = "FAIL"
    status: int | None = None
    error: str | None = None
    # Contexte de trace d'un appel d'outil : l'identifiant de run en dérive.
    trace: CallTrace | None = None
    # Corps de la réponse relayé : sa taille et, avec une clé, son empreinte, au fil
    # de l'eau. Elles ne valent que pour un corps relayé en entier.
    digest: Digest | None = None
    size: int = 0
    relayed_whole: bool = False
    # Qualité de service : horodatages, échec du relais, statut rendu au client (du
    # serveur MCP ou du proxy), réponse rendue en entier ou non.
    timing: CallTiming = field(default_factory=CallTiming)
    failure: Failure | None = None
    answered: int | None = None
    delivered: bool = False
    # Appel d'outil : sa réponse est échantillonnée pour y lire une erreur JSON-RPC.
    observed: bool = False
    sample: ReplySample | None = None
    # En-têtes Mcp-Session-Id reçus, gardés le temps de calculer la portée d'une reprise.
    sessions: list[bytes] = field(default_factory=list, repr=False)
    # Erreur JSON-RPC et son code, quand le relais a lu lui-même la réponse entière (stdio).
    reply_error: tuple[bool, int | None] = (False, None)
    # Outil dont la requête relayée porte l'identifiant de run en commentaire : l'a-t-elle reçu ?
    query_tagged: bool | None = None
    # Appel retenu par une quarantaine, en HTTP : son résultat attend la décision.
    retained: _Retained | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def end_to_end_headers(headers: Iterable[Header]) -> list[Header]:
    """Retire les en-têtes propres à la connexion, dont ceux que Connection désigne."""
    items = list(headers)
    named = {
        token.strip().lower()
        for name, value in items
        if name.lower() == b"connection"
        for token in value.split(b",")
    }
    return [
        (name, value)
        for name, value in items
        if name.lower() not in _HOP_BY_HOP and name.lower() not in named
    ]


def _failure(error: httpx.HTTPError) -> Failure:
    return "timeout" if isinstance(error, httpx.TimeoutException) else "transport_error"


def _traceparents(headers: Iterable[Header]) -> list[str]:
    """Valeurs des en-têtes ``traceparent`` reçus : lues, jamais journalisées."""
    return [value.decode("latin-1") for name, value in headers if name.lower() == _TRACEPARENT]


def _head(chunks: list[bytes], size: int) -> bytes:
    """Les ``size`` premiers octets d'un corps reçu en morceaux, sans tout recopier."""
    kept: list[bytes] = []
    remaining = size
    for chunk in chunks:
        if remaining <= 0:
            break
        kept.append(chunk[:remaining])
        remaining -= len(kept[-1])
    return b"".join(kept)


async def _settle(task: asyncio.Task[Any]) -> None:
    """Annule une tâche et attend qu'elle ait rendu la main."""
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _account(exchange: _Exchange, chunk: bytes) -> None:
    """Un morceau du corps rendu par le serveur : sa taille, son empreinte, son échantillon."""
    exchange.size += len(chunk)
    if exchange.digest is not None:
        exchange.digest.update(chunk)
    if exchange.sample is not None:
        exchange.sample.add(chunk)


def _insertion(
    retained: _Retained, response: httpx.Response
) -> JsonInsertion | SseInsertion | None:
    """De quoi insérer l'avertissement dans cette réponse ; ``None``, le motif dit à l'appel
    retenu, quand ses en-têtes l'excluent : une erreur, une réponse sans contenu, un corps
    compressé ou lié par une empreinte ou une signature, que le proxy ne réécrit pas."""
    status, headers = response.status_code, response.headers
    reason: NotAnnotatedReason
    media = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    lengths = headers.get_list("content-length")
    if status >= 400:
        reason = "error_response"
    elif not 200 <= status < 300:
        reason = "no_content"
    elif headers.get("content-encoding", "identity").strip().lower() != "identity":
        reason = "encoded"
    elif any(name.lower() in _BODY_BOUND for name, _ in headers.raw):
        reason = "body_bound"
    elif media == _JSON:
        # Une longueur annoncée grandit de la taille du bloc : seule une longueur lisible.
        if len(lengths) <= 1 and all(_LENGTH.fullmatch(value.strip()) for value in lengths):
            return JsonInsertion(retained.identifier)
        reason = "body_bound"
    elif media == _EVENT_STREAM:
        # Un flux part avant la place du bloc : une longueur annoncée ne se corrigerait pas.
        if not lengths:
            return SseInsertion(retained.identifier)
        reason = "body_bound"
    else:
        reason = "no_content"
    retained.settle(reason=reason)
    return None


def _lengthened(start: Message, added: int) -> Message:
    """Les en-têtes d'une réponse dont le corps a grandi de ``added`` octets."""
    if not added:
        return start
    headers = [
        (name, str(int(value) + added).encode("ascii") if name == _CONTENT_LENGTH else value)
        for name, value in start["headers"]
    ]
    return {**start, "headers": headers}


class CallCapture[SettingsT: CaptureSettings](ABC):
    """Ce que les deux transports partagent : décrire un appel d'outil terminé, émettre son
    événement, compter, et remettre les relevés de couverture."""

    # Transport des appels, recopié dans la facette de l'appel.
    transport: ClassVar[Transport]

    def __init__(
        self,
        settings: SettingsT,
        sink: EventSink,
        *,
        clock: Callable[[], datetime] = _now,
        analyzer: SqlAnalyzer | None = None,
        coverage_window: timedelta = DEFAULT_WINDOW,
        retries: RetryMemory | None = None,
        quarantine: QuarantineSync | None = None,
        quarantine_analyzer: SqlAnalyzer | None = None,
    ) -> None:
        self.settings = settings
        self.stats = ProxyStats()
        # Les périmètres en quarantaine, appris de l'API hors du chemin des appels ; le relais
        # HTTP annote les lectures de leur zone. Sans adresse d'API, le proxy reste en mode
        # observer.
        self.quarantine = quarantine
        # Compteurs par fenêtre, remis à la destination des événements comme relevés de
        # couverture. L'émetteur est l'espace de noms des jobs du proxy ; l'instance, réglée
        # ou tirée au démarrage, distingue les proxys placés devant un même serveur.
        self.instance = settings.instance_id or drawn_instance()
        self.coverage = CoverageWindows(
            f"mcp://{settings.server_name}",
            instance=self.instance,
            length=coverage_window,
            started_at=clock(),
        )
        self._reporting: asyncio.Task[None] | None = None
        self._sink = sink
        # Une destination à remise différée dit plus tard si chaque événement a été remis :
        # l'appel compte alors parmi les émis ou les perdus de sa fenêtre.
        self._deferred = sink if isinstance(sink, DeferredSink) else None
        if self._deferred is not None:
            self._deferred.watch(self._settled)
        self._clock = clock
        self._sql_arguments = {tool: rule.argument for tool, rule in settings.sql_tools.items()}
        # Outils SQL dont la requête relayée commence par le commentaire de son run.
        self._commented = frozenset(
            tool for tool, rule in settings.sql_tools.items() if rule.run_comment
        )
        # Chaînes de reprises en cours, en mémoire bornée.
        self.retries = retries if retries is not None else RetryMemory()
        # Outils sans règle de lecture déjà journalisés, des noms validés seulement.
        self._undescribed: set[str] = set()
        # Analyses de SQL en cours, menées après la réponse, hors du chemin de l'appel.
        self._pending: set[asyncio.Task[None]] = set()
        # Processus d'analyse du SQL, démarré à la première requête à analyser.
        self.analyzer = (
            analyzer
            if analyzer is not None
            else SqlAnalyzer(
                budget=settings.sql_budget_ms / 1000,
                queue_size=settings.sql_queue_size,
                cache_size=settings.sql_cache_size,
            )
        )
        # Processus d'analyse dédié aux appels retenus, sous un budget fixe : la décision
        # n'attend pas derrière les analyses menées après les appels. Démarré à la première
        # zone qui vise un outil SQL déclaré.
        self.quarantine_analyzer = (
            quarantine_analyzer
            if quarantine_analyzer is not None
            else SqlAnalyzer(
                budget=ANALYSIS_BUDGET_MS / 1000,
                queue_size=settings.sql_queue_size,
                cache_size=settings.sql_cache_size,
            )
        )

    async def drain(self) -> None:
        """Attend la fin des analyses en cours, pour que leurs événements soient émis."""
        while self._pending:
            tasks = list(self._pending)
            await asyncio.gather(*tasks, return_exceptions=True)
            # Retrait explicite : attendre des tâches déjà terminées ne rend pas la
            # main à la boucle, et leur rappel de fin n'aurait pas encore tourné.
            self._pending.difference_update(tasks)

    def quarantine_status(self) -> QuarantineStatus:
        """L'état de la synchronisation des quarantaines : sans elle, le mode observer."""
        if self.quarantine is None:
            return QuarantineStatus(
                mode="observe", scopes=0, datasets=0, last_sync_age=None, failing=False
            )
        return self.quarantine.status()

    async def aclose(self) -> None:
        """Arrête la synchronisation des quarantaines, termine les analyses en cours, remet la
        dernière fenêtre de couverture, arrête le processus d'analyse, puis vide la
        destination à remise différée dans son délai."""
        if self.quarantine is not None:
            await self.quarantine.aclose()
        await self.drain()
        if self._reporting is not None:
            await _settle(self._reporting)
            self._reporting = None
        self._report(self.coverage.close_all(self._clock()))
        await self.analyzer.aclose()
        await self.quarantine_analyzer.aclose()
        if self._deferred is not None:
            await self._deferred.aclose()

    async def _start_reporting(self) -> None:
        """Démarre la destination à remise différée, puis remet chaque fenêtre de couverture
        close, hors du chemin des appels ; démarre la synchronisation des quarantaines, sans
        attendre l'API."""
        if self._deferred is not None:
            await self._deferred.start()
        self._reporting = asyncio.ensure_future(
            report_periodically(self.coverage, self._clock, self._report)
        )
        if self.quarantine is not None:
            await self.quarantine.start()

    def _inspected(self, inspection: Inspection | None, *, untagged: bool = True) -> None:
        """Compte une requête que le proxy n'a pas pu lire sans ambiguïté ; journalise un
        écart entre les en-têtes MCP et le corps, par le nom des en-têtes seulement.

        Quand un outil reçoit le commentaire de son run, une telle requête est aussi
        comptée parmi les non étiquetées : elle pouvait en cacher un appel. ``untagged``
        est faux pour un appel dont l'étiquetage est déjà compté.
        """
        if inspection is None:
            self.stats.uninspected += 1
            self.coverage.uninspected(self._clock())
            if untagged and self._commented:
                self.stats.untagged_queries += 1
        elif inspection.header_mismatches:
            self.stats.header_mismatches += 1
            # Seuls les noms des en-têtes sont journalisés, jamais leur valeur.
            logger.warning(
                "En-têtes MCP contredits par le corps de la requête : %s",
                ", ".join(inspection.header_mismatches),
            )

    def _tagged(self, tagged: bool) -> None:
        """Compte un appel d'un outil à commentaire, selon que sa requête l'a reçu."""
        if tagged:
            self.stats.tagged_queries += 1
        else:
            self.stats.untagged_queries += 1

    @abstractmethod
    def _assess(self, exchange: _Exchange) -> tuple[CallOutcome, int | None]:
        """L'issue d'un échange terminé et son code d'erreur, selon le transport."""

    def _describe(
        self, inspection: Inspection, exchange: _Exchange, *, oversized: bool = False
    ) -> None:
        """Émet l'événement de l'appel, après analyse du SQL si l'outil est déclaré.

        Empreintes et signaux de qualité de service se calculent ici, l'échange
        terminé, dans l'ordre où les appels s'achèvent : c'est cet ordre qui
        relie une reprise à l'appel qu'elle reprend.
        """
        event_time = self._clock()
        self.coverage.begin(event_time)
        try:
            details = self._details(inspection, exchange)
        except Exception as error:
            logger.error("Signaux de l'appel non établis (%s).", type(error).__name__)
            details = {}
        finally:
            # Rien de la réponse ni de la session n'est gardé au-delà.
            exchange.sample = None
            exchange.sessions = []
        rule = self.settings.sql_tools.get(inspection.tool) if inspection.tool else None
        retained = exchange.retained
        if rule is None:
            try:
                reads, declared = self._without_sql(inspection, exchange)
            except Exception as error:
                logger.error("Lectures de l'appel non établies (%s).", type(error).__name__)
                reads, declared = None, None
            if declared is not None:
                details = {**details, "declared": declared}
            note = self._note(retained)
            self._emit(inspection, exchange, event_time, details, reads, quarantine=note)
            return
        if retained is not None:
            # L'analyse de la requête, lancée dès son arrivée, sert à l'événement.
            task = asyncio.ensure_future(
                self._analyze_then_emit(inspection, exchange, event_time, details, rule)
            )
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)
            return
        if exchange.outcome != "COMPLETE":
            # Un appel qui a échoué n'a rien fait lire à l'agent.
            self._emit(inspection, exchange, event_time, details)
            return
        if oversized:
            # La requête n'a pas été lue : sa lecture n'est pas connue.
            reads = SqlAnalysis(resolved=False, reason="request_too_large")
            self._emit(
                inspection, exchange, event_time, details, reads, rule.namespace, rule.dialect
            )
            return
        task = asyncio.ensure_future(
            self._analyze_then_emit(inspection, exchange, event_time, details, rule)
        )
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    def _without_sql(
        self, inspection: Inspection, exchange: _Exchange
    ) -> tuple[SqlAnalysis | None, DeclaredReads | None]:
        """Les lectures d'un appel sans SQL : celles que déclare la règle de l'outil, ou
        une lecture non décrite, comptée, et journalisée une fois par nom d'outil."""
        tool = inspection.tool
        if tool is None:
            return None, None
        rule = self.settings.read_rules.get(tool)
        if rule is None:
            self.stats.undescribed_calls += 1
            if tool not in self._undescribed and len(self._undescribed) < _UNDESCRIBED_LOGGED:
                self._undescribed.add(tool)
                # Un nom d'outil validé, jamais un argument.
                logger.warning(
                    "Outil « %s » sans règle de lecture : ses lectures ne sont pas décrites.",
                    tool,
                )
            return SqlAnalysis(resolved=False, reason=UNDESCRIBED_TOOL), None
        if exchange.outcome != "COMPLETE":
            # Un appel qui a échoué n'a rien fait lire à l'agent.
            return None, None
        return None, declared_reads(
            rule,
            inspection.arguments,
            readable=inspection.has_arguments,
            key=self.settings.fingerprint_key,
        )

    def _details(self, inspection: Inspection, exchange: _Exchange) -> dict[str, Any]:
        """Empreintes, taille du résultat et signaux de qualité de service de l'appel."""
        details = self._fingerprints(inspection, exchange)
        details["signals"] = self._signals(
            inspection, exchange, details.get("arguments_fingerprint")
        )
        return details

    def _signals(
        self, inspection: Inspection, exchange: _Exchange, arguments: str | None
    ) -> CallSignals:
        """Latence, latence ajoutée, issue et reprises d'un appel terminé.

        Une reprise se reconnaît à l'outil et à l'empreinte des arguments : sans
        clé d'empreinte, ou sans empreinte des arguments, le nombre de reprises
        n'est pas connu, et il est absent.
        """
        timing = exchange.timing
        timing.finish()
        ended = timing.ended if timing.ended is not None else timing.started
        outcome, code = self._assess(exchange)
        key = None
        if arguments is not None and inspection.tool is not None:
            scope = self.retries.scope(exchange.sessions, exchange.trace)
            key = RetryKey(scope, inspection.tool, arguments)
        retries = self.retries.record(
            key, started=timing.started, ended=ended, failed=outcome != "success"
        )
        return CallSignals(
            latency_ms=milliseconds(ended - timing.started),
            added_latency_ms=milliseconds(timing.added),
            outcome=outcome,
            error_code=code,
            retry_count=retries,
        )

    async def _analyze_then_emit(
        self,
        inspection: Inspection,
        exchange: _Exchange,
        event_time: datetime,
        details: dict[str, Any],
        rule: SqlTool,
    ) -> None:
        # Tant que l'analyse n'a pas abouti, la lecture est non résolue : l'événement
        # part quoi qu'il arrive, même si l'attente est interrompue.
        reads = SqlAnalysis(resolved=False, reason="analyzer_unavailable")
        retained = exchange.retained
        try:
            if retained is not None and retained.analysis is not None:
                result = await self._retained_analysis(inspection, rule, retained.analysis)
            elif inspection.sql is None:
                result = AnalyzerResult(_MISSING_ARGUMENT, "analyzed")
            else:
                # Dans un processus à part : l'analyse ne dispute pas l'interpréteur à la
                # boucle qui relaie les appels.
                result = await self.analyzer.analyze(
                    inspection.sql,
                    rule.dialect,
                    default_catalog=rule.default_catalog,
                    default_db=rule.default_db,
                )
            counter = _ORIGIN_COUNTERS.get(result.origin)
            if counter is not None:
                setattr(self.stats, counter, getattr(self.stats, counter) + 1)
            reads = result.analysis
        except Exception as error:
            self.stats.sql_analyzer_failures += 1
            logger.error("Analyse du SQL en échec (%s).", type(error).__name__)
            reads = SqlAnalysis(resolved=False, reason="analyzer_failed")
        finally:
            note = None
            if retained is not None:
                if retained.verdict is None:
                    retained.verdict = self._judge(retained, reads)
                note = self._note(retained)
            # Un appel qui a échoué n'a rien fait lire à l'agent.
            read = reads if exchange.outcome == "COMPLETE" else None
            self._emit(
                inspection,
                exchange,
                event_time,
                details,
                read,
                rule.namespace,
                rule.dialect,
                quarantine=note,
            )

    async def _retained_analysis(
        self, inspection: Inspection, rule: SqlTool, analysis: asyncio.Task[AnalyzerResult]
    ) -> AnalyzerResult:
        """L'analyse faite pendant l'appel retenu. Abandonnée à son budget fixe, elle est
        refaite par l'analyseur ordinaire quand son budget réglé est plus large : le lignage
        reste aussi complet que pour un autre appel."""
        result = await analysis
        sql = inspection.sql
        if sql is None or result.origin != "over_budget":
            return result
        if self.settings.sql_budget_ms <= ANALYSIS_BUDGET_MS:
            return result
        return await self.analyzer.analyze(
            sql, rule.dialect, default_catalog=rule.default_catalog, default_db=rule.default_db
        )

    @staticmethod
    def _judge(retained: _Retained, analysis: SqlAnalysis | None) -> Verdict:
        """La décision : statique pour une règle de lecture, d'après l'analyse pour un outil
        SQL, incertaine sans analyse."""
        if retained.retention.verdict is not None:
            return retained.retention.verdict
        namespace = retained.retention.namespace or ""
        return sql_verdict(retained.zone, namespace, analysis)

    def _note(self, retained: _Retained | None) -> QuarantineNote | None:
        """Ce que l'événement d'un appel retenu dit de sa décision ; compte les résultats
        annotés et ceux qui devaient l'être."""
        if retained is None:
            return None
        verdict = retained.verdict if retained.verdict is not None else self._judge(retained, None)
        note = retained.note(verdict)
        if verdict.warns:
            if note.annotated:
                self.stats.annotated_results += 1
            else:
                self.stats.unannotated_results += 1
        return note

    def _emit(
        self,
        inspection: Inspection,
        exchange: _Exchange,
        event_time: datetime,
        details: dict[str, Any],
        reads: SqlAnalysis | None = None,
        namespace: str | None = None,
        dialect: str | None = None,
        *,
        quarantine: QuarantineNote | None = None,
    ) -> None:
        """Émet l'événement de l'appel. Un échec d'émission n'atteint jamais l'appel."""
        self.stats.tool_calls += 1
        trace = exchange.trace
        handed: str | None = None
        try:
            if trace is None:  # pragma: no cover — tout appel d'outil reçoit son contexte.
                raise RuntimeError("contexte de trace manquant")
            event = build_tool_call_event(
                run_id=trace.run_id,
                event_time=event_time,
                outcome=exchange.outcome,
                server=self.settings.server_name,
                inspection=inspection,
                http_status=exchange.status,
                proxy_error=exchange.error,
                reads=reads,
                namespace=namespace,
                trace=trace,
                dialect=dialect,
                transport=self.transport,
                tagged_with_run_id=exchange.query_tagged,
                quarantine=quarantine,
                **details,
            )
            if self._deferred is not None:
                handed = receipt(event)
                self.coverage.hand_off(event_time, handed)
            self._sink.emit(event)
        except Exception as error:
            self.stats.emit_failures += 1
            if handed is None:
                self.coverage.settle(event_time, emitted=False)
            else:
                self._settled(handed, False)
            logger.error("Événement de lignage non émis (%s).", type(error).__name__)
        else:
            self.stats.events_emitted += 1
            if handed is None:
                self.coverage.settle(event_time, emitted=True)

    def _settled(self, receipt: str, delivered: bool) -> None:
        """La destination dit qu'un événement a été remis, ou perdu. Si sa fenêtre est déjà
        close, son relevé corrigé part à son tour."""
        corrected = self.coverage.outcome(receipt, delivered=delivered)
        if corrected is not None:
            self._report([corrected])

    def _report(self, reports: list[Report]) -> None:
        """Remet des relevés de couverture. Un échec de remise n'atteint jamais un appel."""
        for report in reports:
            try:
                self._sink.report_coverage(report)
            except Exception as error:
                logger.error("Relevé de couverture non remis (%s).", type(error).__name__)

    def _fingerprints(self, inspection: Inspection, exchange: _Exchange) -> dict[str, Any]:
        """Empreintes et taille de l'appel : aucune empreinte sans clé, et rien du
        résultat quand son corps n'a pas été relayé en entier."""
        fields: dict[str, Any] = {}
        key = self.settings.fingerprint_key
        if key is not None and inspection.has_arguments:
            fields["arguments_fingerprint"] = arguments_fingerprint(key, inspection.arguments)
        if exchange.relayed_whole:
            fields["result_size"] = exchange.size
            if exchange.digest is not None:
                fields["result_fingerprint"] = exchange.digest.fingerprint()
        return fields


class McpProxy(CallCapture[ProxySettings]):
    """Application ASGI placée devant un serveur MCP."""

    transport = "http"

    def __init__(
        self,
        settings: ProxySettings,
        sink: EventSink,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] = _now,
        analyzer: SqlAnalyzer | None = None,
        coverage_window: timedelta = DEFAULT_WINDOW,
        retries: RetryMemory | None = None,
        quarantine: QuarantineSync | None = None,
        quarantine_analyzer: SqlAnalyzer | None = None,
    ) -> None:
        quiet_transport_logs()
        super().__init__(
            settings,
            sink,
            clock=clock,
            analyzer=analyzer,
            coverage_window=coverage_window,
            retries=retries,
            quarantine=quarantine,
            quarantine_analyzer=quarantine_analyzer,
        )
        self._client = client
        self._owns_client = client is None
        self._prefix = settings.upstream.raw_path.rstrip(b"/")
        self._guard = settings.guard()
        # Appels en cours : à l'arrêt, ceux que le serveur HTTP a coupés finissent de se
        # décrire avant que la boîte d'envoi se vide.
        self._calls: set[asyncio.Task[Any]] = set()
        # La zone de la diffusion vérifiée, et le périmètre dont elle vient : calculée quand le
        # périmètre change, hors du chemin des appels.
        self._zone: Zone | None = None
        self._zone_of: KnownQuarantines | None = None
        # Préchauffage de l'analyseur de quarantaine, hors du chemin des appels.
        self._warming: asyncio.Task[None] | None = None
        if quarantine is not None:
            quarantine.watch(self._zone_changed)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            call = asyncio.current_task()
            if call is not None:
                self._calls.add(call)
            try:
                await self._http(scope, receive, send)
            finally:
                self._calls.discard(call)
        elif scope["type"] == "lifespan":
            await self._lifespan(receive, send)
        elif scope["type"] == "websocket":
            # MCP ne passe pas par WebSocket : la connexion est refusée.
            await receive()
            await send({"type": "websocket.close", "code": 1003})

    async def aclose(self) -> None:
        """Attend que les appels coupés par l'arrêt du serveur HTTP aient émis leur événement,
        termine les analyses en cours, remet la dernière fenêtre de couverture, arrête le
        processus d'analyse, vide la destination à remise différée dans son délai, puis ferme
        les connexions vers le serveur MCP."""
        # Un appel coupé lève l'annulation, puis se décrit en quelques tours de boucle ; un
        # appel que rien n'a coupé n'est pas attendu.
        cut = [call for call in self._calls if call.cancelling()]
        await asyncio.gather(*cut, return_exceptions=True)
        if self._warming is not None:
            await _settle(self._warming)
            self._warming = None
        await super().aclose()
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def _current_zone(self) -> Zone | None:
        """La zone de la diffusion vérifiée, ``None`` sans elle. Calculée une fois par
        périmètre connu : d'ordinaire quand il change, hors du chemin des appels."""
        sync = self.quarantine
        if sync is None or not sync.annotating:
            return None
        known = sync.known
        if known is not None and known is not self._zone_of:
            self._zone, self._zone_of = Zone.of(known.scopes), known
        return self._zone

    def _zone_changed(self) -> None:
        """Le périmètre a changé : la zone se calcule, et, si elle vise un outil SQL déclaré,
        l'analyseur de quarantaine démarre et se préchauffe, hors du chemin des appels."""
        zone = self._current_zone()
        if zone is None or (self._warming is not None and not self._warming.done()):
            return
        if any(rule.namespace in zone.namespaces for rule in self.settings.sql_tools.values()):
            self._warming = asyncio.ensure_future(self.quarantine_analyzer.warm())

    def _upstream_client(self) -> httpx.AsyncClient:
        if self._client is None:
            client = httpx.AsyncClient(
                follow_redirects=False,
                trust_env=False,
                timeout=httpx.Timeout(None, connect=self.settings.connect_timeout),
                limits=httpx.Limits(max_connections=None, max_keepalive_connections=100),
            )
            # Aucun en-tête ajouté : le serveur reçoit ceux du client, rien de plus.
            client.headers.clear()
            self._client = client
        return self._client

    async def _lifespan(self, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                self._upstream_client()
                await self._start_reporting()
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await self.aclose()
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _http(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Premier geste : l'horodatage d'arrivée de la requête.
        timing = CallTiming()
        headers: list[Header] = [(bytes(name), bytes(value)) for name, value in scope["headers"]]
        refusal = self._guard.refusal(headers)
        if refusal is not None:
            self.stats.refused += 1
            # Seul le nom de l'en-tête est journalisé : sa valeur est choisie par l'appelant.
            logger.warning("Requête refusée : en-tête %s inattendu.", _REFUSED_HEADERS[refusal])
            await self._respond(send, 403, _FORBIDDEN_BODIES[refusal])
            return
        self.stats.relayed += 1
        upload_done = asyncio.Event()
        timing.pause()
        try:
            body, stream = await self._receive_body(receive, upload_done)
        except _ClientGoneError:
            self.stats.client_disconnects += 1
            return
        timing.resume()

        # Au-delà de la taille lue, seul le début du corps est décrit.
        oversized = stream is not None or len(body) > self.settings.max_inspected_bytes
        inspection = self._inspect(scope["method"], headers, body, oversized)
        exchange = _Exchange(timing=timing)
        relayed = headers
        if inspection is not None and inspection.is_tool_call:
            exchange.observed = True
            exchange.sessions = [value for name, value in headers if name.lower() == _SESSION]
            exchange.trace = resolve_trace(inspection.trace_context, _traceparents(headers))
            if self.settings.fingerprint_key is not None:
                exchange.digest = self.settings.fingerprint_key.digest(RESULT_LABEL)
            if oversized:
                # Le corps n'a pas été lu en entier, son contexte de trace non plus : la
                # requête est relayée telle quelle, et un contexte créé ne sert qu'à
                # l'identifiant de run.
                exchange.trace = replace(exchange.trace, forwarded=None)
            elif exchange.trace.forwarded is not None:
                # Seul en-tête que le proxy ajoute : le contexte qu'il a créé, transmis au serveur.
                relayed = [*headers, (_TRACEPARENT, exchange.trace.forwarded.encode("ascii"))]
            tool = inspection.tool
            if tool is not None and tool in self._commented:
                # Un corps qui n'a pas été lu en entier n'est jamais modifié.
                tagged = None if oversized else self._comment(body, relayed, tool, exchange.trace)
                exchange.query_tagged = tagged is not None
                self._tagged(exchange.query_tagged)
                if tagged is not None:
                    body, relayed = tagged
            exchange.retained = self._retain(inspection, body, oversized, timing)
        try:
            await self._exchange(scope, relayed, body, stream, upload_done, receive, send, exchange)
        except asyncio.CancelledError:
            # Seul l'arrêt du serveur HTTP annule un appel : au-delà du délai de grâce.
            self.stats.cut_at_shutdown += 1
            raise
        finally:
            timing.finish()
            if inspection is not None and inspection.is_tool_call:
                self._describe(inspection, exchange, oversized=oversized)

    def _retain(
        self, inspection: Inspection, body: bytes, oversized: bool, timing: CallTiming
    ) -> _Retained | None:
        """L'appel est-il retenu par une quarantaine ? Une recherche dans la zone vérifiée, et
        rien d'autre pour un appel qui n'y touche pas. Pour un outil SQL retenu, l'analyse de
        la requête part aussitôt, dans l'analyseur de quarantaine."""
        zone = self._current_zone()
        found = retention(zone, inspection.tool, self.settings.sql_tools, self.settings.read_rules)
        if found is None or zone is None:
            return None
        budget = self.settings.quarantine_decision_ms
        identifier = read_message_prefix(body).identifier if oversized else inspection.identifier
        retained = _Retained(
            found,
            zone,
            deadline=timing.started + budget * _NS_PER_MS,
            budget_ms=budget,
            identifier=identifier,
            verdict=found.verdict,
        )
        tool = inspection.tool
        if found.namespace is not None and tool is not None:
            rule = self.settings.sql_tools[tool]
            retained.analysis = asyncio.ensure_future(
                self._quarantine_analysis(inspection, rule, oversized)
            )
        self.stats.retained_calls += 1
        return retained

    async def _quarantine_analysis(
        self, inspection: Inspection, rule: SqlTool, oversized: bool
    ) -> AnalyzerResult:
        """L'analyse de la requête d'un appel retenu, par l'analyseur de quarantaine. Ne lève
        pas : un échec donne une lecture non résolue."""
        if oversized:
            return AnalyzerResult(_TOO_LARGE, "analyzed")
        if inspection.sql is None:
            return AnalyzerResult(_MISSING_ARGUMENT, "analyzed")
        try:
            return await self.quarantine_analyzer.analyze(
                inspection.sql,
                rule.dialect,
                default_catalog=rule.default_catalog,
                default_db=rule.default_db,
            )
        except Exception as error:
            logger.error("Analyse de quarantaine en échec (%s).", type(error).__name__)
            return AnalyzerResult(SqlAnalysis(resolved=False, reason="analyzer_failed"), "failed")

    def _comment(
        self, body: bytes, headers: list[Header], tool: str, trace: CallTrace
    ) -> tuple[bytes, list[Header]] | None:
        """Le corps et les en-têtes relayés quand la requête SQL commence par le commentaire
        qui porte l'identifiant de run ; ``None`` quand elle ne peut pas le recevoir.

        Seule la chaîne de la requête change, et la longueur du corps avec elle. Un corps
        que des en-têtes lient (codage du contenu, empreinte, signature, arguments
        recopiés) est relayé tel quel. La requête analysée reste celle que l'agent a
        envoyée.
        """
        if any(
            name.lower() in _BODY_BOUND or name.lower().startswith(_PARAM_HEADERS)
            for name, _ in headers
        ):
            return None
        try:
            tagged = comment_query(body, self.settings.sql_tools[tool].argument, trace.run_id)
        except Exception as error:
            # L'étiquetage ne fait jamais échouer un appel : la requête part telle quelle.
            logger.error("Requête relayée sans son commentaire (%s).", type(error).__name__)
            return None
        if not isinstance(tagged, bytes):
            return None
        length = str(len(tagged)).encode("ascii")
        return tagged, [
            (name, length if name.lower() == _CONTENT_LENGTH else value) for name, value in headers
        ]

    def _inspect(
        self, method: str, headers: list[Header], body: bytes, oversized: bool
    ) -> Inspection | None:
        if method != "POST":
            return None
        inspected = [
            (name.decode("latin-1"), value.decode("latin-1"))
            for name, value in headers
            if name.lower() in _INSPECTED_HEADERS
        ]
        if oversized:
            inspection = inspect_prefix(body[:_PREFIX_WINDOW], inspected)
            if inspection is not None:
                self.stats.inspected_from_prefix += 1
        else:
            inspection = inspect_request(
                body,
                inspected,
                self._sql_arguments,
                keep_arguments=self.settings.fingerprint_key is not None,
            )
        self._inspected(inspection)
        return inspection

    async def _receive_body(
        self, receive: Receive, upload_done: asyncio.Event
    ) -> tuple[bytes, AsyncIterator[bytes] | None]:
        """Lit le corps. Au-delà de la taille lue, le reste est relayé en flux, et seul
        le début du corps est rendu."""
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                raise _ClientGoneError
            chunk = message.get("body", b"")
            chunks.append(chunk)
            size += len(chunk)
            if not message.get("more_body", False):
                upload_done.set()
                return b"".join(chunks), None
            if size > self.settings.max_inspected_bytes:
                head = _head(chunks, _PREFIX_WINDOW)
                return head, self._stream_rest(chunks, receive, upload_done)

    @staticmethod
    async def _stream_rest(
        chunks: list[bytes], receive: Receive, upload_done: asyncio.Event
    ) -> AsyncIterator[bytes]:
        for chunk in chunks:
            if chunk:
                yield chunk
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                raise _ClientGoneError
            chunk = message.get("body", b"")
            if chunk:
                yield chunk
            if not message.get("more_body", False):
                upload_done.set()
                return

    def _upstream_request(
        self,
        scope: Scope,
        headers: list[Header],
        body: bytes | None,
        stream: AsyncIterator[bytes] | None,
    ) -> httpx.Request | None:
        raw_path: bytes = scope.get("raw_path") or scope["path"].encode("utf-8")
        if not raw_path.startswith(b"/") or _has_dot_segment(raw_path):
            return None
        target = self._prefix + raw_path
        query: bytes = scope.get("query_string", b"")
        if query:
            target += b"?" + query
        forwarded = end_to_end_headers(headers)
        if not self.settings.preserve_host:
            forwarded = [(name, value) for name, value in forwarded if name.lower() != b"host"]
        content: bytes | AsyncIterator[bytes] = stream if stream is not None else (body or b"")
        try:
            return self._upstream_client().build_request(
                scope["method"],
                self.settings.upstream.copy_with(raw_path=target),
                headers=forwarded,
                content=content,
            )
        except (httpx.InvalidURL, ValueError):
            return None

    async def _exchange(
        self,
        scope: Scope,
        headers: list[Header],
        body: bytes | None,
        stream: AsyncIterator[bytes] | None,
        upload_done: asyncio.Event,
        receive: Receive,
        send: Send,
        exchange: _Exchange,
    ) -> None:
        request = self._upstream_request(scope, headers, body, stream)
        if request is None:
            exchange.error = "invalid_request_target"
            if exchange.retained is not None:
                exchange.retained.settle(reason="error_response")
            await self._answer(send, 400, _BAD_REQUEST_BODY, exchange)
            return

        relay = asyncio.ensure_future(self._relay(request, send, exchange))
        watcher = asyncio.ensure_future(self._client_disconnected(receive, upload_done))
        try:
            await asyncio.wait({relay, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if relay.done():
                relay.result()
            else:
                # Le client est parti : fermer le flux annule l'appel côté serveur.
                exchange.timing.finish()
                await _settle(relay)
                exchange.outcome = "ABORT"
                exchange.failure = "client_abort"
                self.stats.client_disconnects += 1
                if exchange.retained is not None:
                    exchange.retained.settle(reason="client_gone")
        finally:
            await _settle(watcher)
            if not relay.done():
                await _settle(relay)

    @staticmethod
    async def _client_disconnected(receive: Receive, upload_done: asyncio.Event) -> None:
        await upload_done.wait()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return

    async def _relay(self, request: httpx.Request, send: Send, exchange: _Exchange) -> None:
        """Relaie la requête et rend la réponse au fil de l'eau.

        Le temps que le proxy passe entre deux attentes, avant de relayer la
        requête et avant de rendre chaque partie de la réponse, est sa latence
        ajoutée ; les copies pour l'empreinte et l'échantillon y comptent.
        """
        timing = exchange.timing
        timing.pause()
        try:
            response = await self._upstream_client().send(request, stream=True)
        except _ClientGoneError:
            timing.finish()
            exchange.outcome = "ABORT"
            exchange.failure = "client_abort"
            self.stats.client_disconnects += 1
            if exchange.retained is not None:
                exchange.retained.settle(reason="client_gone")
            return
        except httpx.HTTPError as error:
            timing.resume()
            self.stats.upstream_failures += 1
            logger.warning("Serveur MCP injoignable (%s).", type(error).__name__)
            exchange.error = "upstream_unreachable"
            exchange.failure = _failure(error)
            if exchange.retained is not None:
                exchange.retained.settle(reason="error_response")
            await self._answer(send, 502, _BAD_GATEWAY_BODY, exchange)
            return
        timing.resume()
        try:
            exchange.status = exchange.answered = response.status_code
            if exchange.observed:
                exchange.sample = ReplySample(
                    response.headers.get("content-type"), response.headers.get("content-encoding")
                )
            start = {
                "type": "http.response.start",
                "status": response.status_code,
                "headers": [
                    (name.lower(), value)
                    for name, value in end_to_end_headers(response.headers.raw)
                ],
            }
            retained = exchange.retained
            insertion = None if retained is None else _insertion(retained, response)
            if retained is not None and insertion is not None:
                await self._relay_warned(response, start, send, exchange, retained, insertion)
                return
            timing.pause()
            await send(start)
            try:
                async for chunk in response.aiter_raw():
                    timing.resume()
                    if chunk:
                        timing.pause()
                        await send({"type": "http.response.body", "body": chunk, "more_body": True})
                        timing.resume()
                        exchange.size += len(chunk)
                        if exchange.digest is not None:
                            exchange.digest.update(chunk)
                        if exchange.sample is not None:
                            exchange.sample.add(chunk)
                    timing.pause()
            except httpx.HTTPError as error:
                # La réponse reste inachevée : le serveur HTTP ferme la connexion,
                # le client sait qu'elle est tronquée.
                timing.finish()
                self.stats.upstream_failures += 1
                logger.warning("Réponse du serveur MCP interrompue (%s).", type(error).__name__)
                exchange.error = "upstream_interrupted"
                exchange.failure = _failure(error)
                return
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            timing.finish()
            exchange.relayed_whole = exchange.delivered = True
            exchange.outcome = "COMPLETE" if response.status_code < 400 else "FAIL"
        finally:
            await response.aclose()

    async def _relay_warned(
        self,
        response: httpx.Response,
        start: Message,
        send: Send,
        exchange: _Exchange,
        retained: _Retained,
        insertion: JsonInsertion | SseInsertion,
    ) -> None:
        """Relaie la réponse d'un appel retenu, son avertissement en tête de son contenu.

        Une réponse JSON retient ses en-têtes et son début jusqu'à la place du bloc ; un flux
        SSE ne retient que la première ligne de l'événement qui porte la réponse. À la place
        trouvée, la décision est attendue au plus jusqu'à son échéance : ce temps compte
        dans la latence ajoutée. Le reste passe au fil de l'eau. Empreinte, taille et
        échantillon portent sur les octets du serveur, annotation exclue.
        """
        timing = exchange.timing
        head: Message | None = start if isinstance(insertion, JsonInsertion) else None
        # Le proxy attend le serveur : ce temps n'est pas le sien.
        timing.pause()
        if head is None:
            await send(start)
        try:
            async for chunk in response.aiter_raw():
                timing.resume()
                if chunk:
                    _account(exchange, chunk)
                    out = insertion.feed(chunk)
                    if insertion.ready:
                        out += insertion.release(await self._block(retained))
                    if insertion.concluded:
                        retained.settle(added=insertion.added, reason=insertion.reason)
                    if head is not None and not insertion.holding:
                        timing.pause()
                        await send(_lengthened(head, insertion.added))
                        timing.resume()
                        head = None
                    if out:
                        timing.pause()
                        await send({"type": "http.response.body", "body": out, "more_body": True})
                        timing.resume()
                timing.pause()
        except httpx.HTTPError as error:
            # Comme pour un autre appel, la réponse reste inachevée ; ce qui était retenu part
            # tel quel, et rien n'est inséré.
            timing.finish()
            self.stats.upstream_failures += 1
            logger.warning("Réponse du serveur MCP interrompue (%s).", type(error).__name__)
            exchange.error = "upstream_interrupted"
            exchange.failure = _failure(error)
            retained.settle(reason="not_whole")
            rest = insertion.finish()
            if head is not None:
                await send(head)
            if rest:
                await send({"type": "http.response.body", "body": rest, "more_body": True})
            return
        rest = insertion.finish()
        retained.settle(added=insertion.added, reason=insertion.reason)
        if head is not None:
            await send(head)
        if rest:
            await send({"type": "http.response.body", "body": rest, "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})
        timing.finish()
        exchange.relayed_whole = exchange.delivered = True
        exchange.outcome = "COMPLETE"

    async def _block(self, retained: _Retained) -> bytes | None:
        """Le bloc de l'avertissement, une fois la décision prise ; ``None`` hors de la zone.
        Un échec du rendu n'atteint jamais l'appel : rien n'est inséré."""
        try:
            verdict = await self._decide(retained)
            if not verdict.warns:
                return None
            return content_block(warning_text(verdict, retained.zone))
        except Exception as error:
            logger.error("Avertissement de quarantaine non rendu (%s).", type(error).__name__)
            return None

    @classmethod
    async def _decide(cls, retained: _Retained) -> Verdict:
        """La décision, attendue au plus jusqu'à son échéance, comptée depuis l'arrivée de la
        requête : la réponse d'un serveur plus lent que l'échéance n'attend pas. Sans
        analyse à temps, la décision est incertaine."""
        if retained.verdict is not None:
            return retained.verdict
        task = retained.analysis
        if task is not None and not task.done():
            remaining = retained.deadline - time.monotonic_ns()
            if remaining > 0:
                began = time.monotonic_ns()
                await asyncio.wait({task}, timeout=remaining / 1_000_000_000)
                retained.waited = time.monotonic_ns() - began
        analysis = None
        if task is not None and task.done() and not task.cancelled():
            analysis = task.result().analysis
        retained.verdict = cls._judge(retained, analysis)
        return retained.verdict

    async def _answer(self, send: Send, status: int, body: bytes, exchange: _Exchange) -> None:
        """Répond à la place du serveur MCP, avec un corps fixe."""
        exchange.timing.pause()
        await self._respond(send, status, body)
        exchange.timing.finish()
        exchange.answered = status
        exchange.delivered = True

    @staticmethod
    async def _respond(send: Send, status: int, body: bytes) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})

    def _assess(self, exchange: _Exchange) -> tuple[CallOutcome, int | None]:
        failure = exchange.failure
        if failure is None and not exchange.delivered:
            # Échange interrompu sans cause connue : la réponse n'a pas été rendue.
            failure = "transport_error"
        return assess(failure=failure, status=exchange.answered, sample=exchange.sample)
