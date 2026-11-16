"""Sur option, la requête SQL relayée commence par le commentaire qui porte l'identifiant de run.

Le serveur MCP de test reçoit la requête : seul le commentaire diffère de ce que l'agent a
envoyé, octet pour octet, en HTTP comme en stdio. Un corps que le proxy ne lit pas avec
certitude part tel quel, et il est compté ; un outil sans l'option n'est jamais touché.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from answerlineage_proxy import app, cli, stdio
from answerlineage_proxy.config import ProxyConfigurationError, SqlTool
from answerlineage_proxy.tracecontext import derive_run_id, parse_traceparent
from answerlineage_proxy.warehouse import warehouse_tags
from proxy.tests.conftest import (
    MODERN_HEADERS,
    BenchFactory,
    Received,
    exchange,
    stdio_call,
    stdio_session,
    tool_call,
)

pytestmark = pytest.mark.anyio

SQL_TOOLS = {
    "execute_sql": SqlTool("query", "bigquery", "bigquery", run_comment=True),
    "autre_sql": SqlTool("query", "bigquery", "bigquery"),
}
QUERY = "SELECT c.nom FROM `finance-prod.crm.clients` AS c WHERE c.ville = 'Séville'"
HEADERS = [(name, value) for name, value in MODERN_HEADERS if name != "mcp-name"]
IN_META = "00-c0ffee00c0ffee00c0ffee00c0ffee01-badc0ffee0ddf00d-01"
TRACE_ID, PARENT_ID = "4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7"
COMMENT = re.compile(r"/\* answerlineage_run_id=([0-9a-f-]{36}) \*/ ")
TAGGED_SCHEMA = "/spec/facets/1-2-0/ToolCallRunFacet.json#/$defs/ToolCallRunFacet"


def facet(event: Mapping[str, Any]) -> Mapping[str, Any]:
    result: Mapping[str, Any] = event["run"]["facets"]["answerlineage_toolCall"]
    return result


def run_of(traceparent: str) -> str:
    context = parse_traceparent(traceparent)
    assert context is not None
    return str(derive_run_id(context))


def commented(body: bytes, run: str) -> bytes:
    """Le corps envoyé, dont la requête commence par le commentaire du run : rien d'autre."""
    start = body.index(b'"SELECT') + 1
    return body[:start] + f"/* answerlineage_run_id={run} */ ".encode() + body[start:]


def query_of(body: bytes) -> str:
    query: str = json.loads(body)["params"]["arguments"]["query"]
    return query


def without(received: Received, *names: str) -> list[tuple[str, str]]:
    return [(name, value) for name, value in received.headers if name not in names]


# En HTTP.


async def test_only_the_comment_of_the_run_differs_in_what_the_server_receives(
    bench: BenchFactory,
) -> None:
    body = tool_call("execute_sql", {"query": QUERY, "region": "eu"}, traceparent=IN_META)
    async with bench(sql_tools=SQL_TOOLS) as b:
        direct = await exchange("POST", b.upstream_url + "/mcp", headers=HEADERS, content=body)
        proxied = await exchange("POST", b.proxy_url + "/mcp", headers=HEADERS, content=body)
        await b.proxy.drain()
        sent, received = b.upstream.requests
        (event,) = b.sink.events

    run = event["run"]["runId"]
    assert run == run_of(IN_META)
    assert sent.body == body
    assert received.body == commented(body, run)
    # Les en-têtes sont ceux du client, dans leur ordre ; seule la longueur suit le corps.
    assert without(received, "host", "content-length") == without(sent, "host", "content-length")
    assert received.header("content-length") == [str(len(received.body))]
    assert (proxied.status, proxied.body) == (direct.status, direct.body)

    # L'événement le dit, sans rien de la requête ; l'analyse porte sur la requête d'origine.
    assert facet(event)["taggedWithRunId"] is True
    assert facet(event)["_schemaURL"].endswith(TAGGED_SCHEMA)
    assert [dataset["name"] for dataset in event["inputs"]] == ["finance-prod.crm.clients"]
    assert "answerlineage_run_id" not in json.dumps(event)
    stats = b.proxy.stats
    assert (stats.tagged_queries, stats.untagged_queries) == (1, 0)

    # Le serveur qui lit le contexte de trace reçu retrouve le run du commentaire.
    meta = json.loads(received.body)["params"]["_meta"]
    tags = warehouse_tags(meta, received.header("traceparent"))
    assert tags is not None
    assert tags.run_id == run
    assert query_of(received.body) == tags.sql_comment + QUERY


async def test_a_call_without_context_carries_the_run_of_the_context_the_proxy_creates(
    bench: BenchFactory,
) -> None:
    body = tool_call("execute_sql", {"query": QUERY})
    async with bench(sql_tools=SQL_TOOLS) as b:
        await exchange("POST", b.proxy_url + "/mcp", headers=HEADERS, content=body)
        await b.proxy.drain()
        (received,) = b.upstream.requests
        (event,) = b.sink.events
    (created,) = received.header("traceparent")
    run = event["run"]["runId"]
    assert run == run_of(created)
    assert received.body == commented(body, run)
    tags = warehouse_tags(None, received.header("traceparent"))
    assert tags is not None
    assert tags.run_id == run


@pytest.mark.parametrize(
    ("meta", "header", "origin"),
    [
        # Mal formé : ignoré, le proxy crée le contexte.
        ({"traceparent": f"00-{TRACE_ID}-{PARENT_ID}-01 */ DROP TABLE crm.clients; --"}, [], None),
        ({"traceparent": "*/ DROP TABLE crm.clients; /*"}, [], None),
        # Version future bien formée : seuls trace-id et parent-id comptent.
        (
            {},
            [("traceparent", f"cc-{TRACE_ID}-{PARENT_ID}-01-*/ DROP TABLE crm.clients; /*")],
            "header",
        ),
        (
            {"traceparent": f"cc-{TRACE_ID}-{PARENT_ID}-01-'); DROP TABLE crm.clients; --"},
            [],
            "meta",
        ),
    ],
    ids=["meta-version-00", "meta-texte", "en-tete-version-future", "meta-version-future"],
)
async def test_a_trapped_trace_context_never_reaches_the_query(
    bench: BenchFactory,
    meta: dict[str, str],
    header: list[tuple[str, str]],
    origin: str | None,
) -> None:
    body = tool_call("execute_sql", {"query": QUERY}, **meta)
    async with bench(sql_tools=SQL_TOOLS) as b:
        await exchange("POST", b.proxy_url + "/mcp", headers=[*HEADERS, *header], content=body)
        await b.proxy.drain()
        (received,) = b.upstream.requests
        (event,) = b.sink.events
    run = event["run"]["runId"]
    if origin is not None:
        assert run == run_of(f"00-{TRACE_ID}-{PARENT_ID}-01")
        assert facet(event)["traceOrigin"] == origin
    # La requête est le gabarit du commentaire, l'identifiant de run, et la requête envoyée.
    query = query_of(received.body)
    assert query == f"/* answerlineage_run_id={run} */ {QUERY}"
    assert "DROP" not in query
    assert received.body == commented(body, run)


DUPLICATE = (
    b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"execute_sql",'
    b'"arguments":{"query":"SELECT 1","query":"SELECT c.nom FROM crm.clients c"}}}'
)


@pytest.mark.parametrize(
    ("headers", "body", "described"),
    [
        # Clé répétée, lot, corps compressé : le proxy ne lit pas le corps sans ambiguïté.
        ([], DUPLICATE, False),
        ([], b"[" + tool_call("execute_sql", {"query": QUERY}) + b"]", False),
        (
            [("content-encoding", "gzip")],
            gzip.compress(tool_call("execute_sql", {"query": QUERY}), mtime=0),
            False,
        ),
        # Des en-têtes qui lient le corps : un codage, une empreinte, une signature, un argument
        # recopié, que le serveur comparerait au corps modifié.
        ([("content-encoding", "identity")], tool_call("execute_sql", {"query": QUERY}), True),
        ([("content-digest", "sha-256=:AAAA=:")], tool_call("execute_sql", {"query": QUERY}), True),
        (
            [("signature-input", 'sig=("content-length")')],
            tool_call("execute_sql", {"query": QUERY}),
            True,
        ),
        (
            [("mcp-param-query", "=?base64?U0VMRUNU?=")],
            tool_call("execute_sql", {"query": QUERY}),
            True,
        ),
        # Une requête qui commence par une directive, une requête vide, un argument absent.
        ([], tool_call("execute_sql", {"query": "#standardSQL\nSELECT 1"}), True),
        ([], tool_call("execute_sql", {"query": " "}), True),
        ([], tool_call("execute_sql", {"requete": QUERY}), True),
    ],
    ids=[
        "cle-repetee",
        "lot",
        "compresse",
        "codage",
        "empreinte",
        "signature",
        "argument-recopie",
        "directive",
        "vide",
        "argument-absent",
    ],
)
async def test_a_body_that_cannot_be_tagged_with_certainty_is_relayed_untouched_and_counted(
    bench: BenchFactory, headers: list[tuple[str, str]], body: bytes, described: bool
) -> None:
    async with bench(sql_tools=SQL_TOOLS) as b:
        await exchange("POST", b.proxy_url + "/mcp", headers=[*HEADERS, *headers], content=body)
        await b.proxy.drain()
        (received,) = b.upstream.requests
        events = b.sink.events
    assert received.body == body
    assert received.header("content-length") == [str(len(body))]
    if described:
        (event,) = events
        assert facet(event)["taggedWithRunId"] is False
        assert facet(event)["_schemaURL"].endswith(TAGGED_SCHEMA)
    else:
        assert events == []
        assert b.proxy.stats.uninspected == 1
    assert (b.proxy.stats.tagged_queries, b.proxy.stats.untagged_queries) == (0, 1)


async def test_a_body_larger_than_the_size_read_is_streamed_untouched(bench: BenchFactory) -> None:
    body = tool_call("execute_sql", {"query": QUERY + " AND c.note = '" + "x" * 5000 + "'"})
    async with bench(sql_tools=SQL_TOOLS, max_inspected_bytes=1024) as b:
        await exchange("POST", b.proxy_url + "/mcp", headers=HEADERS, content=body)
        await b.proxy.drain()
        (received,) = b.upstream.requests
        (event,) = b.sink.events
    assert received.body == body
    assert facet(event)["taggedWithRunId"] is False
    assert facet(event)["unresolvedReason"] == "request_too_large"
    assert b.proxy.stats.untagged_queries == 1


async def test_a_tool_without_the_option_is_never_touched(bench: BenchFactory) -> None:
    body = tool_call("autre_sql", {"query": QUERY}, traceparent=IN_META)
    async with bench(sql_tools=SQL_TOOLS) as b:
        await exchange("POST", b.proxy_url + "/mcp", headers=HEADERS, content=body)
        await exchange("POST", b.proxy_url + "/mcp", headers=HEADERS, content=b'{"method":')
        await b.proxy.drain()
        received = b.upstream.requests
        (event,) = b.sink.events
    assert [request.body for request in received] == [body, b'{"method":']
    assert "taggedWithRunId" not in facet(event)
    assert facet(event)["_schemaURL"].endswith(
        "/spec/facets/1-0-0/ToolCallRunFacet.json#/$defs/ToolCallRunFacet"
    )
    # Le corps illisible pouvait cacher un appel de l'outil à commentaire : il est compté.
    assert (b.proxy.stats.tagged_queries, b.proxy.stats.untagged_queries) == (0, 1)


async def test_without_the_option_nothing_is_counted_nor_said(bench: BenchFactory) -> None:
    body = tool_call("execute_sql", {"query": QUERY})
    plain = {"execute_sql": SqlTool("query", "bigquery", "bigquery")}
    async with bench(sql_tools=plain) as b:
        await exchange("POST", b.proxy_url + "/mcp", headers=HEADERS, content=body)
        await exchange("POST", b.proxy_url + "/mcp", headers=HEADERS, content=DUPLICATE)
        await b.proxy.drain()
        received = b.upstream.requests
        (event,) = b.sink.events
    assert [request.body for request in received] == [body, DUPLICATE]
    assert "taggedWithRunId" not in facet(event)
    assert (b.proxy.stats.tagged_queries, b.proxy.stats.untagged_queries) == (0, 0)


async def test_a_tagging_that_fails_never_reaches_the_call(
    bench: BenchFactory, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(*_: object) -> bytes:
        raise RuntimeError(QUERY)

    monkeypatch.setattr(app, "comment_query", broken)
    body = tool_call("execute_sql", {"query": QUERY})
    async with bench(sql_tools=SQL_TOOLS) as b:
        with caplog.at_level(logging.ERROR, logger="answerlineage_proxy"):
            answered = await exchange("POST", b.proxy_url + "/mcp", headers=HEADERS, content=body)
        await b.proxy.drain()
        (received,) = b.upstream.requests
        (event,) = b.sink.events
    assert answered.status == 200
    assert received.body == body
    assert facet(event)["taggedWithRunId"] is False
    assert "Requête relayée sans son commentaire (RuntimeError)." in caplog.messages
    assert "Séville" not in caplog.text


# Le réglage.


def test_the_option_is_off_unless_named_for_a_declared_sql_tool(
    caplog: pytest.LogCaptureFixture,
) -> None:
    base = ["--upstream", "http://127.0.0.1:9000", "--sql-tool", "execute_sql=query:bigquery"]
    assert cli.settings_from_args(base, {}).sql_tools["execute_sql"].run_comment is False
    settings = cli.settings_from_args([*base, "--sql-run-comment", "execute_sql"], {})
    assert settings.sql_tools["execute_sql"].run_comment is True
    stdio_settings, _ = stdio.settings_from_args(
        ["--sql-tool", "execute_sql=query:bigquery", "--sql-run-comment", "execute_sql", "--", "s"],
        {},
    )
    assert stdio_settings.sql_tools["execute_sql"].run_comment is True
    with caplog.at_level(logging.WARNING, logger="answerlineage_proxy"):
        cli.log_run_comments(settings.sql_tools)
    (message,) = caplog.messages
    assert "« execute_sql »" in message
    assert "n'est plus strictement transparent" in message


@pytest.mark.parametrize(
    "arguments",
    [
        ["--sql-run-comment", "inconnu-7f3a"],
        ["--sql-tool", "execute_sql=query:bigquery", "--sql-run-comment", "inconnu-7f3a"],
    ],
)
def test_the_option_for_an_undeclared_tool_stops_the_proxy_without_citing_it(
    arguments: list[str],
) -> None:
    with pytest.raises(ProxyConfigurationError) as refused:
        cli.settings_from_args(["--upstream", "http://127.0.0.1:9000", *arguments], {})
    assert "inconnu-7f3a" not in str(refused.value)
    with pytest.raises(ProxyConfigurationError):
        stdio.settings_from_args([*arguments, "--", "serveur"], {})


# En stdio.


async def test_in_stdio_only_the_comment_of_the_run_differs_in_what_the_server_reads(
    tmp_path: Path,
) -> None:
    calls = [
        stdio_call(1, "execute_sql", {"query": QUERY}, traceparent=IN_META),
        stdio_call(2, "get_weather", {"location": "Séville"}),
        stdio_call(3, "execute_sql", {"query": QUERY}),
        stdio_call(4, "autre_sql", {"query": QUERY}),
    ]
    async with stdio_session(tmp_path, sql_tools=SQL_TOOLS) as session:
        for call in calls:
            await session.send(call)
        await session.replies(4)
        assert await session.close() == 0
    # Les événements partent dans l'ordre où les analyses aboutissent : ils se retrouvent par
    # leur outil et l'origine de leur contexte.
    events = {
        (facet(event)["tool"], facet(event)["traceOrigin"]): event for event in session.events
    }
    assert len(events) == 4
    first, third = events["execute_sql", "meta"], events["execute_sql", "proxy"]
    other = events["autre_sql", "proxy"]
    expected = [
        commented(calls[0], first["run"]["runId"]),
        calls[1],
        commented(calls[2], third["run"]["runId"]),
        calls[3],
    ]
    assert session.received == b"".join(expected)
    assert first["run"]["runId"] == run_of(IN_META)
    for event in (first, third):
        assert facet(event)["taggedWithRunId"] is True
        assert facet(event)["readResolution"] == "resolved"
    assert facet(third)["traceOrigin"] == "proxy"
    assert "taggedWithRunId" not in facet(other)
    # Sans _meta, un serveur en stdio ne voit aucun contexte : le commentaire porte seul le run.
    assert warehouse_tags(json.loads(calls[2])["params"].get("_meta")) is None
    stats = session.proxy.stats
    assert (stats.tagged_queries, stats.untagged_queries) == (2, 0)


async def test_in_stdio_a_call_written_in_pieces_is_tagged_once_whole(tmp_path: Path) -> None:
    call = stdio_call(1, "execute_sql", {"query": QUERY}, traceparent=IN_META)
    async with stdio_session(tmp_path, sql_tools=SQL_TOOLS) as session:
        for start in range(0, len(call), 7):
            await session.send(call[start : start + 7])
            await asyncio.sleep(0.002)
        await session.replies(1)
        assert await session.close() == 0
    (event,) = session.events
    assert session.received == commented(call, run_of(IN_META))
    assert facet(event)["taggedWithRunId"] is True


async def test_in_stdio_what_cannot_be_tagged_with_certainty_passes_untouched_and_is_counted(
    tmp_path: Path,
) -> None:
    big = {"query": QUERY + " AND c.note = '" + "x" * 5000 + "'"}
    sent = [
        # Plus long que la taille lue : relayé au fil de l'eau.
        stdio_call(1, "execute_sql", big),
        # Clé répétée : non inspecté.
        DUPLICATE.replace(b'"id":1', b'"id":2') + b"\n",
        # Sans identifiant : l'inspection ne le décrira pas, le relais ne l'étiquette pas.
        json.dumps(
            {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "execute_sql"}}
        ).encode()
        + b"\n",
        # Méthode écrite avec un échappement : le relais ne la reconnaît pas.
        stdio_call(4, "execute_sql", {"query": QUERY}).replace(b"tools/call", b"tools\\/call"),
        # Une directive en tête de la requête.
        stdio_call(5, "execute_sql", {"query": "#standardSQL\nSELECT 1"}),
    ]
    async with stdio_session(tmp_path, sql_tools=SQL_TOOLS, max_inspected_bytes=2048) as session:
        await session.send(b"".join(sent))
        await session.replies(4)
        assert await session.close() == 0
    assert session.received == b"".join(sent)
    assert [facet(event)["taggedWithRunId"] for event in session.events] == [False, False, False]
    stats = session.proxy.stats
    assert (stats.tagged_queries, stats.untagged_queries, stats.uninspected) == (0, 5, 2)


async def test_in_stdio_a_line_without_end_is_relayed_when_the_client_closes(
    tmp_path: Path,
) -> None:
    unfinished = stdio_call(1, "execute_sql", {"query": QUERY}).rstrip(b"\n")
    async with stdio_session(tmp_path, sql_tools=SQL_TOOLS) as session:
        await session.send(unfinished)
        assert await session.close() == 0
    assert session.received == unfinished
    assert session.events == []


async def test_in_stdio_a_tagging_that_fails_never_holds_the_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(*_: object) -> bytes:
        raise RuntimeError(QUERY)

    monkeypatch.setattr(stdio, "comment_query", broken)
    call = stdio_call(1, "execute_sql", {"query": QUERY})
    with caplog.at_level(logging.ERROR, logger="answerlineage_proxy"):
        async with stdio_session(tmp_path, sql_tools=SQL_TOOLS) as session:
            await session.send(call)
            await session.replies(1)
            assert await session.close() == 0
    assert session.received == call
    (event,) = session.events
    assert facet(event)["taggedWithRunId"] is False
    assert "Requête relayée sans son commentaire (RuntimeError)." in caplog.messages
    assert "Séville" not in caplog.text
