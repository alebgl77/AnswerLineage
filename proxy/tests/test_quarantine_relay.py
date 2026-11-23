"""Le relais HTTP avertit : le résultat d'une lecture en quarantaine porte l'avertissement.

Un faux serveur MCP rend des réponses JSON et SSE réglées chemin par chemin ; la
quarantaine est une zone vérifiée fixée ici, sans API (``FixedQuarantine``).
L'analyseur de quarantaine est le vrai, ou un analyseur simulé qui met le temps
qu'on lui dit, ou ne rend jamais la main. Un appel hors de la zone n'attend
jamais ; un appel retenu attend la décision au plus jusqu'à son échéance ; et
quand rien ne s'insère avec certitude, la réponse passe telle quelle et
l'événement dit pourquoi.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import secrets
import socket
import time
from collections.abc import AsyncIterator, Callable, Mapping, MutableMapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
import pytest

from answerlineage_proxy import cli
from answerlineage_proxy.analyzer import AnalyzerResult, SqlAnalyzer
from answerlineage_proxy.app import McpProxy
from answerlineage_proxy.config import (
    DEFAULT_QUARANTINE_DECISION_MS,
    ENV_QUARANTINE_DECISION_MS,
    ENV_UPSTREAM,
    ProxyConfigurationError,
    ProxySettings,
    SqlTool,
)
from answerlineage_proxy.events import TOOL_CALL_FACET, TOOL_CALL_FACET_SCHEMA_QUARANTINE
from answerlineage_proxy.fingerprint import RESULT_LABEL, FingerprintKey
from answerlineage_proxy.quarantine import QuarantineSettings, QuarantineSync, parse
from answerlineage_proxy.rules import DeclaredDataset, ReadRule
from answerlineage_proxy.sink import MemorySink
from answerlineage_proxy.sql import Dialect, SqlAnalysis, analyze_sql
from proxy.tests.conftest import Exchange, exchange, running, tool_call, until
from proxy.tests.test_quarantine import API, INCIDENT, FakeApi, body, document, scope
from proxy.tests.test_quarantine import Reply as ApiReply
from proxy.tests.test_quarantine_signature import WallClock, signed_answer

pytestmark = pytest.mark.anyio

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Any]
Send = Callable[[Message], Any]
Reply = tuple[int, list[tuple[bytes, bytes]], list[Any]]

KEY = FingerprintKey(bytes(range(32)))
ORDERS = {"namespace": "bigquery", "name": "ventes.suivi.commandes"}
QUERY = "SELECT montant FROM ventes.suivi.commandes WHERE jour = '2026-11-16'"
ELSEWHERE = "SELECT taux FROM finance.ref.taux_change"
SQL_TOOLS = {"execute_sql": SqlTool(argument="query", dialect="bigquery", namespace="bigquery")}
RULES = (
    ReadRule(tool="get_orders", datasets=(DeclaredDataset(ORDERS["namespace"], ORDERS["name"]),)),
    ReadRule(tool="get_weather", datasets=(DeclaredDataset("meteo", "previsions"),)),
)
RESULT = (
    b'{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"12 commandes"}],'
    b'"isError":false,"resultType":"complete"}}'
)
PROGRESS = b'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
REPLY_EVENT = b"event: message\ndata: " + RESULT + b"\n\n"
JSONRPC_ERROR = b'{"jsonrpc":"2.0","id":1,"error":{"code":-32602,"message":"Requete invalide"}}'
JSON = (b"content-type", b"application/json")
EVENT_STREAM = (b"content-type", b"text/event-stream")


class FixedQuarantine(QuarantineSync):
    """Une synchronisation simulée : la zone qu'on lui donne, vérifiée ou non, sans API."""

    def __init__(self, *scopes: dict[str, Any], verified: bool = True) -> None:
        secret = "partage-" + secrets.token_urlsafe(32) if verified else None
        super().__init__(QuarantineSettings.create(httpx.URL(API), secret=secret))
        self.known = parse(body(document(*(scopes or (scope(datasets=[ORDERS]),)))))

    async def start(self) -> None:
        """Rien à interroger."""


class TimedAnalyzer(SqlAnalyzer):
    """Un analyseur simulé : l'analyse de sqlglot, dans le processus, après ``delay`` secondes ;
    sans délai, il ne rend jamais la main. Il compte ce qu'on lui demande."""

    def __init__(self, delay: float | None, *, over_budget: bool = False) -> None:
        super().__init__()
        self.delay = delay
        self.over_budget = over_budget
        self.asked = 0

    async def analyze(
        self,
        sql: str,
        dialect: Dialect,
        *,
        default_catalog: str | None = None,
        default_db: str | None = None,
    ) -> AnalyzerResult:
        self.asked += 1
        if self.delay is None:
            await asyncio.Event().wait()
        await asyncio.sleep(self.delay or 0)
        if self.over_budget:
            abandoned = SqlAnalysis(resolved=False, reason="budget_exceeded")
            return AnalyzerResult(abandoned, "over_budget")
        return AnalyzerResult(analyze_sql(sql, dialect), "analyzed")

    async def warm(self) -> None:
        """Rien à démarrer."""


def json_reply(content: bytes = RESULT, *headers: tuple[bytes, bytes]) -> Reply:
    length = (b"content-length", str(len(content)).encode())
    return 200, [JSON, length, *headers], [content]


@dataclass
class Upstream:
    """Un serveur MCP réglé chemin par chemin : statut, en-têtes, morceaux du corps. Un
    morceau ``None`` attend ``gate`` ; un nombre, autant de secondes ; un morceau ``...``
    attend sans fin."""

    replies: dict[str, Reply] = field(default_factory=dict)
    gate: asyncio.Event = field(default_factory=asyncio.Event)
    delay: float = 0.0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        while (await receive()).get("more_body", False):
            pass
        await asyncio.sleep(self.delay)
        status, headers, chunks = self.replies.get(scope["path"].rsplit("/", 1)[-1], json_reply())
        await send({"type": "http.response.start", "status": status, "headers": headers})
        for chunk in chunks:
            if chunk is None:
                await self.gate.wait()
            elif chunk is ...:
                await asyncio.Event().wait()
            elif isinstance(chunk, float):
                await asyncio.sleep(chunk)
            else:
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})


@dataclass
class Warned:
    upstream: Upstream
    proxy: McpProxy
    url: str
    sink: MemorySink

    async def call(self, path: str = "mcp", **options: Any) -> tuple[Exchange, Mapping[str, Any]]:
        response = await exchange("POST", f"{self.url}/{path}", content=sql_call(**options))
        await self.proxy.drain()
        return response, self.sink.events[-1]


@asynccontextmanager
async def warned(
    sync: QuarantineSync | None = None,
    *,
    quarantine: bool = True,
    analyzer: SqlAnalyzer | None = None,
    ordinary: SqlAnalyzer | None = None,
    upstream: Upstream | None = None,
    upstream_url: str | None = None,
    sql_budget_ms: int = 50,
    max_inspected_bytes: int = 1024 * 1024,
    decision_ms: int = DEFAULT_QUARANTINE_DECISION_MS,
) -> AsyncIterator[Warned]:
    server = upstream or Upstream()
    sink = MemorySink()
    async with running(server) as served:
        settings = ProxySettings.create(
            upstream_url or served,
            "entrepot",
            sql_tools=SQL_TOOLS,
            read_rules=RULES,
            sql_budget_ms=sql_budget_ms,
            max_inspected_bytes=max_inspected_bytes,
            fingerprint_key=KEY,
            quarantine_decision_ms=decision_ms,
        )
        proxy = McpProxy(
            settings,
            sink,
            quarantine=(sync or FixedQuarantine()) if quarantine else None,
            quarantine_analyzer=analyzer,
            analyzer=ordinary,
        )
        async with running(proxy, transparent=True) as url:
            yield Warned(server, proxy, url, sink)


def sql_call(tool: str = "execute_sql", query: str | None = QUERY) -> bytes:
    return tool_call(tool, {} if query is None else {"query": query})


def content(response: Exchange) -> list[dict[str, Any]]:
    return list(json.loads(response.body)["result"]["content"])


def facet(event: Mapping[str, Any]) -> dict[str, Any]:
    described: dict[str, Any] = event["run"]["facets"][TOOL_CALL_FACET]
    return described


def warning(response: Exchange) -> str:
    first = content(response)[0]
    assert first["annotations"] == {"audience": ["assistant", "user"], "priority": 1}
    text: str = first["text"]
    return text


# Critère 1 : une lecture SQL de la zone est annotée.


async def test_a_sql_read_of_the_zone_carries_the_incident_the_perimeter_and_the_date() -> None:
    async with warned(analyzer=TimedAnalyzer(0.005)) as bench:
        response, event = await bench.call()
    text = warning(response)
    assert INCIDENT in text
    assert "Périmètre lu : bigquery ventes.suivi.commandes." in text
    assert "en quarantaine depuis le 2026-11-16 08:55 UTC" in text
    # Le contenu d'origine suit, intact, et la longueur annoncée suit le corps.
    assert content(response)[1:] == json.loads(RESULT)["result"]["content"]
    assert response.header("content-length") == [str(len(response.body))]
    described = facet(event)
    assert described["_schemaURL"] == TOOL_CALL_FACET_SCHEMA_QUARANTINE
    quarantine = described["quarantine"]
    assert {key: quarantine[key] for key in ("decision", "incidents", "annotated")} == {
        "decision": "certain",
        "incidents": [INCIDENT],
        "annotated": True,
    }
    assert 0 <= quarantine["waitMs"] <= 50
    assert "notAnnotatedReason" not in quarantine
    # Empreinte et taille portent sur les octets du serveur, annotation exclue.
    assert described["resultSize"] == len(RESULT)
    expected = KEY.digest(RESULT_LABEL)
    expected.update(RESULT)
    assert described["resultFingerprint"] == expected.fingerprint()
    assert [item["name"] for item in event["inputs"]] == [ORDERS["name"]]
    assert bench.proxy.stats.retained_calls == bench.proxy.stats.annotated_results == 1


async def test_with_the_real_analyzer_warmed_a_read_of_the_zone_is_certain() -> None:
    """Le serveur répond en 100 ms, puis rend son corps 100 ms plus tard."""
    upstream = Upstream(delay=0.1)
    length = (b"content-length", str(len(RESULT)).encode())
    upstream.replies["mcp"] = (200, [JSON, length], [0.1, RESULT])
    async with warned(upstream=upstream) as bench:
        await bench.proxy.quarantine_analyzer.warm()
        response, event = await bench.call()
    assert warning(response).startswith(
        "[AnswerLineage] Avertissement sur la qualité des données :"
    )
    assert facet(event)["quarantine"]["decision"] == "certain"
    # Le serveur a mis plus que l'échéance à répondre : la réponse n'a rien attendu, et le
    # temps du serveur ne compte pas dans la latence que le proxy ajoute.
    assert facet(event)["quarantine"]["waitMs"] == 0
    signals = event["run"]["facets"]["answerlineage_qos"]
    assert signals["latencyMs"] >= 200
    assert signals["addedLatencyMs"] < 100


async def test_a_read_outside_the_zone_is_decided_outside_and_left_untouched() -> None:
    async with warned(analyzer=TimedAnalyzer(0.0)) as bench:
        response, event = await bench.call(query=ELSEWHERE)
    assert response.body == RESULT
    quarantine = facet(event)["quarantine"]
    assert (quarantine["decision"], quarantine["incidents"], quarantine["annotated"]) == (
        "outside",
        [INCIDENT],
        False,
    )
    assert "notAnnotatedReason" not in quarantine
    assert bench.proxy.stats.annotated_results == bench.proxy.stats.unannotated_results == 0


async def test_a_tool_whose_read_rule_declares_the_zone_is_annotated_without_waiting() -> None:
    blocked = TimedAnalyzer(None)
    async with warned(analyzer=blocked) as bench:
        response, event = await bench.call(tool="get_orders", query=None)
    assert "bigquery ventes.suivi.commandes" in warning(response)
    assert (facet(event)["quarantine"]["waitMs"], facet(event)["quarantine"]["annotated"]) == (
        0,
        True,
    )
    assert blocked.asked == 0


# Critère 4 : la décision n'attend que son échéance.


async def test_the_decision_waits_at_most_its_budget() -> None:
    """Une analyse de 200 ms : l'appel passe à l'échéance, annoté comme incertain ; le lignage
    reçoit la lecture une fois l'analyse finie."""
    async with warned(analyzer=TimedAnalyzer(0.2)) as bench:
        started = time.perf_counter()
        response = await exchange("POST", f"{bench.url}/mcp", content=sql_call())
        elapsed = time.perf_counter() - started
        await bench.proxy.drain()
        event = bench.sink.events[-1]
    assert elapsed < 0.18
    assert "incertain" in warning(response)
    assert "Périmètre possible : espace de noms bigquery." in warning(response)
    quarantine = facet(event)["quarantine"]
    assert quarantine["decision"] == "uncertain"
    assert quarantine["uncertainReason"] == "decision_timeout"
    assert quarantine["annotated"] is True
    assert 0 < quarantine["waitMs"] <= 50
    assert event["run"]["facets"]["answerlineage_qos"]["addedLatencyMs"] >= quarantine["waitMs"]
    assert [item["name"] for item in event["inputs"]] == [ORDERS["name"]]


async def test_with_a_budget_of_zero_the_answer_never_waits() -> None:
    async with warned(analyzer=TimedAnalyzer(0.1), decision_ms=0) as bench:
        response, event = await bench.call()
    assert "incertain" in warning(response)
    assert facet(event)["quarantine"]["waitMs"] == 0
    assert facet(event)["quarantine"]["uncertainReason"] == "decision_timeout"


@pytest.mark.parametrize(
    ("query", "reason", "max_bytes"),
    [
        ("UPDATE ventes.suivi.commandes SET montant = 0", "unsupported_statement", 1024 * 1024),
        (None, "missing_argument", 1024 * 1024),
        (QUERY + " " * 400, "request_too_large", 256),
    ],
)
async def test_an_unresolved_read_is_uncertain(
    query: str | None, reason: str, max_bytes: int
) -> None:
    async with warned(analyzer=TimedAnalyzer(0.0), max_inspected_bytes=max_bytes) as bench:
        response, event = await bench.call(query=query)
    assert "incertain" in warning(response)
    described = facet(event)
    assert described["quarantine"]["uncertainReason"] == "unresolved_read"
    assert described["unresolvedReason"] == reason


async def test_an_analysis_abandoned_at_its_fixed_budget_is_redone_for_the_lineage() -> None:
    async with warned(
        analyzer=TimedAnalyzer(0.0, over_budget=True),
        ordinary=TimedAnalyzer(0.0),
        sql_budget_ms=500,
    ) as bench:
        response, event = await bench.call()
    assert "incertain" in warning(response)
    assert facet(event)["quarantine"]["uncertainReason"] == "unresolved_read"
    assert facet(event)["readResolution"] == "resolved"
    assert [item["name"] for item in event["inputs"]] == [ORDERS["name"]]


# Un appel hors de la zone n'attend jamais.


async def test_a_call_outside_the_zone_never_waits() -> None:
    """L'analyseur de quarantaine ne rend jamais la main : un appel qu'aucune quarantaine ne
    retient (un outil d'un autre périmètre, un outil non décrit) revient sans délai et à
    l'octet près, sans rien demander à cet analyseur."""
    blocked = TimedAnalyzer(None)
    async with warned(analyzer=blocked) as bench:
        for tool in ("get_weather", "inconnu", "get_weather"):
            started = time.perf_counter()
            response, event = await bench.call(tool=tool, query=None)
            assert time.perf_counter() - started < 1
            assert response.body == RESULT
            assert "quarantine" not in facet(event)
            assert facet(event)["_schemaURL"].endswith(
                "/1-0-0/ToolCallRunFacet.json#/$defs/ToolCallRunFacet"
            )
            assert event["run"]["facets"]["answerlineage_qos"]["addedLatencyMs"] < 50
    assert bench.proxy.stats.retained_calls == 0
    assert blocked.asked == 0


async def test_nothing_is_retained_without_quarantine_or_without_a_verified_diffusion() -> None:
    for options in ({"quarantine": False}, {"sync": FixedQuarantine(verified=False)}):
        async with warned(analyzer=TimedAnalyzer(None), **options) as bench:
            response, event = await bench.call()
        assert response.body == RESULT
        assert "quarantine" not in facet(event)
        assert bench.proxy.stats.retained_calls == 0


async def test_a_zone_in_another_namespace_retains_nothing() -> None:
    other = scope(datasets=[{"namespace": "postgres://ref", "name": ORDERS["name"]}])
    async with warned(FixedQuarantine(other), analyzer=TimedAnalyzer(None)) as bench:
        response, event = await bench.call()
    assert response.body == RESULT
    assert "quarantine" not in facet(event)


# Un flux SSE.


async def test_in_a_stream_the_notifications_pass_at_once_and_the_reply_is_annotated() -> None:
    upstream = Upstream()
    upstream.replies["sse"] = (200, [EVENT_STREAM], [PROGRESS, None, REPLY_EVENT])
    async with warned(upstream=upstream, analyzer=TimedAnalyzer(0.0)) as bench:
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            client.headers.clear()
            async with client.stream("POST", f"{bench.url}/sse", content=sql_call()) as response:
                chunks = response.aiter_raw()
                # La notification arrive avant que le serveur ne rende la réponse.
                assert await anext(chunks) == PROGRESS
                upstream.gate.set()
                rest = b"".join([chunk async for chunk in chunks])
        await bench.proxy.drain()
        event = bench.sink.events[-1]
    assert rest.startswith(b"event: message\ndata: ")
    reply = json.loads(rest.split(b"data: ", 1)[1])
    assert reply["result"]["content"][0]["text"].startswith("[AnswerLineage]")
    assert reply["result"]["content"][1:] == json.loads(RESULT)["result"]["content"]
    assert facet(event)["quarantine"]["annotated"] is True
    assert facet(event)["resultSize"] == len(PROGRESS + REPLY_EVENT)


# Rien n'est inséré, et l'événement dit pourquoi.

UNTOUCHED: dict[str, tuple[Reply, str]] = {
    "gzip": (
        (200, [JSON, (b"content-encoding", b"gzip")], [gzip.compress(RESULT, mtime=0)]),
        "encoded",
    ),
    "digest": (json_reply(RESULT, (b"content-digest", b"sha-256=:AAAA:")), "body_bound"),
    "signature": (
        json_reply(RESULT, (b"signature-input", b'sig=("content-digest")')),
        "body_bound",
    ),
    "http-error": ((500, [JSON], [JSONRPC_ERROR]), "error_response"),
    "jsonrpc-error": (json_reply(JSONRPC_ERROR), "error_response"),
    "no-content": (
        json_reply(b'{"jsonrpc":"2.0","id":1,"result":{"isError":false}}'),
        "no_content",
    ),
    "other-id": (json_reply(RESULT.replace(b'"id":1', b'"id":2')), "not_whole"),
    "text": ((200, [(b"content-type", b"text/plain")], [b"12 commandes"]), "no_content"),
    "redirect": ((307, [(b"location", b"http://example.invalid/")], []), "no_content"),
    "sse-with-length": (
        (200, [EVENT_STREAM, (b"content-length", str(len(REPLY_EVENT)).encode())], [REPLY_EVENT]),
        "body_bound",
    ),
    "sse-several-lines": (
        (200, [EVENT_STREAM], [b"data: " + RESULT[:30] + b"\ndata: " + RESULT[30:] + b"\n\n"]),
        "not_whole",
    ),
}


@pytest.mark.parametrize("case", sorted(UNTOUCHED))
async def test_a_reply_that_cannot_be_annotated_with_certainty_passes_untouched_and_says_why(
    case: str,
) -> None:
    reply, reason = UNTOUCHED[case]
    upstream = Upstream()
    upstream.replies[case] = reply
    async with warned(upstream=upstream, analyzer=TimedAnalyzer(0.0)) as bench:
        response, event = await bench.call(case)
    assert response.body == b"".join(reply[2])
    quarantine = facet(event)["quarantine"]
    assert (quarantine["annotated"], quarantine["notAnnotatedReason"]) == (False, reason)
    assert bench.proxy.stats.unannotated_results == 1


async def test_a_reply_cut_while_held_passes_what_was_held_and_says_why() -> None:
    upstream = Upstream()
    upstream.replies["cut"] = (
        200,
        [JSON, (b"content-length", b"500")],
        [b'{"jsonrpc":"2.0","id":1,"result":{"content"'],
    )
    async with warned(upstream=upstream, analyzer=TimedAnalyzer(0.0)) as bench:
        with pytest.raises(httpx.HTTPError):
            await exchange("POST", f"{bench.url}/cut", content=sql_call())
        await until(lambda: bench.sink.events)
        await bench.proxy.drain()
        event = bench.sink.events[-1]
    assert facet(event)["proxyError"] == "upstream_interrupted"
    assert facet(event)["quarantine"]["notAnnotatedReason"] == "not_whole"


async def test_a_client_gone_while_the_reply_is_held_is_said() -> None:
    upstream = Upstream()
    upstream.replies["stall"] = (200, [JSON], [b'{"jsonrpc":"2.0","id":1,"result":', ...])
    async with warned(upstream=upstream, analyzer=TimedAnalyzer(0.0)) as bench:
        async with httpx.AsyncClient(trust_env=False, timeout=0.3) as client:
            with pytest.raises(httpx.ReadTimeout):
                await client.post(f"{bench.url}/stall", content=sql_call())
        await until(lambda: bench.sink.events)
        await bench.proxy.drain()
        event = bench.sink.events[-1]
    assert event["eventType"] == "ABORT"
    assert facet(event)["quarantine"]["notAnnotatedReason"] == "client_gone"


async def test_a_request_the_proxy_refuses_is_said_as_an_error_response() -> None:
    async with warned(analyzer=TimedAnalyzer(0.0)) as bench:
        response, event = await bench.call("base/%2e%2e/mcp")
    assert response.status == 400
    assert facet(event)["quarantine"]["notAnnotatedReason"] == "error_response"


async def test_an_unreachable_server_is_said_as_an_error_response() -> None:
    with socket.socket() as reserved:
        # Réservé sans écouter : la connexion au serveur MCP est refusée.
        reserved.bind(("127.0.0.1", 0))
        unreachable = f"http://127.0.0.1:{reserved.getsockname()[1]}"
        async with warned(upstream_url=unreachable, analyzer=TimedAnalyzer(0.0)) as bench:
            response, event = await bench.call()
    assert response.status == 502
    assert facet(event)["quarantine"]["notAnnotatedReason"] == "error_response"


# Le préchauffage de l'analyseur de quarantaine, et les réglages.


class CountedWarmth(TimedAnalyzer):
    def __init__(self) -> None:
        super().__init__(0.0)
        self.warmed = 0

    async def warm(self) -> None:
        self.warmed += 1


async def test_a_zone_that_targets_a_sql_tool_warms_the_quarantine_analyzer() -> None:
    """Une nouvelle zone vérifiée qui vise l'espace de noms d'un outil SQL déclaré démarre
    l'analyseur de quarantaine, hors du chemin des appels ; une zone ailleurs, ou l'oubli du
    périmètre, non."""
    secret = "partage-" + secrets.token_urlsafe(32)
    replies: list[ApiReply] = [
        signed_answer(document(scope(datasets=[ORDERS])), [secret]),
        signed_answer(
            document(
                scope(datasets=[{"namespace": "meteo", "name": "previsions"}]), version="b" * 64
            ),
            [secret],
        ),
    ]
    api = FakeApi(replies)
    client = httpx.AsyncClient(transport=httpx.MockTransport(api))
    sync = QuarantineSync(
        QuarantineSettings.create(httpx.URL(API), secret=secret),
        client=client,
        wall_clock=WallClock(),
        stale_after=timedelta(0),
    )
    warmth = CountedWarmth()
    settings = ProxySettings.create("http://127.0.0.1:9000", "entrepot", sql_tools=SQL_TOOLS)
    proxy = McpProxy(settings, MemorySink(), quarantine=sync, quarantine_analyzer=warmth)
    try:
        assert await sync.sync_once()
        # La zone est calculée au changement du périmètre, avant tout appel.
        first = proxy._zone
        assert first is not None and first.entry(*ORDERS.values()) is not None
        assert proxy._warming is not None
        await proxy._warming
        assert warmth.warmed == 1
        assert await sync.sync_once()
        assert proxy._zone is not first
        api.replies = [httpx.ConnectError("injoignable")]
        assert not await sync.sync_once()
        assert sync.known is None
        assert proxy._current_zone() is None
        assert warmth.warmed == 1
    finally:
        await proxy.aclose()
        await client.aclose()


def test_the_command_line_sets_the_wait_for_the_decision_from_0_to_50_ms(
    caplog: pytest.LogCaptureFixture,
) -> None:
    upstream = ["--upstream", "http://127.0.0.1:9000"]
    default = cli.settings_from_args(upstream, {})
    assert default.quarantine_decision_ms == DEFAULT_QUARANTINE_DECISION_MS == 50
    variable = {ENV_QUARANTINE_DECISION_MS: "0"}
    assert cli.settings_from_args(upstream, variable).quarantine_decision_ms == 0
    environment = {
        ENV_UPSTREAM: "http://127.0.0.1:9000",
        ENV_QUARANTINE_DECISION_MS: "12",
    }
    assert ProxySettings.from_environment(environment).quarantine_decision_ms == 12
    option = [*upstream, "--quarantine-decision-ms", "20"]
    assert cli.settings_from_args(option, variable).quarantine_decision_ms == 20
    for value in ("-1", "51", "x"):
        with pytest.raises(ProxyConfigurationError) as refused:
            cli.settings_from_args(upstream, {ENV_QUARANTINE_DECISION_MS: value})
        assert value not in str(refused.value).replace("50", "").replace("0 à", "")
    caplog.set_level(logging.INFO, logger="answerlineage_proxy")
    verified = QuarantineSettings.create(
        httpx.URL(API), secret="partage-" + secrets.token_urlsafe(32)
    )
    cli.log_warnings(verified, 20)
    cli.log_warnings(verified)
    cli.log_warnings(QuarantineSettings.create(httpx.URL(API)), 20)
    cli.log_warnings(None, 20)
    said = [record.getMessage() for record in caplog.records]
    assert said == [
        "Quarantaine en mode avertir : le résultat d'un appel d'un outil visé porte un "
        "avertissement, après une attente de la décision de 20 ms au plus ; un appel hors de "
        "la zone n'attend jamais.",
        "Quarantaine : en stdio, aucun résultat n'est annoté.",
    ]
