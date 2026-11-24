"""Les émetteurs du dépôt passent le kit de conformité.

L'entreprise synthétique et les scénarios de vérité parlent comme un adaptateur
d'agents : leur capture porte la chaîne complète. Le proxy est un point de
capture, en HTTP comme en stdio : il ne connaît pas l'agent, et cite le contexte
de trace dont son identifiant de run dérive. Un agent joué par un script, sans modèle, derrière
le vrai proxy, donne une capture combinée : les deux émetteurs voient le même
contexte de trace, émettent pour le même run, et le graphe en tire une chaîne
complète, du jeu de données à la réponse.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_api.pseudonyms.vault import PseudonymKey, pseudonym_of
from answerlineage_conformance.judgement import Judgement, judge
from answerlineage_conformance.runid import derive_run_id
from answerlineage_corpus.enterprise import FIRST_DAY, generate
from answerlineage_corpus.events import render
from answerlineage_corpus.scenarios import Scenario, load_corpus
from answerlineage_proxy.app import McpProxy, ProxyStats
from answerlineage_proxy.config import ProxySettings, SqlTool
from answerlineage_proxy.fingerprint import FingerprintKey
from answerlineage_proxy.sink import MemorySink
from answerlineage_workers.queue import process_batch
from api.tests.issuer import authentication, signed_in
from proxy.tests.conftest import (
    MODERN_META,
    FakeMcpServer,
    Received,
    exchange,
    running,
    stdio_call,
    stdio_session,
    tool_call,
)

# Heures fixes : aucun jugement ne dépend de l'horloge.
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
RECEIVED_AT = FIRST_DAY + timedelta(days=30)

PRODUCER = "https://exemple.org/agent-joue-par-un-script"
RUN_EVENT = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"
OWN = "https://raw.githubusercontent.com/alebgl77/AnswerLineage/main/spec/facets/1-0-0/"
PARENT = "https://openlineage.io/spec/facets/1-2-0/ParentRunFacet.json#/$defs/ParentRunFacet"
AGENT = {"namespace": "agent://finance", "name": "analyste-change"}
AGENT_RUN = "0193a9c5-7a10-7c3e-9d5e-2f1b6c8a4e77"
# Le destinataire de la réponse, pseudonymisé comme l'agent le ferait, sous une clé tirée ici.
RECIPIENT = pseudonym_of(PseudonymKey(secrets.token_bytes(32)), "destinataire@exemple.fr")
TABLE = "finance.ref.taux_change"
CHANNEL, MESSAGE_REF = "slack", "m-2026-11-02-0001"
# Le contexte de trace de l'appel d'outil, que l'agent ouvre et que le proxy lit.
TRACE_ID, SPAN_ID = "0af7651916cd43dd8448eb211c80319c", "b7ad6b7169203331"
TRACEPARENT = f"00-{TRACE_ID}-{SPAN_ID}-01"
HEADERS = [
    ("content-type", "application/json"),
    ("mcp-protocol-version", "2026-07-28"),
    ("mcp-method", "tools/call"),
    ("mcp-name", "execute_sql"),
]
SQL_TOOLS = {"execute_sql": SqlTool("query", "bigquery", "bigquery")}


def proxy_settings(upstream_url: str) -> ProxySettings:
    """Le proxy devant le serveur MCP de test, avec son outil SQL et sa clé d'empreinte.

    Avec la clé, le proxy émet tout ce qu'il sait émettre : les empreintes des
    arguments et du résultat, et la forme de la requête sans ses littéraux. Le
    budget d'analyse du SQL est au plus large : sur une machine chargée, une
    analyse abandonnée ferait une lecture « non résolue », et la preuve ne
    tiendrait plus qu'à l'horloge.
    """
    return ProxySettings.create(
        upstream_url,
        "entrepot",
        sql_tools=SQL_TOOLS,
        sql_budget_ms=60_000,
        fingerprint_key=FingerprintKey(bytes(range(32))),
    )


def conformant(judgement: Judgement) -> bool:
    """Vrai quand rien n'est relevé ; sinon l'échec du test liste les problèmes."""
    assert [(p.code, p.event, p.pointer, p.reason) for p in judgement.problems] == []
    return judgement.conformant


def test_the_synthetic_enterprise_is_conformant_as_an_adapter() -> None:
    events = render(generate(7))
    judgement = judge(events, profile="adapter", received_at=RECEIVED_AT)
    assert conformant(judgement)
    assert (judgement.events, judgement.runs) == (len(events), 727)
    assert {control.group for control in judgement.controls} == {"shape", "context", "projection"}


SCENARIOS = load_corpus().scenarios


def test_the_truth_corpus_holds_twelve_scenarios() -> None:
    assert len(SCENARIOS) == 12


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda scenario: scenario.name)
def test_a_truth_scenario_is_conformant_as_an_adapter(scenario: Scenario) -> None:
    assert conformant(judge(scenario.events(), profile="adapter", received_at=RECEIVED_AT))


def _moment(seconds: int) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _own(name: str, **fields: Any) -> dict[str, Any]:
    address = f"{OWN}{name}.json#/$defs/{name}"
    return {"_producer": PRODUCER, "_schemaURL": address, **fields}


def _event(kind: str, seconds: int, run: dict[str, Any], job: dict[str, str]) -> dict[str, Any]:
    return {
        "eventType": kind,
        "eventTime": _moment(seconds),
        "producer": PRODUCER,
        "schemaURL": RUN_EVENT,
        "run": run,
        "job": job,
        "inputs": [],
        "outputs": [],
    }


def agent_event(kind: str, seconds: int) -> dict[str, Any]:
    """Un événement du run de l'agent, tel qu'un adaptateur écrit d'après la documentation."""
    run = {"runId": AGENT_RUN, "facets": {"answerlineage_agent": _own("AgentRunFacet")}}
    return _event(kind, seconds, run, dict(AGENT))


def tool_call_opened(seconds: int) -> dict[str, Any]:
    """L'appel d'outil vu par l'agent : il le rattache à son run, le proxy dira comment il finit.

    L'identifiant de run est refait d'après ``docs/proxy.md``, à partir du
    contexte de trace que l'agent transmet : c'est celui que le proxy dérive.
    """
    run = {
        "runId": str(derive_run_id(TRACE_ID, SPAN_ID)),
        "facets": {
            "parent": {
                "_producer": PRODUCER,
                "_schemaURL": PARENT,
                "run": {"runId": AGENT_RUN},
                "job": dict(AGENT),
            },
            "answerlineage_toolCall": _own(
                "ToolCallRunFacet",
                server="entrepot",
                tool="execute_sql",
                traceId=TRACE_ID,
                spanId=SPAN_ID,
            ),
        },
    }
    return _event("START", seconds, run, {"namespace": "mcp://entrepot", "name": "execute_sql"})


def answer_delivered(seconds: int, answer: str) -> dict[str, Any]:
    """La fin du run de l'agent : la réponse n'y est que par son empreinte."""
    event = agent_event("COMPLETE", seconds)
    event["outputs"] = [
        {
            "namespace": AGENT["namespace"],
            "name": f"{AGENT['name']}/reponses",
            "outputFacets": {
                "answerlineage_delivery": _own(
                    "DeliveryOutputDatasetFacet",
                    fingerprint="sha256:" + hashlib.sha256(answer.encode()).hexdigest(),
                    channel=CHANNEL,
                    messageRef=MESSAGE_REF,
                    recipients=[RECIPIENT],
                )
            },
        }
    ]
    return event


@dataclass
class Played:
    """Une demande jouée : ce que les émetteurs ont émis, et ce que chacun a vu passer."""

    # Capture combinée : les événements de l'agent et ceux du proxy.
    events: list[dict[str, Any]]
    # L'appel tel que le serveur MCP l'a reçu, et son résultat tel que l'agent l'a reçu.
    received: Received
    result: bytes
    stats: ProxyStats

    @property
    def from_proxy(self) -> list[dict[str, Any]]:
        return [event for event in self.events if event["producer"] != PRODUCER]


async def play(prompt: str, tool_result: bytes | None, answer: str) -> Played:
    """Joue une demande : un agent écrit d'avance appelle un outil à travers le vrai proxy.

    Le script tient lieu d'agent et d'adaptateur : aucun composant du dépôt ne
    voit encore un prompt ou une réponse. Seul l'appel d'outil traverse un vrai
    composant, le proxy, devant un serveur MCP de test.
    """
    upstream = FakeMcpServer() if tool_result is None else FakeMcpServer(tool_result=tool_result)
    sink = MemorySink()
    # L'agent tire de la demande le littéral de sa requête : le prompt part dans l'argument.
    query = f"SELECT taux FROM {TABLE} WHERE devise = '{prompt}'"  # noqa: S608
    emitted = [agent_event("START", 0), tool_call_opened(1)]
    async with running(upstream) as upstream_url:
        settings = proxy_settings(upstream_url)
        proxy = McpProxy(settings, sink, clock=lambda: T0 + timedelta(seconds=2))
        async with running(proxy, transparent=True) as proxy_url:
            relayed = await exchange(
                "POST",
                f"{proxy_url}/mcp",
                headers=HEADERS,
                content=tool_call(
                    "execute_sql", {"query": query}, traceparent=TRACEPARENT, **MODERN_META
                ),
            )
            await proxy.drain()
    assert relayed.status == 200
    emitted.extend(dict(event) for event in sink.events)
    emitted.append(answer_delivered(3, answer))
    (received,) = upstream.requests
    return Played(emitted, received, relayed.body, proxy.stats)


@pytest.mark.anyio
async def test_the_events_of_the_proxy_are_conformant_as_a_capture_point() -> None:
    """Contexte reçu dans le corps, reçu en en-tête, ou créé par le proxy ; appel réussi ou non."""
    upstream = FakeMcpServer()
    sink = MemorySink()
    query = {"query": f"SELECT devise, taux FROM {TABLE}"}  # noqa: S608
    in_header = ("traceparent", "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
    async with running(upstream) as upstream_url:
        settings = proxy_settings(upstream_url)
        proxy = McpProxy(settings, sink, clock=lambda: T0)
        async with running(proxy, transparent=True) as proxy_url:
            in_meta = tool_call("execute_sql", query, traceparent=TRACEPARENT, **MODERN_META)
            plain = tool_call("execute_sql", query, **MODERN_META)
            await exchange("POST", f"{proxy_url}/mcp", headers=HEADERS, content=in_meta)
            await exchange("POST", f"{proxy_url}/mcp", headers=[*HEADERS, in_header], content=plain)
            await exchange("POST", f"{proxy_url}/mcp", headers=HEADERS, content=plain)
            refused = await exchange(
                "POST", f"{proxy_url}/unauthorized", headers=HEADERS, content=plain
            )
            unreadable = tool_call("execute_sql", {"query": "CALL proc()"}, **MODERN_META)
            await exchange("POST", f"{proxy_url}/mcp", headers=HEADERS, content=unreadable)
            await proxy.drain()
    assert refused.status == 401

    events = [dict(event) for event in sink.events]
    facets = [event["run"]["facets"]["answerlineage_toolCall"] for event in events]
    assert sorted(facet["traceOrigin"] for facet in facets) == [
        "header",
        "meta",
        "proxy",
        "proxy",
        "proxy",
    ]
    assert sorted(event["eventType"] for event in events) == ["COMPLETE"] * 4 + ["FAIL"]
    assert sorted(facet.get("readResolution", "") for facet in facets) == [
        "",
        "resolved",
        "resolved",
        "resolved",
        "unresolved",
    ]

    judgement = judge(events, profile="capture", received_at=RECEIVED_AT)
    assert conformant(judgement)
    assert (judgement.events, judgement.runs) == (5, 5)
    # Le proxy ne connaît pas l'agent : sa capture seule n'est pas celle d'un adaptateur.
    as_adapter = judge(events, profile="adapter", received_at=RECEIVED_AT)
    assert {"parent_missing", "no_complete_chain"} <= set(as_adapter.counts)


@pytest.mark.anyio
async def test_the_events_of_the_stdio_proxy_are_conformant_as_a_capture_point(
    tmp_path: Path,
) -> None:
    """En stdio : contexte reçu dans ``_meta`` ou créé par le proxy ; appel réussi, en
    erreur JSON-RPC, et interrompu par la mort du serveur."""
    query = {"query": f"SELECT devise, taux FROM {TABLE}"}  # noqa: S608
    async with stdio_session(tmp_path, sql_tools=SQL_TOOLS, clock=lambda: T0) as session:
        await session.send(stdio_call(1, "execute_sql", query, traceparent=TRACEPARENT))
        await session.send(stdio_call(2, "execute_sql", query))
        await session.send(stdio_call(3, "fail", {"devise": "EUR"}))
        await session.replies(3)
        await session.send(stdio_call(4, "die", {"code": 0}))
        assert await session.finished() == 0

    events = [dict(event) for event in session.events]
    facets = [event["run"]["facets"]["answerlineage_toolCall"] for event in events]
    assert {facet["transport"] for facet in facets} == {"stdio"}
    assert sorted(facet["traceOrigin"] for facet in facets) == ["meta", "proxy", "proxy", "proxy"]
    assert sorted(event["eventType"] for event in events) == ["COMPLETE"] * 3 + ["FAIL"]
    assert [event["inputs"][0]["name"] for event in events if event["inputs"]] == [TABLE] * 2

    judgement = judge(events, profile="capture", received_at=RECEIVED_AT)
    assert conformant(judgement)
    assert (judgement.events, judgement.runs) == (4, 4)


@pytest.mark.integration
@pytest.mark.anyio
async def test_an_agent_behind_the_proxy_gives_a_combined_capture_and_a_complete_chain(
    engine: AsyncEngine,
) -> None:
    played = await play("EUR", None, "Le taux de change du jour est de 1,08.")

    # Les deux émetteurs ont vu le même contexte de trace : ils émettent pour le même run.
    (from_proxy,) = played.from_proxy
    opened = played.events[1]
    assert from_proxy["run"]["runId"] == opened["run"]["runId"]
    assert "parent" not in from_proxy["run"]["facets"]
    assert from_proxy["run"]["facets"]["answerlineage_toolCall"]["traceOrigin"] == "meta"

    assert conformant(judge(played.events, profile="adapter", received_at=RECEIVED_AT))

    transport = httpx.ASGITransport(app=create_app(engine, authentication=authentication()))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://localhost", headers=signed_in()
    ) as api:
        ingested = await api.post("/api/v1/lineage/batch", json=played.events)
        assert ingested.json() == {"accepted": 4, "duplicates": 0, "rejected": []}
        while not (await process_batch(engine)).empty:
            pass
        found = await api.get(
            "/api/v1/answers", params={"channel": CHANNEL, "message_ref": MESSAGE_REF}
        )
        (answer,) = found.json()["answers"]
        sources = (await api.get(f"/api/v1/answers/{answer['id']}/sources")).json()

    # Du jeu de données à la réponse : la table lue par le proxy, l'outil, l'agent, la réponse.
    assert answer["run_id"] == AGENT_RUN
    assert answer["agent"] == AGENT
    assert sources["agents"] == [AGENT]
    assert sources["tools"] == [{"server": "entrepot", "name": "execute_sql", "calls": 1}]
    (dataset,) = sources["datasets"]
    assert (dataset["namespace"], dataset["name"]) == ("bigquery", TABLE)
    assert (dataset["precision"], dataset["columns"]) == ("columns", ["devise", "taux"])
    assert json.loads(played.received.body)["params"]["_meta"]["traceparent"] == TRACEPARENT
