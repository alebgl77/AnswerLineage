"""Relais ASGI transparent devant un serveur MCP (US-0401, EF-CAP-01).

Le proxy relaie chaque requête HTTP à l'identique et rend la réponse du
serveur octet pour octet, en flux : il ne décompresse rien, ne réécrit rien et
ne retient aucun fragment. Seuls les en-têtes propres à une connexion (RFC 9110,
section 7.6.1) ne sont pas transmis.

À côté du relais, il lit le corps des requêtes POST pour reconnaître un appel
d'outil et émet un événement par appel, une fois l'échange terminé. Aucun
en-tête, argument ou résultat n'est conservé ni journalisé (ADR-004,
ENF-SEC-05).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, MutableMapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx

from answerlineage_proxy.config import ProxySettings
from answerlineage_proxy.events import Outcome, build_tool_call_event
from answerlineage_proxy.inspection import (
    HEADER_METHOD,
    HEADER_NAME,
    HEADER_PROTOCOL_VERSION,
    Inspection,
    inspect_request,
)
from answerlineage_proxy.sink import EventSink

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
_BAD_GATEWAY_BODY = b'{"error":"upstream_unreachable"}'
_BAD_REQUEST_BODY = b'{"error":"invalid_request_target"}'


# httpx journalise l'URL complète de chaque requête, paramètres compris, et
# httpcore les en-têtes des réponses en mode débogage.
_TRANSPORT_LOGGERS = ("httpx", "httpcore")


def quiet_transport_logs() -> None:
    """Relève le niveau des journaux du client HTTP, qui citeraient URL et en-têtes.

    Le réglage vaut pour tout le processus et résiste à un niveau DEBUG posé
    sur le journal racine (ENF-SEC-05).
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


@dataclass
class _Exchange:
    """Issue d'un échange relayé."""

    outcome: Outcome = "FAIL"
    status: int | None = None
    error: str | None = None


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
        new_run_id: Callable[[], UUID] = uuid4,
    ) -> None:
        quiet_transport_logs()
        self.settings = settings
        self.stats = ProxyStats()
        self._sink = sink
        self._client = client
        self._owns_client = client is None
        self._clock = clock
        # L'identifiant de run sera dérivé du contexte de trace W3C par US-0403.
        self._new_run_id = new_run_id
        self._prefix = settings.upstream.raw_path.rstrip(b"/")

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
        """Ferme les connexions vers le serveur MCP."""
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
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await self.aclose()
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _http(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.stats.relayed += 1
        headers: list[Header] = [(bytes(name), bytes(value)) for name, value in scope["headers"]]
        upload_done = asyncio.Event()
        try:
            body, stream = await self._receive_body(receive, upload_done)
        except _ClientGoneError:
            self.stats.client_disconnects += 1
            return

        inspection = self._inspect(scope["method"], headers, body)
        exchange = _Exchange()
        try:
            await self._exchange(scope, headers, body, stream, upload_done, receive, send, exchange)
        finally:
            if inspection is not None and inspection.is_tool_call:
                self._emit(inspection, exchange)

    def _inspect(self, method: str, headers: list[Header], body: bytes | None) -> Inspection | None:
        if method != "POST":
            return None
        if body is None or len(body) > self.settings.max_inspected_bytes:
            self.stats.uninspected += 1
            return None
        inspected = [
            (name.decode("latin-1"), value.decode("latin-1"))
            for name, value in headers
            if name.lower() in _INSPECTED_HEADERS
        ]
        inspection = inspect_request(body, inspected)
        if inspection is None:
            self.stats.uninspected += 1
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
    ) -> tuple[bytes | None, AsyncIterator[bytes] | None]:
        """Lit le corps. Au-delà de la taille lue, le reste est relayé en flux."""
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
                return None, self._stream_rest(chunks, receive, upload_done)

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
        if not raw_path.startswith(b"/"):
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
            except httpx.HTTPError as error:
                # La réponse reste inachevée : le serveur HTTP ferme la connexion,
                # le client sait qu'elle est tronquée.
                self.stats.upstream_failures += 1
                logger.warning("Réponse du serveur MCP interrompue (%s).", type(error).__name__)
                exchange.error = "upstream_interrupted"
                return
            await send({"type": "http.response.body", "body": b"", "more_body": False})
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

    def _emit(self, inspection: Inspection, exchange: _Exchange) -> None:
        """Émet l'événement de l'appel. Un échec d'émission n'atteint jamais l'appel."""
        self.stats.tool_calls += 1
        try:
            event = build_tool_call_event(
                run_id=self._new_run_id(),
                event_time=self._clock(),
                outcome=exchange.outcome,
                server=self.settings.server_name,
                inspection=inspection,
                http_status=exchange.status,
                proxy_error=exchange.error,
            )
            self._sink.emit(event)
        except Exception as error:
            self.stats.emit_failures += 1
            logger.error("Événement de lignage non émis (%s).", type(error).__name__)
        else:
            self.stats.events_emitted += 1
