"""Un jeton qui traverse le proxy ne laisse aucune trace dans AnswerLineage.

Le parcours complet : un appel d'outil relayé par le proxy, son événement
envoyé à l'API d'ingestion, projeté par un worker dans le graphe, puis relu.
Le jeton, le cookie, le paramètre recopié en en-tête, l'argument de l'outil et
son résultat sont cherchés dans les événements, la base entière, les réponses
de l'API, les compteurs du proxy et tous les journaux.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_proxy.app import McpProxy
from answerlineage_proxy.config import ProxySettings, SqlTool
from answerlineage_proxy.sink import MemorySink
from answerlineage_workers.queue import process_batch
from proxy.tests.conftest import (
    MODERN_META,
    TOOL_RESULT,
    FakeMcpServer,
    exchange,
    running,
    tool_call,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

# Canaris : des valeurs inventées, reconnaissables, qui ne sont pas des secrets.
BEARER = "canari-jeton-9f27c1"
COOKIE = "canari-cookie-55aa"
PARAMETER = "canari-parametre-71be"
IN_URL = "canari-url-0c4d"
ARGUMENT = "canari-argument-3e88"
# Le résultat de l'outil est du contenu, au même titre que son argument.
CANARIES = (BEARER, COOKIE, PARAMETER, IN_URL, ARGUMENT, "canari", "Séville", "Il fait beau")


async def dump(engine: AsyncEngine) -> str:
    """Toute la base, ligne par ligne, sous forme de texte."""
    lines: list[str] = []
    async with engine.connect() as conn:
        tables = await conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
        )
        for table in tables.scalars().all():
            rows = await conn.execute(
                text(f'SELECT CAST(row_to_json(t) AS text) FROM "{table}" AS t')  # noqa: S608
            )
            lines.extend(rows.scalars())
    return "\n".join(lines)


async def test_a_token_crosses_the_whole_path_without_leaving_a_trace(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    upstream = FakeMcpServer()
    sink = MemorySink()
    query = f"SELECT taux FROM finance.ref.taux_change WHERE devise = '{ARGUMENT}'"  # noqa: S608
    headers = [
        ("content-type", "application/json"),
        ("mcp-protocol-version", "2026-07-28"),
        ("mcp-method", "tools/call"),
        ("mcp-name", "execute_sql"),
        ("mcp-param-region", PARAMETER),
        ("authorization", f"Bearer {BEARER}"),
        ("cookie", f"session={COOKIE}"),
    ]
    seen: list[str] = []

    with caplog.at_level(logging.DEBUG):
        async with running(upstream) as upstream_url:
            settings = ProxySettings.create(
                upstream_url,
                "entrepot",
                sql_tools={"execute_sql": SqlTool("query", "bigquery", "bigquery")},
            )
            proxy = McpProxy(settings, sink)
            async with running(proxy, transparent=True) as proxy_url:
                relayed = await exchange(
                    "POST",
                    f"{proxy_url}/mcp?access_token={IN_URL}",
                    headers=headers,
                    content=tool_call("execute_sql", {"query": query}, **MODERN_META),
                )
                await proxy.drain()
            seen.append(repr(proxy.stats))

        # Le jeton est bien passé : le serveur MCP l'a reçu, avec le reste de l'appel.
        assert (relayed.status, relayed.body) == (200, TOOL_RESULT)
        (received,) = upstream.requests
        assert received.header("authorization") == [f"Bearer {BEARER}"]
        assert received.header("mcp-param-region") == [PARAMETER]
        assert IN_URL.encode() in received.query
        assert ARGUMENT.encode() in received.body

        events = list(sink.events)
        assert len(events) == 1
        seen.append(json.dumps(events, ensure_ascii=False))

        transport = httpx.ASGITransport(app=create_app(engine))
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as api:
            ingested = await api.post("/api/v1/lineage/batch", json=events)
            assert ingested.json() == {"accepted": 1, "duplicates": 0, "rejected": []}
            while not (await process_batch(engine)).empty:
                pass
            searched = await api.get(
                "/api/v1/answers", params={"channel": "slack", "message_ref": BEARER}
            )
            seen.extend([ingested.text, searched.text])

        stored = await dump(engine)
        seen.append(stored)

    # Le parcours a bien eu lieu : l'appel et la table lue sont dans le graphe.
    assert '"kind":"tool"' in stored
    assert "finance.ref.taux_change" in stored
    assert '"columns":["devise","taux"]' in stored

    seen.append(caplog.text)
    for canary in CANARIES:
        for place in seen:
            assert canary not in place
