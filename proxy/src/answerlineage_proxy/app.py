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
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, MutableMapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from urllib.parse import unquote_to_bytes

import httpx

from answerlineage_proxy.analyzer import Origin, SqlAnalyzer
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
from answerlineage_proxy.prefix import PREFIX_BYTES
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
    ) -> None:
        self.settings = settings
        self.stats = ProxyStats()
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

    async def drain(self) -> None:
        """Attend la fin des analyses en cours, pour que leurs événements soient émis."""
        while self._pending:
            tasks = list(self._pending)
            await asyncio.gather(*tasks, return_exceptions=True)
            # Retrait explicite : attendre des tâches déjà terminées ne rend pas la
            # main à la boucle, et leur rappel de fin n'aurait pas encore tourné.
            self._pending.difference_update(tasks)

    async def aclose(self) -> None:
        """Termine les analyses en cours, remet la dernière fenêtre de couverture, arrête le
        processus d'analyse, puis vide la destination à remise différée dans son délai."""
        await self.drain()
        if self._reporting is not None:
            await _settle(self._reporting)
            self._reporting = None
        self._report(self.coverage.close_all(self._clock()))
        await self.analyzer.aclose()
        if self._deferred is not None:
            await self._deferred.aclose()

    async def _start_reporting(self) -> None:
        """Démarre la destination à remise différée, puis remet chaque fenêtre de couverture
        close, hors du chemin des appels."""
        if self._deferred is not None:
            await self._deferred.start()
        self._reporting = asyncio.ensure_future(
            report_periodically(self.coverage, self._clock, self._report)
        )

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
        if rule is None:
            try:
                reads, declared = self._without_sql(inspection, exchange)
            except Exception as error:
                logger.error("Lectures de l'appel non établies (%s).", type(error).__name__)
                reads, declared = None, None
            if declared is not None:
                details = {**details, "declared": declared}
            self._emit(inspection, exchange, event_time, details, reads)
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
        try:
            if inspection.sql is None:
                reads = SqlAnalysis(resolved=False, reason="missing_argument")
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
            self._emit(
                inspection, exchange, event_time, details, reads, rule.namespace, rule.dialect
            )

    def _emit(
        self,
        inspection: Inspection,
        exchange: _Exchange,
        event_time: datetime,
        details: dict[str, Any],
        reads: SqlAnalysis | None = None,
        namespace: str | None = None,
        dialect: str | None = None,
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
    ) -> None:
        quiet_transport_logs()
        super().__init__(
            settings,
            sink,
            clock=clock,
            analyzer=analyzer,
            coverage_window=coverage_window,
            retries=retries,
        )
        self._client = client
        self._owns_client = client is None
        self._prefix = settings.upstream.raw_path.rstrip(b"/")
        self._guard = settings.guard()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            await self._http(scope, receive, send)
        elif scope["type"] == "lifespan":
            await self._lifespan(receive, send)
        elif scope["type"] == "websocket":
            # MCP ne passe pas par WebSocket : la connexion est refusée.
            await receive()
            await send({"type": "websocket.close", "code": 1003})

    async def aclose(self) -> None:
        """Termine les analyses en cours, remet la dernière fenêtre de couverture, arrête le
        processus d'analyse, vide la destination à remise différée dans son délai, puis ferme
        les connexions vers le serveur MCP."""
        await super().aclose()
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

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
        try:
            await self._exchange(scope, relayed, body, stream, upload_done, receive, send, exchange)
        finally:
            timing.finish()
            if inspection is not None and inspection.is_tool_call:
                self._describe(inspection, exchange, oversized=oversized)

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
            return
        except httpx.HTTPError as error:
            timing.resume()
            self.stats.upstream_failures += 1
            logger.warning("Serveur MCP injoignable (%s).", type(error).__name__)
            exchange.error = "upstream_unreachable"
            exchange.failure = _failure(error)
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
