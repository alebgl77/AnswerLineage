"""D'une règle de lecture au graphe, et d'un outil sans règle à la liste des outils à décrire.

Le vrai proxy, devant un serveur MCP de test, décrit un outil d'API par sa règle
et marque un outil qui n'en a pas ; ses événements passent le kit de conformité,
l'API les reçoit, un worker les projette. La lecture déclarée entre dans le
graphe, la clé lue reste dans l'événement sous son empreinte, et l'outil à
décrire se lit par l'API, sans ouvrir le journal du proxy.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_conformance.judgement import judge
from answerlineage_proxy.app import McpProxy
from answerlineage_proxy.config import ProxySettings
from answerlineage_proxy.fingerprint import FingerprintKey
from answerlineage_proxy.rules import KEY_LABEL, parse_read_rules
from answerlineage_proxy.sink import MemorySink
from answerlineage_workers.queue import process_batch
from api.tests.issuer import authentication, signed_in
from proxy.tests.conftest import FakeMcpServer, exchange, running, tool_call

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

KEY = FingerprintKey(bytes(range(32)))
CANARY = "canari-produit-e2e-51aa"
RULES = parse_read_rules(
    """\
version: 1
rules:
  - tool: get_price
    datasets:
      - namespace: postgres://referentiel:5432
        name: ref.tarifs
        columns: [produit, prix]
    key: produit
"""
)


async def test_a_declared_read_reaches_the_graph_and_an_undescribed_tool_the_list(
    engine: AsyncEngine,
) -> None:
    upstream = FakeMcpServer()
    sink = MemorySink()
    async with running(upstream) as upstream_url:
        settings = ProxySettings.create(
            upstream_url, "catalogue", fingerprint_key=KEY, read_rules=RULES
        )
        proxy = McpProxy(settings, sink)
        async with running(proxy, transparent=True) as proxy_url:
            for body in (
                tool_call("get_price", {"produit": CANARY}),
                tool_call("get_weather", {"location": "Lyon"}),
            ):
                assert (await exchange("POST", f"{proxy_url}/mcp", content=body)).status == 200
    events = [dict(event) for event in sink.events]
    received_at = datetime.now(UTC) + timedelta(minutes=1)
    judgement = judge(events, profile="capture", received_at=received_at)
    assert [(problem.code, problem.pointer) for problem in judgement.problems] == []

    transport = httpx.ASGITransport(app=create_app(engine, authentication=authentication()))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://localhost", headers=signed_in()
    ) as api:
        ingested = await api.post("/api/v1/lineage/batch", json=events)
        assert ingested.json() == {"accepted": 2, "duplicates": 0, "rejected": []}
        while not (await process_batch(engine)).empty:
            pass
        listed = (await api.get("/api/v1/ingestion/undescribed-tools")).json()["tools"]

    assert [(tool["emitter"], tool["tool"], tool["calls"]) for tool in listed] == [
        ("mcp://catalogue", "get_weather", 1)
    ]
    async with engine.connect() as conn:
        reads = (
            await conn.execute(
                text(
                    "SELECT dataset.namespace, dataset.name, read_edge.columns FROM read_edge"
                    " JOIN dataset ON dataset.id = read_edge.dataset_id"
                )
            )
        ).all()
        stored = (await conn.execute(text("SELECT payload::text FROM event_raw"))).scalars().all()
    assert [tuple(row) for row in reads] == [
        ("postgres://referentiel:5432", "ref.tarifs", ["prix", "produit"])
    ]
    # La clé lue n'est conservée que sous son empreinte à clé.
    fingerprint = KEY.fingerprint(KEY_LABEL, json.dumps(CANARY).encode())
    assert any(fingerprint in payload for payload in stored)
    assert all(CANARY not in payload for payload in stored)
