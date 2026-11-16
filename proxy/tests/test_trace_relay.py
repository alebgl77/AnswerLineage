"""L'identifiant de run d'un appel vient de son contexte de trace W3C, reçu ou créé par le proxy."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from answerlineage_proxy.app import McpProxy
from answerlineage_proxy.config import ProxySettings
from answerlineage_proxy.sink import MemorySink
from answerlineage_proxy.tracecontext import derive_run_id, parse_traceparent
from proxy.tests.conftest import (
    MODERN_HEADERS,
    MODERN_META,
    BenchFactory,
    FakeMcpServer,
    exchange,
    running,
    tool_call,
)
from proxy.tests.test_relay import http_scope, recording_client, run_asgi

pytestmark = pytest.mark.anyio

# Contextes reconnaissables : on les cherche ensuite dans les journaux.
IN_META = "00-c0ffee00c0ffee00c0ffee00c0ffee01-badc0ffee0ddf00d-01"
IN_HEADER = "00-feedfacefeedfacefeedfacefeedface-0123456789abcdef-01"


def facet(event: Mapping[str, Any]) -> Mapping[str, Any]:
    result: Mapping[str, Any] = event["run"]["facets"]["answerlineage_toolCall"]
    return result


def run_id_of(traceparent: str) -> str:
    context = parse_traceparent(traceparent)
    assert context is not None
    return str(derive_run_id(context))


async def test_a_call_without_context_receives_one_created_by_the_proxy(
    bench: BenchFactory,
) -> None:
    body = tool_call(**MODERN_META)
    async with bench() as b:
        await exchange("POST", b.upstream_url + "/mcp", headers=MODERN_HEADERS, content=body)
        await exchange("POST", b.proxy_url + "/mcp", headers=MODERN_HEADERS, content=body)
        direct, proxied = b.upstream.requests
        (event,) = b.sink.events

    # Le corps relayé est identique à l'octet près ; seul l'en-tête traceparent s'ajoute.
    assert proxied.body == direct.body == body
    (created,) = proxied.header("traceparent")
    added = [h for h in proxied.headers if h[0] != "host"]
    assert added == [*(h for h in direct.headers if h[0] != "host"), ("traceparent", created)]
    assert created.startswith("00-") and created.endswith("-00")
    assert event["run"]["runId"] == run_id_of(created)
    context = parse_traceparent(created)
    assert context is not None
    assert facet(event)["traceOrigin"] == "proxy"
    assert (facet(event)["traceId"], facet(event)["spanId"]) == (
        context.trace_id,
        context.parent_id,
    )


async def test_the_context_of_the_tool_call_in_meta_gives_the_run_id(bench: BenchFactory) -> None:
    body = tool_call(**MODERN_META, traceparent=IN_META)
    async with bench() as b:
        await exchange(
            "POST",
            b.proxy_url + "/mcp",
            headers=[*MODERN_HEADERS, ("traceparent", IN_HEADER)],
            content=body,
        )
        (seen,) = b.upstream.requests
        (event,) = b.sink.events

    # _meta l'emporte sur l'en-tête, qui n'est ni remplacé ni doublé.
    assert seen.header("traceparent") == [IN_HEADER]
    assert seen.body == body
    assert event["run"]["runId"] == run_id_of(IN_META)
    assert facet(event)["traceOrigin"] == "meta"
    assert facet(event)["traceId"] == "c0ffee00c0ffee00c0ffee00c0ffee01"
    assert facet(event)["spanId"] == "badc0ffee0ddf00d"


async def test_the_http_header_alone_gives_the_run_id(bench: BenchFactory) -> None:
    async with bench() as b:
        await exchange(
            "POST",
            b.proxy_url + "/mcp",
            headers=[*MODERN_HEADERS, ("traceparent", IN_HEADER)],
            content=tool_call(**MODERN_META),
        )
        (seen,) = b.upstream.requests
        (event,) = b.sink.events
    assert seen.header("traceparent") == [IN_HEADER]
    assert event["run"]["runId"] == run_id_of(IN_HEADER)
    assert facet(event)["traceOrigin"] == "header"


@pytest.mark.parametrize(
    "meta", [{"traceparent": "ff-" + IN_META[3:]}, {"traceparent": 7}, {}], ids=str
)
async def test_a_malformed_header_is_relayed_untouched_and_nothing_is_added(
    bench: BenchFactory, meta: dict[str, Any]
) -> None:
    malformed = "00-PAS-UN-CONTEXTE"
    async with bench() as b:
        await exchange(
            "POST",
            b.proxy_url + "/mcp",
            headers=[("traceparent", malformed)],
            content=tool_call(**meta),
        )
        (seen,) = b.upstream.requests
        (event,) = b.sink.events
    assert seen.header("traceparent") == [malformed]
    assert facet(event)["traceOrigin"] == "proxy"
    assert malformed not in json.dumps(event)
    assert "PAS-UN" not in json.dumps(event)


async def test_requests_that_are_not_tool_calls_receive_no_context(bench: BenchFactory) -> None:
    listing = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).encode()
    async with bench(max_inspected_bytes=1000) as b:
        await exchange("POST", b.proxy_url + "/mcp", content=listing)
        await exchange("GET", b.proxy_url + "/mcp")
        await exchange("POST", b.proxy_url + "/echo", content=tool_call() + b" " * 2000)
        await exchange("POST", b.proxy_url + "/mcp", content=b'{"method": "tools/call"')
        assert [r.header("traceparent") for r in b.upstream.requests] == [[], [], [], []]


async def test_two_proxies_that_see_the_same_context_emit_for_the_same_run() -> None:
    body = tool_call(**MODERN_META, traceparent=IN_META)
    run_ids = []
    for _ in range(2):
        seen: list[httpx.Request] = []
        sink = MemorySink()
        async with recording_client(seen) as client:
            proxy = McpProxy(
                ProxySettings.create("https://amont.exemple", "meteo"), sink, client=client
            )
            await run_asgi(
                proxy, http_scope(), [{"type": "http.request", "body": body, "more_body": False}]
            )
        (event,) = sink.events
        run_ids.append(event["run"]["runId"])
    assert run_ids == [run_id_of(IN_META)] * 2


async def test_proxies_in_series_emit_for_the_same_run_when_the_first_creates_the_context() -> None:
    upstream = FakeMcpServer()
    first, second = MemorySink(), MemorySink()
    async with running(upstream) as upstream_url:
        inner = McpProxy(ProxySettings.create(upstream_url, "meteo"), second)
        async with running(inner, transparent=True) as inner_url:
            outer = McpProxy(ProxySettings.create(inner_url, "meteo"), first)
            async with running(outer, transparent=True) as outer_url:
                await exchange("POST", outer_url + "/mcp", content=tool_call())
    (received,) = upstream.requests
    (created,) = received.header("traceparent")
    assert first.events[0]["run"]["runId"] == second.events[0]["run"]["runId"] == run_id_of(created)
    assert facet(first.events[0])["traceOrigin"] == "proxy"
    assert facet(second.events[0])["traceOrigin"] == "header"


async def test_different_contexts_give_different_runs(bench: BenchFactory) -> None:
    async with bench() as b:
        for traceparent in (IN_META, IN_HEADER):
            await exchange("POST", b.proxy_url + "/mcp", content=tool_call(traceparent=traceparent))
        await exchange("POST", b.proxy_url + "/mcp", content=tool_call())
        await exchange("POST", b.proxy_url + "/mcp", content=tool_call())
        assert len({event["run"]["runId"] for event in b.sink.events}) == 4


async def test_a_received_context_never_reaches_the_logs(
    bench: BenchFactory, caplog: pytest.LogCaptureFixture
) -> None:
    async with bench() as b:
        with caplog.at_level(logging.DEBUG):
            await exchange(
                "POST",
                b.proxy_url + "/mcp",
                headers=[("traceparent", IN_HEADER), ("mcp-name", "autre")],
                content=tool_call(traceparent=IN_META),
            )
            await exchange(
                "POST",
                b.proxy_url + "/mcp",
                headers=[("traceparent", "00-canari-mal-forme")],
                content=tool_call(traceparent="ff-canari-meta"),
            )
            await exchange("POST", b.proxy_url + "/mcp", content=tool_call())
        created = b.upstream.requests[2].header("traceparent")[0]
    assert "mcp-name" in caplog.text
    for canary in ("c0ffee", "badc0ffee", "feedface", "0123456789abcdef", "canari"):
        assert canary not in caplog.text
    assert created[3:35] not in caplog.text
