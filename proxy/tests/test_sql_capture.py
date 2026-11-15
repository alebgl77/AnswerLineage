"""Une requête SQL qui traverse le proxy donne les tables et colonnes lues."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Mapping
from typing import Any

import pytest

from answerlineage_proxy import cli
from answerlineage_proxy.app import McpProxy
from answerlineage_proxy.config import ProxyConfigurationError, ProxySettings, SqlTool
from answerlineage_proxy.sink import MemorySink
from answerlineage_proxy.sql import SqlAnalysis, TableRead
from proxy.tests.conftest import (
    HEADER_MISMATCH,
    MODERN_HEADERS,
    TOOL_RESULT,
    BenchFactory,
    exchange,
    tool_call,
)
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


async def test_analysis_runs_beside_the_call_and_never_delays_it(
    bench: BenchFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La réponse revient alors que l'analyse n'est pas terminée."""
    release = threading.Event()
    started = threading.Event()

    def slow_analysis(sql: str, dialect: str, **options: object) -> SqlAnalysis:
        started.set()
        release.wait(timeout=10)
        return SqlAnalysis(resolved=True, tables=(TableRead("s.t", "table"),))

    monkeypatch.setattr("answerlineage_proxy.app.analyze_sql", slow_analysis)
    body = tool_call("execute_sql", {"query": "SELECT a FROM s.t"})
    async with bench(sql_tools=SQL_TOOLS) as b:
        try:
            proxied = await asyncio.wait_for(
                exchange("POST", b.proxy_url + "/mcp", content=body), timeout=5
            )
            assert (proxied.status, proxied.body) == (200, TOOL_RESULT)
            await asyncio.to_thread(started.wait, 5)
            assert b.sink.events == []
            # Pendant l'analyse, le proxy relaie d'autres appels.
            other = await asyncio.wait_for(
                exchange("POST", b.proxy_url + "/mcp", content=tool_call()), timeout=5
            )
            assert other.status == 200
            assert [event["job"]["name"] for event in b.sink.events] == ["get_weather"]
        finally:
            release.set()
        await b.proxy.aclose()
        assert [event["job"]["name"] for event in b.sink.events] == ["get_weather", "execute_sql"]
        assert b.sink.events[1]["inputs"] == [{"namespace": "bigquery", "name": "s.t"}]
        # L'heure de l'événement est celle de l'appel, pas celle de la fin de l'analyse.
        assert b.sink.events[1]["eventTime"] < b.sink.events[0]["eventTime"]


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
