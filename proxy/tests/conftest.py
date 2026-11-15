"""Outils de test du proxy : serveurs HTTP réels sur la boucle locale et faux serveur MCP."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import uvicorn

from answerlineage_proxy.app import McpProxy, Message, Receive, Scope, Send
from answerlineage_proxy.config import ProxySettings, SqlTool
from answerlineage_proxy.sink import MemorySink

TOOL_RESULT = (
    b'{"jsonrpc":"2.0","id":1,\n  "result":{"content":[{"type":"text","text":'
    b'"Il fait beau \xc3\xa0 S\xc3\xa9ville"}],"isError":false,"resultType":"complete"}}\n'
)
SSE_FIRST = b'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
SSE_LAST = b'event: message\r\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\r\n\r\n'
HEADER_MISMATCH = b'{"jsonrpc":"2.0","id":1,"error":{"code":-32020,"message":"Header mismatch"}}'


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
    gate: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
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
        elif path.endswith("/hang"):
            await self._hang(receive, send)
        elif path.endswith("/gzip"):
            await _respond(
                send,
                200,
                gzip.compress(TOOL_RESULT, mtime=0),
                [(b"content-type", b"application/json"), (b"content-encoding", b"gzip")],
            )
        elif path.endswith("/echo"):
            digest = hashlib.sha256(body).hexdigest()
            await _respond(send, 200, json.dumps({"sha256": digest, "size": len(body)}).encode())
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
            TOOL_RESULT,
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
            )
            proxy = McpProxy(settings, sink)
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
