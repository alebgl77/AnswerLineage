"""Outils de test du proxy : serveurs HTTP réels sur la boucle locale, faux serveur MCP, et
client d'un proxy stdio devant le serveur MCP de test en stdio."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from answerlineage_proxy.analyzer import SqlAnalyzer
from answerlineage_proxy.app import McpProxy, Message, Receive, Scope, Send
from answerlineage_proxy.config import ProxySettings, SqlTool, StdioSettings
from answerlineage_proxy.fingerprint import FingerprintKey
from answerlineage_proxy.rules import ReadRule
from answerlineage_proxy.sink import MemorySink
from answerlineage_proxy.stdio import StdioProxy

TOOL_RESULT = (
    b'{"jsonrpc":"2.0","id":1,\n  "result":{"content":[{"type":"text","text":'
    b'"Il fait beau \xc3\xa0 S\xc3\xa9ville"}],"isError":false,"resultType":"complete"}}\n'
)
SSE_FIRST = b'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
SSE_LAST = b'event: message\r\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\r\n\r\n'
HEADER_MISMATCH = b'{"jsonrpc":"2.0","id":1,"error":{"code":-32020,"message":"Header mismatch"}}'
# Erreur JSON-RPC dont le message cite un argument : il ne doit sortir nulle part.
JSONRPC_ERROR = (
    b'{"jsonrpc":"2.0","id":1,"error":{"code":-32602,'
    b'"message":"Lieu inconnu : canari-message-erreur-5e1d"}}'
)
SSE_ERROR = b"event: message\ndata: " + JSONRPC_ERROR + b"\n\n"
# Défi d'un serveur MCP protégé : il désigne ses métadonnées OAuth (RFC 9728).
CHALLENGE = 'Bearer resource_metadata="https://mcp.exemple.fr/.well-known/oauth-protected-resource"'
UNAUTHORIZED = b'{"error":"invalid_token"}'
# Résultat volumineux, rendu en morceaux de 64 Ko.
LARGE_RESULT = bytes(range(256)) * (32 * 1024)
# Serveur MCP de test en stdio, lancé comme un vrai serveur.
STDIO_SERVER = Path(__file__).with_name("stdio_server.py")


def stdio_server(*options: str) -> list[str]:
    """La commande qui lance le serveur MCP de test en stdio, avec ses options."""
    return [sys.executable, str(STDIO_SERVER), *options]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@asynccontextmanager
async def running(app: Any, *, transparent: bool = False) -> AsyncIterator[str]:
    """Sert une application ASGI sur un port libre de la boucle locale."""
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        log_level="warning",
        access_log=False,
        # Mêmes réglages que la ligne de commande du proxy.
        server_header=not transparent,
        date_header=not transparent,
        timeout_graceful_shutdown=1,
    )
    server = uvicorn.Server(config)
    task = asyncio.ensure_future(server.serve())
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.005)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


@dataclass
class Received:
    method: str
    raw_path: bytes
    query: bytes
    headers: list[tuple[str, str]]
    body: bytes

    def header(self, name: str) -> list[str]:
        return [value for key, value in self.headers if key == name]


@dataclass
class FakeMcpServer:
    """Serveur HTTP qui enregistre ce qu'il reçoit et répond selon le chemin."""

    requests: list[Received] = field(default_factory=list)
    # Résultat rendu à un appel d'outil : réglable, pour y placer un canari.
    tool_result: bytes = TOOL_RESULT
    gate: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    # Premiers octets d'un corps reçus : la requête est relayée avant d'être complète.
    first_bytes: asyncio.Event = field(default_factory=asyncio.Event)
    aborted: int = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        body = b""
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                # Requête interrompue avant la fin de son corps : elle n'est pas traitée.
                self.aborted += 1
                return
            body += message.get("body", b"")
            if body:
                self.first_bytes.set()
            if not message.get("more_body", False):
                break
        received = Received(
            method=scope["method"],
            raw_path=scope["raw_path"],
            query=scope["query_string"],
            headers=[(k.decode("latin-1"), v.decode("latin-1")) for k, v in scope["headers"]],
            body=body,
        )
        self.requests.append(received)
        path = scope["path"]
        if path.endswith("/sse"):
            await self._sse(send)
        elif path.endswith("/sse-error"):
            await _respond(
                send, 200, SSE_FIRST + SSE_ERROR, [(b"content-type", b"text/event-stream")]
            )
        elif path.endswith("/fail"):
            await _respond(send, 500, JSONRPC_ERROR, [(b"content-type", b"application/json")])
        elif path.endswith("/silent"):
            # Ne répond rien, pas même ses en-têtes, jusqu'au départ du client.
            while (await receive())["type"] != "http.disconnect":
                pass
            self.cancelled.set()
        elif path.endswith("/hang"):
            await self._hang(receive, send)
        elif path.endswith("/gzip"):
            await _respond(
                send,
                200,
                gzip.compress(TOOL_RESULT, mtime=0),
                [(b"content-type", b"application/json"), (b"content-encoding", b"gzip")],
            )
        elif path.endswith("/large"):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            for start in range(0, len(LARGE_RESULT), 64 * 1024):
                chunk = LARGE_RESULT[start : start + 64 * 1024]
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        elif path.endswith("/echo"):
            digest = hashlib.sha256(body).hexdigest()
            await _respond(send, 200, json.dumps({"sha256": digest, "size": len(body)}).encode())
        elif path.endswith("/unauthorized"):
            await _respond(
                send,
                401,
                UNAUTHORIZED,
                [(b"www-authenticate", CHALLENGE.encode()), (b"content-type", b"application/json")],
            )
        elif path.endswith("/redirect"):
            await _respond(send, 307, b"", [(b"location", b"http://example.invalid/ailleurs")])
        elif path.endswith("/truncated"):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", b"100"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b'{"jsonrpc"', "more_body": False})
        elif received.method == "POST":
            await self._mcp(received, send)
        elif received.method == "GET":
            await _respond(send, 200, SSE_FIRST, [(b"content-type", b"text/event-stream")])
        else:
            await _respond(send, 200, b"")

    async def _mcp(self, received: Received, send: Send) -> None:
        try:
            message = json.loads(received.body)
        except ValueError:
            await _respond(send, 400, b'{"jsonrpc":"2.0","error":{"code":-32700,"message":"x"}}')
            return
        params = message.get("params", {}) if isinstance(message, dict) else {}
        names = received.header("mcp-name")
        # Un serveur conforme à la révision 2026-07-28 refuse un en-tête contredit par le corps.
        if names and not names[0].startswith("=?base64?") and names != [params.get("name")]:
            await _respond(send, 400, HEADER_MISMATCH)
            return
        await _respond(
            send,
            200,
            self.tool_result,
            [
                (b"content-type", b"application/json"),
                (b"x-duplique", b"un"),
                (b"x-duplique", b"deux"),
                (b"cache-control", b"no-store"),
            ],
        )

    async def _sse(self, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream"), (b"x-accel-buffering", b"no")],
            }
        )
        await send({"type": "http.response.body", "body": SSE_FIRST, "more_body": True})
        await self.gate.wait()
        await send({"type": "http.response.body", "body": SSE_LAST, "more_body": False})

    async def _hang(self, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        await send({"type": "http.response.body", "body": SSE_FIRST, "more_body": True})
        while True:
            message: Message = await receive()
            if message["type"] == "http.disconnect":
                self.cancelled.set()
                return


async def _respond(
    send: Send, status: int, body: bytes, headers: list[tuple[bytes, bytes]] | None = None
) -> None:
    all_headers = [*(headers or []), (b"content-length", str(len(body)).encode())]
    await send({"type": "http.response.start", "status": status, "headers": all_headers})
    await send({"type": "http.response.body", "body": body, "more_body": False})


@dataclass
class Bench:
    """Un faux serveur MCP, un proxy devant lui et la file des événements émis."""

    upstream: FakeMcpServer
    upstream_url: str
    proxy: McpProxy
    proxy_url: str
    sink: MemorySink


BenchFactory = Callable[..., AbstractAsyncContextManager[Bench]]


@pytest.fixture
def bench() -> BenchFactory:
    @asynccontextmanager
    async def start(
        *,
        prefix: str = "",
        preserve_host: bool = True,
        max_inspected_bytes: int = 1024 * 1024,
        sql_tools: dict[str, SqlTool] | None = None,
        allowed_hosts: tuple[str, ...] = (),
        allowed_origins: tuple[str, ...] = (),
        sql_budget_ms: int = 50,
        analyzer: SqlAnalyzer | None = None,
        fingerprint_key: FingerprintKey | None = None,
        read_rules: tuple[ReadRule, ...] = (),
    ) -> AsyncIterator[Bench]:
        upstream = FakeMcpServer()
        sink = MemorySink()
        async with running(upstream) as upstream_url:
            settings = ProxySettings.create(
                upstream_url + prefix,
                "meteo",
                preserve_host=preserve_host,
                max_inspected_bytes=max_inspected_bytes,
                sql_tools=sql_tools,
                allowed_hosts=allowed_hosts,
                allowed_origins=allowed_origins,
                sql_budget_ms=sql_budget_ms,
                fingerprint_key=fingerprint_key,
                read_rules=read_rules,
            )
            proxy = McpProxy(settings, sink, analyzer=analyzer)
            async with running(proxy, transparent=True) as proxy_url:
                yield Bench(upstream, upstream_url, proxy, proxy_url, sink)

    return start


@dataclass
class Exchange:
    status: int
    headers: list[tuple[str, str]]
    body: bytes

    def header(self, name: str) -> list[str]:
        return [value for key, value in self.headers if key == name]


_CONNECTION_HEADERS = {"connection", "keep-alive", "transfer-encoding"}


async def exchange(
    method: str,
    url: str,
    *,
    headers: list[tuple[str, str]] | None = None,
    content: bytes | None = None,
) -> Exchange:
    """Échange HTTP brut : aucun en-tête ajouté, corps lu sans décompression."""
    async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
        client.headers.clear()
        request = client.build_request(method, url, headers=headers, content=content)
        response = await client.send(request, stream=True)
        try:
            body = b"".join([chunk async for chunk in response.aiter_raw()])
        finally:
            await response.aclose()
        # L'ordre des en-têtes est conservé : le relais ne doit pas le changer.
        end_to_end = [
            (name.decode("latin-1").lower(), value.decode("latin-1"))
            for name, value in response.headers.raw
            if name.decode("latin-1").lower() not in _CONNECTION_HEADERS
        ]
        return Exchange(response.status_code, end_to_end, body)


def tool_call(name: object = "get_weather", arguments: object = None, **meta: object) -> bytes:
    """Corps d'un appel d'outil, avec une mise en forme volontairement irrégulière."""
    if arguments is None:
        arguments = {"location": "Séville, ES"}
    params: dict[str, Any] = {"name": name, "arguments": arguments}
    if meta:
        params["_meta"] = meta
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
    return json.dumps(message, ensure_ascii=False, indent=1).encode("utf-8")


MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}
MODERN_HEADERS = [
    ("content-type", "application/json"),
    ("accept", "application/json, text/event-stream"),
    ("mcp-protocol-version", "2026-07-28"),
    ("mcp-method", "tools/call"),
    ("mcp-name", "get_weather"),
]

# Le mode stdio : un client devant le proxy, lui-même devant le serveur de test.

STDIO_KEY = FingerprintKey(bytes(range(32)))


def stdio_message(
    identifier: object, method: str, params: Mapping[str, Any] | None = None
) -> bytes:
    """Une ligne JSON-RPC, telle qu'un client stdio l'écrit."""
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": identifier, "method": method}
    if params is not None:
        body["params"] = params
    return json.dumps(body, ensure_ascii=False).encode() + b"\n"


def stdio_call(identifier: object, tool: str, arguments: Mapping[str, Any], **meta: str) -> bytes:
    params: dict[str, Any] = {"name": tool, "arguments": dict(arguments)}
    if meta:
        params["_meta"] = meta
    return stdio_message(identifier, "tools/call", params)


def stdio_notification(method: str, params: Mapping[str, Any] | None = None) -> bytes:
    body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        body["params"] = params
    return json.dumps(body).encode() + b"\n"


INITIALIZE = stdio_message(
    0,
    "initialize",
    {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "client", "version": "1"},
    },
)


class ClientReader(threading.Thread):
    """Le client lit la sortie du proxy jusqu'à sa fin, sans bloquer la boucle."""

    def __init__(self, fd: int) -> None:
        super().__init__(daemon=True)
        self.fd = fd
        self.data = bytearray()

    def run(self) -> None:
        while chunk := os.read(self.fd, 65536):
            self.data += chunk


async def until(condition: Any, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition jamais atteinte"
        await asyncio.sleep(0.01)


@dataclass
class StdioSession:
    """Un client devant le proxy, lui-même devant le serveur de test."""

    proxy: StdioProxy
    sink: MemorySink
    record: Path
    to_proxy: int
    reader: ClientReader
    task: asyncio.Future[int]
    closed: bool = False

    async def send(self, data: bytes) -> None:
        await asyncio.to_thread(write_all, self.to_proxy, data)

    async def replies(self, count: int) -> list[bytes]:
        await until(lambda: self.reader.data.count(b"\n") >= count)
        return bytes(self.reader.data).splitlines(keepends=True)

    async def close(self) -> int:
        """Le client ferme son entrée ; le proxy s'arrête avec le serveur."""
        if not self.closed:
            self.closed = True
            os.close(self.to_proxy)
        return await self.finished()

    async def finished(self) -> int:
        """Attend que le proxy s'arrête de lui-même, avec le serveur ; rend son code."""
        code = await asyncio.wait_for(self.task, timeout=20)
        await asyncio.to_thread(self.reader.join, 10)
        return code

    @property
    def received(self) -> bytes:
        """Ce que le serveur a lu sur son entrée."""
        return (self.record / "recu").read_bytes() if (self.record / "recu").exists() else b""

    @property
    def written(self) -> bytes:
        """Ce que le serveur a écrit sur sa sortie."""
        return (self.record / "ecrit").read_bytes() if (self.record / "ecrit").exists() else b""

    @property
    def events(self) -> list[Mapping[str, Any]]:
        return self.sink.events


def write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


@asynccontextmanager
async def stdio_session(
    tmp_path: Path,
    *options: str,
    key: FingerprintKey | None = STDIO_KEY,
    sql_tools: dict[str, SqlTool] | None = None,
    max_inspected_bytes: int = 1024 * 1024,
    **proxy_options: Any,
) -> AsyncIterator[StdioSession]:
    """Lance le proxy dans le processus du test, devant le serveur de test qu'il lance.

    Le serveur note dans ``tmp_path/echanges`` les octets qu'il lit et ceux qu'il
    écrit ; le client écrit dans un tube l'entrée du proxy et lit sa sortie dans un
    autre. Les signaux ne sont transmis que si le test le demande.
    """
    record = tmp_path / "echanges"
    record.mkdir(exist_ok=True)
    settings = StdioSettings.create(
        stdio_server("--record", str(record), *options),
        "meteo",
        fingerprint_key=key,
        sql_tools=sql_tools,
        max_inspected_bytes=max_inspected_bytes,
        sql_budget_ms=60_000,
    )
    client_input, to_proxy = os.pipe()
    from_proxy, client_output = os.pipe()
    sink = MemorySink()
    proxy_options.setdefault("forwarded_signals", ())
    proxy = StdioProxy(
        settings, sink, input_fd=client_input, output_fd=client_output, **proxy_options
    )
    reader = ClientReader(from_proxy)
    reader.start()
    session = StdioSession(
        proxy, sink, record, to_proxy, reader, asyncio.ensure_future(proxy.run())
    )
    try:
        yield session
    finally:
        if not session.closed:
            session.closed = True
            os.close(to_proxy)
        if not session.task.done():
            await asyncio.wait_for(session.task, timeout=20)
        await asyncio.to_thread(reader.join, 10)
        for fd in (client_input, client_output, from_proxy):
            os.close(fd)
