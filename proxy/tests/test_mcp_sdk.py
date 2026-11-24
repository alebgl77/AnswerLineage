"""Un vrai client et un vrai serveur MCP fonctionnent au travers du proxy.

Le SDK MCP officiel parle la révision 2026-07-28 (sans session) et, en mode
``legacy``, la révision 2025-11-25 (session, flux GET, clôture par DELETE).
Pendant une quarantaine, le vrai client reçoit l'avertissement en tête du
contenu, puis le contenu d'origine, en réponse JSON comme en flux SSE, et le
résultat structuré d'un outil qui le déclare reste valide.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from pydantic import BaseModel

from answerlineage_proxy.app import McpProxy, Message, Receive, Scope, Send
from answerlineage_proxy.config import ProxySettings
from answerlineage_proxy.quarantine import QuarantineSync
from answerlineage_proxy.rules import DeclaredDataset, ReadRule
from answerlineage_proxy.sink import MemorySink
from proxy.tests.conftest import running
from proxy.tests.test_quarantine import INCIDENT, scope
from proxy.tests.test_quarantine_relay import FixedQuarantine

pytestmark = pytest.mark.anyio

FORECAST = DeclaredDataset("meteo", "previsions.quotidiennes")
WEATHER_TOOLS = ("get_weather", "forecast", "noisy_weather")


@dataclass
class Seen:
    method: str
    headers: dict[str, str]
    # Type de contenu de la réponse du serveur : JSON ou flux SSE.
    answered: list[str] = field(default_factory=list)


@dataclass
class SdkBench:
    proxy: McpProxy
    proxy_url: str
    sink: MemorySink
    seen: list[Seen]


class Forecast(BaseModel):
    """Une prévision : le résultat structuré déclaré par l'outil."""

    ciel: str
    degres: int


@asynccontextmanager
async def sdk_bench(
    *, json_response: bool = False, quarantine: QuarantineSync | None = None
) -> AsyncIterator[SdkBench]:
    server = MCPServer("meteo")

    @server.tool()
    def get_weather(location: str) -> str:
        """Météo d'un lieu."""
        return f"Il fait beau à {location}"

    @server.tool()
    def forecast(location: str) -> Forecast:
        """Prévision d'un lieu, en résultat structuré."""
        return Forecast(ciel=f"clair à {location}", degres=14)

    @server.tool()
    async def noisy_weather(location: str, ctx: Context) -> str:
        """Météo d'un lieu, annoncée par sa progression : la réponse part en flux SSE."""
        await ctx.report_progress(1, 2, "prévision en cours")
        return f"Il pleut à {location}"

    inner = server.streamable_http_app(json_response=json_response)
    seen: list[Seen] = []

    async def recording(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await inner(scope, receive, send)
            return
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        current = Seen(scope["method"], headers)
        seen.append(current)

        async def noted(message: Message) -> None:
            if message["type"] == "http.response.start":
                answered = dict(message.get("headers", [])).get(b"content-type", b"")
                current.answered.append(answered.decode("latin-1").split(";")[0])
            await send(message)

        await inner(scope, receive, noted)

    sink = MemorySink()
    rules = tuple(ReadRule(tool=tool, datasets=(FORECAST,)) for tool in WEATHER_TOOLS)
    async with running(recording) as upstream_url:
        settings = ProxySettings.create(upstream_url, "meteo", read_rules=rules)
        proxy = McpProxy(settings, sink, quarantine=quarantine)
        async with running(proxy, transparent=True) as proxy_url:
            yield SdkBench(proxy, proxy_url, sink, seen)


def in_quarantine() -> FixedQuarantine:
    dataset = {"namespace": FORECAST.namespace, "name": FORECAST.name}
    return FixedQuarantine(scope(datasets=[dataset]))


def text_of(block: Any) -> str:
    text: str = block.text
    return text


async def test_stateless_revision_2026_07_28_works_through_the_proxy() -> None:
    async with sdk_bench() as b:
        async with Client(b.proxy_url + "/mcp", mode="auto") as client:
            result = await client.call_tool("get_weather", {"location": "Séville"})
            tools = await client.list_tools()
        assert result.content[0].text == "Il fait beau à Séville"  # type: ignore[union-attr]
        assert [tool.name for tool in tools.tools] == list(WEATHER_TOOLS)

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


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize("json_response", [False, True], ids=["sse", "json"])
async def test_the_real_client_reads_the_warning_first_then_the_original_content(
    mode: str, json_response: bool
) -> None:
    announced: list[str | None] = []

    async def noted(progress: float, total: float | None, message: str | None) -> None:
        announced.append(message)

    async with sdk_bench(json_response=json_response, quarantine=in_quarantine()) as b:
        async with Client(b.proxy_url + "/mcp", mode=mode) as client:
            weather = await client.call_tool("get_weather", {"location": "Séville"})
            structured = await client.call_tool("forecast", {"location": "Lyon"})
            noisy = await client.call_tool(
                "noisy_weather", {"location": "Brest"}, progress_callback=noted
            )
        for result in (weather, structured, noisy):
            first = result.content[0]
            warning = text_of(first)
            assert warning.startswith("[AnswerLineage] Avertissement sur la qualité des données")
            assert INCIDENT in warning
            assert "meteo previsions.quotidiennes" in warning
            annotations = getattr(first, "annotations", None)
            assert annotations is not None
            assert annotations.audience == ["assistant", "user"]
            assert not result.is_error
        assert text_of(weather.content[1]) == "Il fait beau à Séville"
        assert text_of(noisy.content[1]) == "Il pleut à Brest"
        # Le résultat structuré, validé par le client contre le schéma de sortie, est intact.
        assert structured.structured_content == {"ciel": "clair à Lyon", "degres": 14}
        assert len(structured.content) == 2

    answered = {kind for item in b.seen if item.method == "POST" for kind in item.answered}
    assert "application/json" in answered
    if not json_response:
        # La progression, en flux SSE, a traversé le proxy intacte avant la réponse annotée.
        assert "text/event-stream" in answered
        assert announced == ["prévision en cours"]
    events: list[Mapping[str, Any]] = [
        event for event in b.sink.events if event["job"]["name"] in WEATHER_TOOLS
    ]
    notes = [event["run"]["facets"]["answerlineage_toolCall"]["quarantine"] for event in events]
    assert [note["annotated"] for note in notes] == [True, True, True]
    assert {note["decision"] for note in notes} == {"certain"}
