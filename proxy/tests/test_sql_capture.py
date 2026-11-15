"""Une requête SQL qui traverse le proxy donne les tables et colonnes lues."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import statistics
import time
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

import httpx
import pytest

from answerlineage_proxy import cli
from answerlineage_proxy.analyzer import AnalyzerResult, SqlAnalyzer
from answerlineage_proxy.app import McpProxy, _Exchange
from answerlineage_proxy.config import ProxyConfigurationError, ProxySettings, SqlTool
from answerlineage_proxy.inspection import Inspection
from answerlineage_proxy.sink import MemorySink
from answerlineage_proxy.tracecontext import resolve_trace
from proxy.tests.conftest import (
    HEADER_MISMATCH,
    MODERN_HEADERS,
    TOOL_RESULT,
    BenchFactory,
    exchange,
    tool_call,
)
from proxy.tests.test_analyzer import PATHOLOGICAL, scripted, until
from proxy.tests.test_sql import BIGQUERY

pytestmark = pytest.mark.anyio

SQL_TOOLS = {"execute_sql": SqlTool(argument="query", dialect="bigquery", namespace="bigquery")}


def facet(event: Mapping[str, Any]) -> Mapping[str, Any]:
    result: Mapping[str, Any] = event["run"]["facets"]["answerlineage_toolCall"]
    return result


def columns(dataset: Mapping[str, Any]) -> list[str]:
    return [item["name"] for item in dataset["facets"]["schema"]["fields"]]


async def test_sql_crossing_the_proxy_lists_source_tables_and_columns(
    bench: BenchFactory, caplog: pytest.LogCaptureFixture
) -> None:
    body = tool_call("execute_sql", {"query": BIGQUERY, "region": "eu"})
    async with bench(sql_tools=SQL_TOOLS) as b:
        direct = await exchange("POST", b.upstream_url + "/mcp", content=body)
        with caplog.at_level(logging.DEBUG):
            proxied = await exchange("POST", b.proxy_url + "/mcp", content=body)
            await b.proxy.drain()

        # L'appel passe à l'identique : l'analyse se fait à côté.
        assert (proxied.status, proxied.body) == (direct.status, direct.body) == (200, TOOL_RESULT)
        assert b.upstream.requests[1].body == body

        (event,) = b.sink.events
        assert event["eventType"] == "COMPLETE"
        assert facet(event)["readResolution"] == "resolved"
        assert "unresolvedReason" not in facet(event)
        inputs = {dataset["name"]: dataset for dataset in event["inputs"]}
        assert {dataset["namespace"] for dataset in event["inputs"]} == {"bigquery"}
        assert sorted(inputs) == [
            "finance-prod.crm.clients",
            "finance-prod.ventes.commandes",
            "ref.taux_change",
        ]
        assert columns(inputs["ref.taux_change"]) == ["devise", "taux"]
        assert columns(inputs["finance-prod.ventes.commandes"]) == [
            "client_id",
            "date_commande",
            "montant",
        ]
        # Ni la requête ni ses littéraux n'atteignent l'événement ou les journaux.
        recorded = json.dumps(event) + caplog.text
        assert "2026-01-01" not in recorded
        assert "SELECT" not in recorded


async def test_table_read_as_a_whole_carries_no_column_list(bench: BenchFactory) -> None:
    body = tool_call("execute_sql", {"query": "SELECT * FROM ref.tarifs"})
    async with bench(sql_tools=SQL_TOOLS) as b:
        await exchange("POST", b.proxy_url + "/mcp", content=body)
        await b.proxy.drain()
        assert b.sink.events[0]["inputs"] == [{"namespace": "bigquery", "name": "ref.tarifs"}]


async def test_query_the_analyzer_does_not_understand_passes_and_is_marked_unresolved(
    bench: BenchFactory,
) -> None:
    body = tool_call("execute_sql", {"query": "SELEC nom FRM clients WHERE secret = 'canari'"})
    async with bench(sql_tools=SQL_TOOLS) as b:
        direct = await exchange("POST", b.upstream_url + "/mcp", content=body)
        proxied = await exchange("POST", b.proxy_url + "/mcp", content=body)
        await b.proxy.drain()

        assert (proxied.status, proxied.body) == (direct.status, direct.body) == (200, TOOL_RESULT)
        (event,) = b.sink.events
        assert event["eventType"] == "COMPLETE"
        assert event["inputs"] == []
        assert facet(event)["readResolution"] == "unresolved"
        assert facet(event)["unresolvedReason"] == "not_understood"
        assert "canari" not in json.dumps(event)


@pytest.mark.parametrize("arguments", [{}, {"query": 42}, {"query": None}, "pas un objet", []])
async def test_missing_sql_argument_is_marked_unresolved(
    bench: BenchFactory, arguments: object
) -> None:
    async with bench(sql_tools=SQL_TOOLS) as b:
        await exchange("POST", b.proxy_url + "/mcp", content=tool_call("execute_sql", arguments))
        await b.proxy.drain()
        (event,) = b.sink.events
        assert facet(event)["readResolution"] == "unresolved"
        assert facet(event)["unresolvedReason"] == "missing_argument"


async def test_tool_without_sql_rule_is_not_analyzed(bench: BenchFactory) -> None:
    body = tool_call("autre_outil", {"query": "SELECT a FROM t"})
    async with bench(sql_tools=SQL_TOOLS) as b:
        await exchange("POST", b.proxy_url + "/mcp", content=body)
        (event,) = b.sink.events
        assert "readResolution" not in facet(event)
        assert event["inputs"] == []


async def test_failed_call_reads_nothing(bench: BenchFactory) -> None:
    headers = [h for h in MODERN_HEADERS if h[0] != "mcp-name"] + [("mcp-name", "autre")]
    body = tool_call("execute_sql", {"query": "SELECT a FROM t"})
    async with bench(sql_tools=SQL_TOOLS) as b:
        proxied = await exchange("POST", b.proxy_url + "/mcp", headers=headers, content=body)
        await b.proxy.drain()
        assert (proxied.status, proxied.body) == (400, HEADER_MISMATCH)
        (event,) = b.sink.events
        assert event["eventType"] == "FAIL"
        assert event["inputs"] == []
        assert "readResolution" not in facet(event)


async def test_analysis_runs_beside_the_call_and_never_delays_it(bench: BenchFactory) -> None:
    """La réponse revient alors que l'analyse n'est pas terminée."""
    analyzer = SqlAnalyzer(budget=10, command=scripted())
    body = tool_call("execute_sql", {"query": "dors 1"})
    async with bench(sql_tools=SQL_TOOLS, analyzer=analyzer) as b:
        proxied = await asyncio.wait_for(
            exchange("POST", b.proxy_url + "/mcp", content=body), timeout=5
        )
        assert (proxied.status, proxied.body) == (200, TOOL_RESULT)
        await until(lambda: analyzer.analyzing)
        assert b.sink.events == []
        # Pendant l'analyse, le proxy relaie d'autres appels.
        other = await asyncio.wait_for(
            exchange("POST", b.proxy_url + "/mcp", content=tool_call()), timeout=5
        )
        assert other.status == 200
        assert analyzer.analyzing
        assert [event["job"]["name"] for event in b.sink.events] == ["get_weather"]
        await b.proxy.aclose()
        assert [event["job"]["name"] for event in b.sink.events] == ["get_weather", "execute_sql"]
        assert b.sink.events[1]["inputs"] == [{"namespace": "bigquery", "name": "s.analyse1"}]
        # L'heure de l'événement est celle de l'appel, pas celle de la fin de l'analyse.
        assert b.sink.events[1]["eventTime"] < b.sink.events[0]["eventTime"]


async def test_sql_too_large_to_inspect_is_streamed_and_leaves_an_unresolved_read(
    bench: BenchFactory,
) -> None:
    """Une requête SQL de 5 Mo passe en flux ; l'appel laisse un événement, lecture non résolue."""
    query = "SELECT a FROM s.t WHERE a IN (" + "1," * 2_500_000 + "1)"
    body = tool_call("execute_sql", {"query": query})
    assert len(body) > 5_000_000
    head = 2 * 1024 * 1024

    async with bench(sql_tools=SQL_TOOLS) as b:

        async def upload() -> AsyncIterator[bytes]:
            yield body[:head]
            # Le serveur MCP reçoit le début du corps avant que le client ait fini d'envoyer.
            await asyncio.wait_for(b.upstream.first_bytes.wait(), timeout=5)
            for start in range(head, len(body), 256 * 1024):
                yield body[start : start + 256 * 1024]

        async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
            response = await client.post(b.proxy_url + "/mcp", content=upload())
        assert (response.status_code, response.content) == (200, TOOL_RESULT)
        assert b.upstream.requests[0].body == body
        await b.proxy.drain()

        (event,) = b.sink.events
        assert event["eventType"] == "COMPLETE"
        assert event["job"]["name"] == "execute_sql"
        assert event["inputs"] == []
        assert facet(event)["readResolution"] == "unresolved"
        assert facet(event)["unresolvedReason"] == "request_too_large"
        assert (b.proxy.stats.uninspected, b.proxy.stats.inspected_from_prefix) == (0, 1)
        # La requête n'a pas été confiée au processus d'analyse.
        assert b.proxy.analyzer.process_id is None


async def test_analysis_over_budget_is_abandoned_without_affecting_the_call(
    bench: BenchFactory,
) -> None:
    body = tool_call("execute_sql", {"query": PATHOLOGICAL})
    async with bench(sql_tools=SQL_TOOLS) as b:
        direct = await exchange("POST", b.upstream_url + "/mcp", content=body)
        proxied = await exchange("POST", b.proxy_url + "/mcp", content=body)
        assert (proxied.status, proxied.body) == (direct.status, direct.body) == (200, TOOL_RESULT)
        await b.proxy.drain()

        (event,) = b.sink.events
        assert event["eventType"] == "COMPLETE"
        assert event["inputs"] == []
        assert facet(event)["readResolution"] == "unresolved"
        assert facet(event)["unresolvedReason"] == "budget_exceeded"
        assert b.proxy.stats.sql_over_budget == 1


async def test_same_query_sent_twice_is_analyzed_once(bench: BenchFactory) -> None:
    body = tool_call("execute_sql", {"query": BIGQUERY})
    async with bench(sql_tools=SQL_TOOLS) as b:
        for _ in range(2):
            await exchange("POST", b.proxy_url + "/mcp", content=body)
            await b.proxy.drain()
        first, second = b.sink.events
        assert first["inputs"] == second["inputs"] != []
        assert facet(second)["readResolution"] == "resolved"
        assert b.proxy.stats.sql_from_cache == 1


async def test_calls_are_relayed_without_delay_while_a_pathological_analysis_runs(
    bench: BenchFactory,
) -> None:
    """Comparé à une référence mesurée dans le même test : l'analyse n'occupe pas la boucle."""

    async def latencies(url: str, keep_going: Callable[[], bool], least: int) -> list[float]:
        measured: list[float] = []
        while len(measured) < least or keep_going():
            started = time.perf_counter()
            response = await exchange("POST", url, content=tool_call())
            measured.append(time.perf_counter() - started)
            assert response.status == 200
        return measured

    warm_up = tool_call("execute_sql", {"query": "SELECT 1"})
    pathological = tool_call("execute_sql", {"query": PATHOLOGICAL})
    async with bench(sql_tools=SQL_TOOLS, sql_budget_ms=1500) as b:
        analyzer = b.proxy.analyzer
        # Le processus d'analyse est démarré avant la mesure de référence.
        await exchange("POST", b.proxy_url + "/mcp", content=warm_up)
        await b.proxy.drain()
        reference = await latencies(b.proxy_url + "/mcp", lambda: False, least=20)

        await exchange("POST", b.proxy_url + "/mcp", content=pathological)
        await until(lambda: analyzer.analyzing)
        during = await latencies(
            b.proxy_url + "/mcp", lambda: b.proxy.stats.sql_over_budget == 0, least=20
        )
        await b.proxy.drain()
        assert b.proxy.stats.sql_over_budget == 1

        # Marges larges : la machine peut être chargée, mais une analyse qui disputerait
        # l'interpréteur à la boucle multiplierait la latence bien au-delà.
        assert statistics.median(during) < 3 * statistics.median(reference) + 0.02
        # Aucun appel n'attend l'analyse, qui court jusqu'à son budget de 1,5 s.
        assert max(during) < 0.5


async def test_event_is_emitted_even_when_the_analysis_process_cannot_start(
    bench: BenchFactory,
) -> None:
    analyzer = SqlAnalyzer(command=["/chemin/inexistant/python"])
    body = tool_call("execute_sql", {"query": "SELECT a FROM s.t"})
    async with bench(sql_tools=SQL_TOOLS, analyzer=analyzer) as b:
        proxied = await exchange("POST", b.proxy_url + "/mcp", content=body)
        await b.proxy.drain()
        assert (proxied.status, proxied.body) == (200, TOOL_RESULT)
        (event,) = b.sink.events
        assert facet(event)["unresolvedReason"] == "analyzer_unavailable"
        assert b.proxy.stats.sql_analyzer_failures == 1


async def test_event_is_emitted_even_when_the_analysis_fails_unexpectedly(
    bench: BenchFactory, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(*args: object, **options: object) -> AnalyzerResult:
        raise RuntimeError("panne canari")

    body = tool_call("execute_sql", {"query": "SELECT a FROM s.t"})
    async with bench(sql_tools=SQL_TOOLS) as b:
        monkeypatch.setattr(b.proxy.analyzer, "analyze", broken)
        with caplog.at_level(logging.DEBUG):
            await exchange("POST", b.proxy_url + "/mcp", content=body)
            await b.proxy.drain()
        (event,) = b.sink.events
        assert facet(event)["unresolvedReason"] == "analyzer_failed"
        assert b.proxy.stats.sql_analyzer_failures == 1
        assert b.proxy.stats.events_emitted == 1
        assert "RuntimeError" in caplog.text
        assert "canari" not in caplog.text


async def test_event_is_emitted_when_waiting_for_the_analysis_is_cancelled() -> None:
    analyzer = SqlAnalyzer(budget=10, command=scripted())
    sink = MemorySink()
    settings = ProxySettings.create("http://127.0.0.1:1", "meteo", sql_tools=SQL_TOOLS)
    proxy = McpProxy(settings, sink, analyzer=analyzer)
    inspection = Inspection(is_tool_call=True, tool="execute_sql", sql="dors 10")
    trace = resolve_trace(None, [])
    proxy._describe(inspection, _Exchange(outcome="COMPLETE", status=200, trace=trace))
    await until(lambda: analyzer.analyzing)
    for task in list(proxy._pending):
        task.cancel()
    await proxy.drain()
    (event,) = sink.events
    assert facet(event)["unresolvedReason"] == "analyzer_unavailable"
    await proxy.aclose()


async def test_closing_the_proxy_leaves_no_analysis_process(bench: BenchFactory) -> None:
    body = tool_call("execute_sql", {"query": "SELECT a FROM s.t"})
    async with bench(sql_tools=SQL_TOOLS) as b:
        await exchange("POST", b.proxy_url + "/mcp", content=body)
        await b.proxy.drain()
        pid = b.proxy.analyzer.process_id
        assert pid is not None
    # L'arrêt du serveur HTTP ferme le proxy (lifespan) : son processus d'analyse s'arrête.
    assert b.proxy.analyzer.process_id is None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_waiting_for_analyses_that_already_finished_returns_at_once() -> None:
    """Attendre des tâches déjà terminées ne rend pas la main à la boucle : l'attente
    ne doit pas dépendre de leur rappel de fin pour se terminer."""
    proxy = McpProxy(ProxySettings.create("http://127.0.0.1:1", "meteo"), MemorySink())

    async def finished() -> None:
        return None

    task = asyncio.ensure_future(finished())
    await task
    proxy._pending.add(task)
    await asyncio.wait_for(proxy.drain(), timeout=2)
    assert proxy._pending == set()


def test_sql_tool_rule_is_read_from_its_compact_form() -> None:
    assert SqlTool.parse("execute_sql=query:bigquery") == (
        "execute_sql",
        SqlTool(argument="query", dialect="bigquery", namespace="bigquery"),
    )
    assert SqlTool.parse("run=sql:postgres:postgres://db.interne:5432") == (
        "run",
        SqlTool(argument="sql", dialect="postgres", namespace="postgres://db.interne:5432"),
    )


@pytest.mark.parametrize(
    "value",
    [
        "execute_sql",
        "execute_sql=query",
        "=query:bigquery",
        "outil invalide=query:bigquery",
        "execute_sql=query:oracle",
        "execute_sql=arg invalide:bigquery",
        "execute_sql=query:bigquery:espace invalide",
        "execute_sql=:bigquery",
    ],
)
def test_invalid_sql_tool_rule_is_refused(value: str) -> None:
    with pytest.raises(ProxyConfigurationError):
        SqlTool.parse(value)


def test_settings_refuse_an_invalid_sql_tool_name() -> None:
    with pytest.raises(ProxyConfigurationError):
        ProxySettings.create(
            "http://127.0.0.1:9000", "m", sql_tools={"nom invalide": SQL_TOOLS["execute_sql"]}
        )


def test_command_line_declares_sql_tools() -> None:
    settings = cli.settings_from_args(
        [
            "--upstream",
            "http://127.0.0.1:9000",
            "--sql-tool",
            "execute_sql=query:bigquery",
            "--sql-tool",
            "run=sql:snowflake:snowflake://compte",
        ],
        {},
    )
    assert settings.sql_tools == {
        "execute_sql": SqlTool(argument="query", dialect="bigquery", namespace="bigquery"),
        "run": SqlTool(argument="sql", dialect="snowflake", namespace="snowflake://compte"),
    }
    assert cli.settings_from_args(["--upstream", "http://127.0.0.1:9000"], {}).sql_tools == {}


def test_analysis_settings_have_bounded_defaults() -> None:
    settings = ProxySettings.create("http://127.0.0.1:9000", "m")
    assert (settings.sql_budget_ms, settings.sql_queue_size, settings.sql_cache_size) == (
        50,
        32,
        1024,
    )


@pytest.mark.parametrize(
    ("budget", "queue", "cache"),
    [
        (0, 32, 1024),
        (60_001, 32, 1024),
        (50, 0, 1024),
        (50, 10_001, 1024),
        (50, 32, -1),
        (50, 32, 1_000_001),
    ],
)
def test_out_of_range_analysis_settings_are_refused(budget: int, queue: int, cache: int) -> None:
    with pytest.raises(ProxyConfigurationError, match="entre"):
        ProxySettings.create(
            "http://127.0.0.1:9000",
            "m",
            sql_budget_ms=budget,
            sql_queue_size=queue,
            sql_cache_size=cache,
        )


def test_analysis_settings_come_from_the_command_line_or_the_environment() -> None:
    environment = {
        "ANSWERLINEAGE_PROXY_SQL_BUDGET_MS": "80",
        "ANSWERLINEAGE_PROXY_SQL_QUEUE_SIZE": "8",
        "ANSWERLINEAGE_PROXY_SQL_CACHE_SIZE": "0",
    }
    from_env = cli.settings_from_args(["--upstream", "http://127.0.0.1:9000"], environment)
    assert (from_env.sql_budget_ms, from_env.sql_queue_size, from_env.sql_cache_size) == (80, 8, 0)
    from_settings = ProxySettings.from_environment(
        {"ANSWERLINEAGE_PROXY_UPSTREAM": "http://127.0.0.1:9000", **environment}
    )
    assert (from_settings.sql_budget_ms, from_settings.sql_queue_size) == (80, 8)
    overridden = cli.settings_from_args(
        [
            "--upstream",
            "http://127.0.0.1:9000",
            "--sql-budget-ms",
            "120",
            "--sql-queue-size",
            "4",
            "--sql-cache-size",
            "16",
        ],
        environment,
    )
    assert (overridden.sql_budget_ms, overridden.sql_queue_size, overridden.sql_cache_size) == (
        120,
        4,
        16,
    )
    with pytest.raises(ProxyConfigurationError, match="ANSWERLINEAGE_PROXY_SQL_BUDGET_MS"):
        cli.settings_from_args(
            ["--upstream", "http://h"], {"ANSWERLINEAGE_PROXY_SQL_BUDGET_MS": "vite"}
        )


def test_proxy_analyzer_follows_the_settings() -> None:
    settings = ProxySettings.create(
        "http://127.0.0.1:9000", "m", sql_budget_ms=75, sql_queue_size=3, sql_cache_size=5
    )
    analyzer = McpProxy(settings, MemorySink()).analyzer
    assert (analyzer.budget, analyzer.queue_size, analyzer.cache_size) == (0.075, 3, 5)
