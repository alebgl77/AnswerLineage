"""Chaque appel porte sa latence, son issue, ses reprises et la latence ajoutée par le proxy.

Les signaux sont observés sur de vrais échanges HTTP, à travers le proxy, devant
un serveur MCP de test qui réussit, répond une erreur JSON-RPC ou un statut 500,
coupe sa réponse ou se tait ; et devant un client qui abandonne.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from answerlineage_proxy.app import McpProxy
from answerlineage_proxy.config import ProxySettings, SqlTool
from answerlineage_proxy.events import QOS_FACET_SCHEMA
from answerlineage_proxy.fingerprint import FingerprintKey
from answerlineage_proxy.signals import RetryKey, RetryMemory
from answerlineage_proxy.sink import MemorySink
from proxy.tests.conftest import (
    JSONRPC_ERROR,
    SSE_FIRST,
    BenchFactory,
    FakeMcpServer,
    exchange,
    running,
    tool_call,
)
from proxy.tests.test_relay import eventually, http_scope, run_asgi

pytestmark = pytest.mark.anyio

KEY = FingerprintKey(bytes(range(32)))
JSON = [("content-type", "application/json")]


def qos(event: Mapping[str, Any]) -> dict[str, Any]:
    facet: dict[str, Any] = event["run"]["facets"]["answerlineage_qos"]
    return facet


def call_facet(event: Mapping[str, Any]) -> dict[str, Any]:
    facet: dict[str, Any] = event["run"]["facets"]["answerlineage_toolCall"]
    return facet


async def test_a_call_carries_its_latency_and_the_latency_the_proxy_added(
    bench: BenchFactory,
) -> None:
    async with bench() as b:
        started = time.monotonic()
        proxied = await exchange("POST", b.proxy_url + "/mcp", headers=JSON, content=tool_call())
        seen_by_the_client = (time.monotonic() - started) * 1000
        (event,) = b.sink.events
    assert proxied.status == 200
    signals = qos(event)
    assert signals["_schemaURL"] == QOS_FACET_SCHEMA
    assert signals["outcome"] == "success"
    assert signals["errorCount"] == 0
    assert "errorCode" not in signals
    # Sans clé d'empreinte, une reprise ne se reconnaît pas : le nombre est absent, pas nul.
    assert "retryCount" not in signals
    assert 0 < signals["addedLatencyMs"] < signals["latencyMs"] < seen_by_the_client


async def test_the_wait_for_the_server_is_not_latency_added_by_the_proxy(
    bench: BenchFactory,
) -> None:
    """Le serveur garde le flux SSE ouvert 300 ms : l'échange dure, le proxy n'y ajoute rien."""
    async with bench() as b, httpx.AsyncClient(trust_env=False, timeout=5) as client:
        request = client.build_request("POST", b.proxy_url + "/sse", content=tool_call())
        response = await client.send(request, stream=True)
        try:
            chunks = response.aiter_raw()
            assert await anext(chunks) == SSE_FIRST
            await asyncio.sleep(0.3)
            b.upstream.gate.set()
            async for _ in chunks:
                pass
        finally:
            await response.aclose()
        await eventually(lambda: len(b.sink.events) == 1)
        signals = qos(b.sink.events[0])
    assert signals["latencyMs"] >= 300
    assert signals["addedLatencyMs"] < 50
    assert signals["outcome"] == "success"


@pytest.mark.parametrize("path", ["/mcp", "/sse-error"])
async def test_a_json_rpc_error_is_said_by_its_code_never_by_its_message(
    bench: BenchFactory, caplog: pytest.LogCaptureFixture, path: str
) -> None:
    """En JSON comme en flux SSE ; le message, qui cite un argument, ne sort nulle part."""
    async with bench() as b:
        b.upstream.tool_result = JSONRPC_ERROR
        with caplog.at_level(logging.DEBUG):
            proxied = await exchange("POST", b.proxy_url + path, headers=JSON, content=tool_call())
        await eventually(lambda: len(b.sink.events) == 1)
        (event,) = b.sink.events
    assert proxied.status == 200
    assert JSONRPC_ERROR in proxied.body
    signals = qos(event)
    assert (signals["outcome"], signals["errorCode"], signals["errorCount"]) == (
        "jsonrpc_error",
        -32602,
        1,
    )
    # L'état du run reste celui du statut HTTP : le graphe ne change pas.
    assert event["eventType"] == "COMPLETE"
    recorded = json.dumps(event, ensure_ascii=False) + caplog.text
    for canary in ("canari", "Lieu inconnu", "Séville"):
        assert canary not in recorded


async def test_an_http_error_status_is_said_by_its_status(bench: BenchFactory) -> None:
    async with bench() as b:
        proxied = await exchange("POST", b.proxy_url + "/fail", content=tool_call())
        (event,) = b.sink.events
    assert proxied.status == 500
    assert event["eventType"] == "FAIL"
    signals = qos(event)
    assert (signals["outcome"], signals["errorCode"], signals["errorCount"]) == (
        "http_error",
        500,
        1,
    )


async def test_a_response_cut_by_the_server_is_a_transport_failure(bench: BenchFactory) -> None:
    async with bench() as b:
        with pytest.raises(httpx.RemoteProtocolError):
            await exchange("POST", b.proxy_url + "/truncated", content=tool_call())
        await eventually(lambda: len(b.sink.events) == 1)
        signals = qos(b.sink.events[0])
    assert (signals["outcome"], signals["errorCount"]) == ("transport_error", 1)
    assert "errorCode" not in signals


async def test_an_unreachable_server_is_a_transport_failure() -> None:
    sink = MemorySink()
    proxy = McpProxy(ProxySettings.create("http://127.0.0.1:1", "meteo"), sink)
    async with running(proxy, transparent=True) as proxy_url:
        proxied = await exchange("POST", proxy_url + "/mcp", content=tool_call())
    assert proxied.status == 502
    signals = qos(sink.events[0])
    assert (signals["outcome"], signals["errorCount"]) == ("transport_error", 1)
    assert "errorCode" not in signals


@asynccontextmanager
async def impatient_proxy(
    read_timeout: float,
) -> AsyncIterator[tuple[FakeMcpServer, str, MemorySink]]:
    """Un proxy dont le client HTTP abandonne un serveur MCP muet après ``read_timeout``."""
    upstream = FakeMcpServer()
    sink = MemorySink()
    async with (
        running(upstream) as upstream_url,
        httpx.AsyncClient(
            trust_env=False, timeout=httpx.Timeout(None, read=read_timeout)
        ) as client,
    ):
        client.headers.clear()
        proxy = McpProxy(ProxySettings.create(upstream_url, "meteo"), sink, client=client)
        async with running(proxy, transparent=True) as proxy_url:
            yield upstream, proxy_url, sink


async def test_a_server_that_answers_nothing_in_time_is_a_timeout() -> None:
    async with impatient_proxy(0.2) as (_, proxy_url, sink):
        proxied = await exchange("POST", proxy_url + "/silent", content=tool_call())
        (event,) = sink.events
    assert proxied.status == 502
    assert call_facet(event)["proxyError"] == "upstream_unreachable"
    signals = qos(event)
    assert (signals["outcome"], signals["errorCount"]) == ("timeout", 1)
    assert signals["latencyMs"] >= 200


async def test_a_server_that_stops_in_the_middle_of_its_response_is_a_timeout() -> None:
    async with impatient_proxy(0.2) as (_, proxy_url, sink):
        with pytest.raises(httpx.RemoteProtocolError):
            await exchange("POST", proxy_url + "/hang", content=tool_call())
        await eventually(lambda: len(sink.events) == 1)
        (event,) = sink.events
    assert call_facet(event)["proxyError"] == "upstream_interrupted"
    assert qos(event)["outcome"] == "timeout"


async def test_a_call_the_client_abandons_is_not_an_error(bench: BenchFactory) -> None:
    async with bench() as b:
        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            request = client.build_request("POST", b.proxy_url + "/hang", content=tool_call())
            response = await client.send(request, stream=True)
            assert await anext(response.aiter_raw()) == SSE_FIRST
            await response.aclose()
        await eventually(lambda: len(b.sink.events) == 1)
        (event,) = b.sink.events
    assert event["eventType"] == "ABORT"
    signals = qos(event)
    assert (signals["outcome"], signals["errorCount"]) == ("client_abort", 0)


async def test_a_request_the_proxy_refuses_is_said_by_the_status_it_answered() -> None:
    sink = MemorySink()
    proxy = McpProxy(ProxySettings.create("http://127.0.0.1:1", "meteo"), sink)
    sent = await run_asgi(
        proxy, http_scope(b"*"), [{"type": "http.request", "body": tool_call(), "more_body": False}]
    )
    assert sent[0]["status"] == 400
    signals = qos(sink.events[0])
    assert (signals["outcome"], signals["errorCode"]) == ("http_error", 400)
    await proxy.aclose()


async def outcomes_and_retries(
    proxy_url: str, sink: MemorySink, calls: list[tuple[str, list[tuple[str, str]], bytes]]
) -> list[tuple[str, int | None]]:
    for path, headers, body in calls:
        await exchange("POST", proxy_url + path, headers=headers, content=body)
    return [(qos(event)["outcome"], qos(event).get("retryCount")) for event in sink.drain()]


async def test_retries_of_a_failed_call_are_counted_until_it_succeeds(bench: BenchFactory) -> None:
    async with bench(fingerprint_key=KEY) as b:
        assert await outcomes_and_retries(
            b.proxy_url,
            b.sink,
            [
                ("/fail", [], tool_call()),
                ("/fail", [], tool_call()),
                ("/mcp", [], tool_call()),
                # Le succès clôt la chaîne : le même appel ensuite n'est pas une reprise.
                ("/mcp", [], tool_call()),
            ],
        ) == [("http_error", 0), ("http_error", 1), ("success", 2), ("success", 0)]


async def test_a_retry_has_the_same_tool_the_same_arguments_and_the_same_scope(
    bench: BenchFactory,
) -> None:
    session_a = [("mcp-session-id", "canari-session-a-77d1")]
    session_b = [("mcp-session-id", "canari-session-b-0c4e")]
    other_trace = [("traceparent", "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01")]
    async with bench(fingerprint_key=KEY) as b:
        assert await outcomes_and_retries(
            b.proxy_url,
            b.sink,
            [
                ("/fail", session_a, tool_call()),
                # Autre session, autres arguments, autre outil : d'autres chaînes.
                ("/fail", session_b, tool_call()),
                ("/fail", session_a, tool_call(arguments={"location": "Lyon"})),
                ("/fail", session_a, tool_call("get_forecast")),
                ("/fail", other_trace, tool_call()),
                # La reprise dans la première session.
                ("/mcp", session_a, tool_call()),
            ],
        ) == [
            ("http_error", 0),
            ("http_error", 0),
            ("http_error", 0),
            ("http_error", 0),
            ("http_error", 0),
            ("success", 1),
        ]


async def test_without_a_fingerprint_of_the_arguments_retries_are_unknown(
    bench: BenchFactory,
) -> None:
    async with bench() as b:
        unkeyed = await outcomes_and_retries(
            b.proxy_url, b.sink, [("/fail", [], tool_call()), ("/fail", [], tool_call())]
        )
    assert unkeyed == [("http_error", None), ("http_error", None)]
    # Un corps trop grand pour être lu : ses arguments n'ont pas d'empreinte.
    async with bench(fingerprint_key=KEY, max_inspected_bytes=100) as b:
        oversized = tool_call() + b" " * 1000
        unread = await outcomes_and_retries(
            b.proxy_url, b.sink, [("/fail", [], oversized), ("/fail", [], oversized)]
        )
    assert unread == [("http_error", None), ("http_error", None)]


async def test_signals_carry_nothing_of_the_session_the_arguments_or_the_error(
    bench: BenchFactory, caplog: pytest.LogCaptureFixture
) -> None:
    headers = [("mcp-session-id", "canari-session-91aa"), *JSON]
    async with bench(fingerprint_key=KEY) as b:
        b.upstream.tool_result = JSONRPC_ERROR
        with caplog.at_level(logging.DEBUG):
            for path in ("/fail", "/mcp", "/sse-error"):
                body = tool_call(arguments={"location": "canari-argument-3f0b"})
                await exchange("POST", b.proxy_url + path, headers=headers, content=body)
        await eventually(lambda: len(b.sink.events) == 3)
        recorded = json.dumps(b.sink.events, ensure_ascii=False) + caplog.text
    assert [qos(event)["retryCount"] for event in b.sink.events] == [0, 1, 2]
    assert "canari" not in recorded
    assert "Lieu inconnu" not in recorded


async def test_a_sql_call_carries_its_signals_once_analysed(bench: BenchFactory) -> None:
    tools = {"execute_sql": SqlTool("query", "bigquery", "bigquery")}
    body = tool_call("execute_sql", {"query": "SELECT c.nom FROM crm.clients c"})
    async with bench(sql_tools=tools, sql_budget_ms=60_000) as b:
        await exchange("POST", b.proxy_url + "/mcp", headers=JSON, content=body)
        await b.proxy.drain()
        (event,) = b.sink.events
    assert event["inputs"][0]["name"] == "crm.clients"
    assert qos(event)["outcome"] == "success"


async def test_signals_that_cannot_be_established_never_reach_the_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class BrokenMemory(RetryMemory):
        def record(
            self, key: RetryKey | None, *, started: int, ended: int, failed: bool
        ) -> int | None:
            raise RuntimeError("mémoire en panne")

    upstream = FakeMcpServer()
    sink = MemorySink()
    async with running(upstream) as upstream_url:
        settings = ProxySettings.create(upstream_url, "meteo")
        proxy = McpProxy(settings, sink, retries=BrokenMemory())
        async with running(proxy, transparent=True) as proxy_url:
            with caplog.at_level(logging.ERROR, logger="answerlineage_proxy"):
                proxied = await exchange("POST", proxy_url + "/mcp", content=tool_call())
    assert proxied.status == 200
    (event,) = sink.events
    assert "answerlineage_qos" not in event["run"]["facets"]
    assert "Signaux de l'appel non établis (RuntimeError)" in caplog.text
    assert proxy.stats.events_emitted == 1
