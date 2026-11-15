"""Relais ASGI transparent devant un serveur MCP.

Le proxy relaie chaque requête HTTP à l'identique et rend la réponse du
serveur octet pour octet, en flux : il ne décompresse rien, ne réécrit rien et
ne retient aucun fragment. Seuls les en-têtes propres à une connexion (RFC 9110,
section 7.6.1) ne sont pas transmis.

À côté du relais, il lit le corps des requêtes POST pour reconnaître un appel
d'outil et émet un événement par appel, une fois l'échange terminé. Aucun
en-tête, argument ou résultat n'est conservé ni journalisé.

Seule exception à la transparence : un appel d'outil qui ne porte aucun
contexte de trace W3C reçoit celui que le proxy crée, dans un en-tête
``traceparent`` ajouté à la requête relayée, sauf si son corps est trop grand
pour être lu en entier. Le corps n'est jamais modifié.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, MutableMapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote_to_bytes

import httpx

from answerlineage_proxy.analyzer import Origin, SqlAnalyzer
from answerlineage_proxy.config import ProxySettings, SqlTool
from answerlineage_proxy.coverage import (
    DEFAULT_WINDOW,
    CoverageWindows,
    Report,
    report_periodically,
)
from answerlineage_proxy.events import Outcome, build_tool_call_event
from answerlineage_proxy.fingerprint import RESULT_LABEL, Digest, arguments_fingerprint
from answerlineage_proxy.inspection import (
    HEADER_METHOD,
    HEADER_NAME,
    HEADER_PROTOCOL_VERSION,
    Inspection,
    inspect_prefix,
    inspect_request,
)
from answerlineage_proxy.sink import EventSink
from answerlineage_proxy.sql import SqlAnalysis
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
_PREFIX_WINDOW = 16 * 1024
# Compteur de ProxyStats de chaque origine d'une analyse du SQL qui en a un.
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


@dataclass
class _Exchange:
    """Issue d'un échange relayé."""

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


class McpProxy:
    """Application ASGI placée devant un serveur MCP."""

    def __init__(
        self,
        settings: ProxySettings,
        sink: EventSink,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] = _now,
        analyzer: SqlAnalyzer | None = None,
        coverage_window: timedelta = DEFAULT_WINDOW,
    ) -> None:
        quiet_transport_logs()
        self.settings = settings
        self.stats = ProxyStats()
        # Compteurs par fenêtre, remis à la destination des événements comme relevés de
        # couverture. L'émetteur est l'espace de noms des jobs du proxy.
        self.coverage = CoverageWindows(
            f"mcp://{settings.server_name}", length=coverage_window, started_at=clock()
        )
        self._reporting: asyncio.Task[None] | None = None
        self._sink = sink
        self._client = client
        self._owns_client = client is None
        self._clock = clock
        self._prefix = settings.upstream.raw_path.rstrip(b"/")
        self._sql_arguments = {tool: rule.argument for tool, rule in settings.sql_tools.items()}
        self._guard = settings.guard()
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

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            await self._http(scope, receive, send)
        elif scope["type"] == "lifespan":
            await self._lifespan(receive, send)
        elif scope["type"] == "websocket":
            # MCP ne passe pas par WebSocket : la connexion est refusée.
            await receive()
            await send({"type": "websocket.close", "code": 1003})

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
        processus d'analyse, puis ferme les connexions vers le serveur MCP."""
        await self.drain()
        if self._reporting is not None:
            await _settle(self._reporting)
            self._reporting = None
        self._report(self.coverage.close_all(self._clock()))
        await self.analyzer.aclose()
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
                self._reporting = asyncio.ensure_future(
                    report_periodically(self.coverage, self._clock, self._report)
                )
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await self.aclose()
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _http(self, scope: Scope, receive: Receive, send: Send) -> None:
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
        try:
            body, stream = await self._receive_body(receive, upload_done)
        except _ClientGoneError:
            self.stats.client_disconnects += 1
            return

        # Au-delà de la taille lue, seul le début du corps est décrit.
        oversized = stream is not None or len(body) > self.settings.max_inspected_bytes
        inspection = self._inspect(scope["method"], headers, body, oversized)
        exchange = _Exchange()
        relayed = headers
        if inspection is not None and inspection.is_tool_call:
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
        try:
            await self._exchange(scope, relayed, body, stream, upload_done, receive, send, exchange)
        finally:
            if inspection is not None and inspection.is_tool_call:
                self._describe(inspection, exchange, oversized=oversized)

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
        if inspection is None:
            self.stats.uninspected += 1
            self.coverage.uninspected(self._clock())
        elif inspection.header_mismatches:
            self.stats.header_mismatches += 1
            # Seuls les noms des en-têtes sont journalisés, jamais leur valeur.
            logger.warning(
                "En-têtes MCP contredits par le corps de la requête : %s",
                ", ".join(inspection.header_mismatches),
            )
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
            await self._respond(send, 400, _BAD_REQUEST_BODY)
            return

        relay = asyncio.ensure_future(self._relay(request, send, exchange))
        watcher = asyncio.ensure_future(self._client_disconnected(receive, upload_done))
        try:
            await asyncio.wait({relay, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if relay.done():
                relay.result()
            else:
                # Le client est parti : fermer le flux annule l'appel côté serveur.
                await _settle(relay)
                exchange.outcome = "ABORT"
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
        try:
            response = await self._upstream_client().send(request, stream=True)
        except _ClientGoneError:
            exchange.outcome = "ABORT"
            self.stats.client_disconnects += 1
            return
        except httpx.HTTPError as error:
            self.stats.upstream_failures += 1
            logger.warning("Serveur MCP injoignable (%s).", type(error).__name__)
            exchange.error = "upstream_unreachable"
            await self._respond(send, 502, _BAD_GATEWAY_BODY)
            return
        try:
            exchange.status = response.status_code
            await send(
                {
                    "type": "http.response.start",
                    "status": response.status_code,
                    "headers": [
                        (name.lower(), value)
                        for name, value in end_to_end_headers(response.headers.raw)
                    ],
                }
            )
            try:
                async for chunk in response.aiter_raw():
                    if chunk:
                        await send({"type": "http.response.body", "body": chunk, "more_body": True})
                        exchange.size += len(chunk)
                        if exchange.digest is not None:
                            exchange.digest.update(chunk)
            except httpx.HTTPError as error:
                # La réponse reste inachevée : le serveur HTTP ferme la connexion,
                # le client sait qu'elle est tronquée.
                self.stats.upstream_failures += 1
                logger.warning("Réponse du serveur MCP interrompue (%s).", type(error).__name__)
                exchange.error = "upstream_interrupted"
                return
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            exchange.relayed_whole = True
            exchange.outcome = "COMPLETE" if response.status_code < 400 else "FAIL"
        finally:
            await response.aclose()

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

    def _describe(
        self, inspection: Inspection, exchange: _Exchange, *, oversized: bool = False
    ) -> None:
        """Émet l'événement de l'appel, après analyse du SQL si l'outil est déclaré."""
        event_time = self._clock()
        self.coverage.begin(event_time)
        rule = self.settings.sql_tools.get(inspection.tool) if inspection.tool else None
        if rule is None or exchange.outcome != "COMPLETE":
            # Un appel qui a échoué n'a rien fait lire à l'agent.
            self._emit(inspection, exchange, event_time)
            return
        if oversized:
            # La requête n'a pas été lue : sa lecture n'est pas connue.
            reads = SqlAnalysis(resolved=False, reason="request_too_large")
            self._emit(inspection, exchange, event_time, reads, rule.namespace, rule.dialect)
            return
        task = asyncio.ensure_future(
            self._analyze_then_emit(inspection, exchange, event_time, rule)
        )
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _analyze_then_emit(
        self, inspection: Inspection, exchange: _Exchange, event_time: datetime, rule: SqlTool
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
            self._emit(inspection, exchange, event_time, reads, rule.namespace, rule.dialect)

    def _emit(
        self,
        inspection: Inspection,
        exchange: _Exchange,
        event_time: datetime,
        reads: SqlAnalysis | None = None,
        namespace: str | None = None,
        dialect: str | None = None,
    ) -> None:
        """Émet l'événement de l'appel. Un échec d'émission n'atteint jamais l'appel."""
        self.stats.tool_calls += 1
        trace = exchange.trace
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
                **self._fingerprints(inspection, exchange),
            )
            self._sink.emit(event)
        except Exception as error:
            self.stats.emit_failures += 1
            self.coverage.settle(event_time, emitted=False)
            logger.error("Événement de lignage non émis (%s).", type(error).__name__)
        else:
            self.stats.events_emitted += 1
            self.coverage.settle(event_time, emitted=True)

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
