"""US-0401 : un vrai client et un vrai serveur MCP fonctionnent au travers du proxy.

Le SDK MCP officiel parle la révision 2026-07-28 (sans session) et, en mode
``legacy``, la révision 2025-11-25 (session, flux GET, clôture par DELETE).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest
from mcp import Client
from mcp.server import MCPServer

from answerlineage_proxy.app import McpProxy, Receive, Scope, Send
from answerlineage_proxy.config import ProxySettings
from answerlineage_proxy.sink import MemorySink
from proxy.tests.conftest import running

pytestmark = pytest.mark.anyio


@dataclass
class Seen:
    method: str
    headers: dict[str, str]


@dataclass
class SdkBench:
    proxy: McpProxy
    proxy_url: str
    sink: MemorySink
    seen: list[Seen]


@asynccontextmanager
async def sdk_bench() -> AsyncIterator[SdkBench]:
    server = MCPServer("meteo")

    @server.tool()
    def get_weather(location: str) -> str:
        """Météo d'un lieu."""
        return f"Il fait beau à {location}"

    inner = server.streamable_http_app()
    seen: list[Seen] = []

    async def recording(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
            seen.append(Seen(scope["method"], headers))
        await inner(scope, receive, send)

    sink = MemorySink()
    async with running(recording) as upstream_url:
        proxy = McpProxy(ProxySettings.create(upstream_url, "meteo"), sink)
        async with running(proxy, transparent=True) as proxy_url:
            yield SdkBench(proxy, proxy_url, sink, seen)


async def test_stateless_revision_2026_07_28_works_through_the_proxy() -> None:
    async with sdk_bench() as b:
        async with Client(b.proxy_url + "/mcp", mode="auto") as client:
            result = await client.call_tool("get_weather", {"location": "Séville"})
            tools = await client.list_tools()
        assert result.content[0].text == "Il fait beau à Séville"  # type: ignore[union-attr]
        assert [tool.name for tool in tools.tools] == ["get_weather"]

        assert {s.headers.get("mcp-protocol-version") for s in b.seen} == {"2026-07-28"}
        assert all("mcp-session-id" not in s.headers for s in b.seen)
        assert {s.method for s in b.seen} == {"POST"}

        (event,) = b.sink.events
        assert event["eventType"] == "COMPLETE"
        assert event["job"] == {"namespace": "mcp://meteo", "name": "get_weather"}
        facet = event["run"]["facets"]["answerlineage_toolCall"]
        assert facet["protocolVersion"] == "2026-07-28"
        assert facet["headerMismatch"] == []
        assert b.proxy.stats.uninspected == 0


async def test_session_revision_2025_11_25_works_through_the_proxy() -> None:
    async with sdk_bench() as b:
        async with Client(b.proxy_url + "/mcp", mode="legacy") as client:
            first = await client.call_tool("get_weather", {"location": "Lyon"})
            second = await client.call_tool("get_weather", {"location": "Nantes"})
        assert first.content[0].text == "Il fait beau à Lyon"  # type: ignore[union-attr]
        assert second.content[0].text == "Il fait beau à Nantes"  # type: ignore[union-attr]

        # La session s'ouvre, se poursuit et se clôt au travers du proxy.
        sessions = {s.headers["mcp-session-id"] for s in b.seen if "mcp-session-id" in s.headers}
        assert len(sessions) == 1
        assert {"POST", "DELETE"} <= {s.method for s in b.seen}
        versions = {s.headers.get("mcp-protocol-version") for s in b.seen} - {None}
        assert versions == {"2025-11-25"}

        events = b.sink.events
        assert [event["job"]["name"] for event in events] == ["get_weather", "get_weather"]
        assert {event["eventType"] for event in events} == {"COMPLETE"}
        facets = [event["run"]["facets"]["answerlineage_toolCall"] for event in events]
        assert {facet["protocolVersion"] for facet in facets} == {"2025-11-25"}
        assert len({event["run"]["runId"] for event in events}) == 2
