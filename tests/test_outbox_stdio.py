"""Le mode stdio remet ses événements par la boîte d'envoi, jusqu'au graphe.

Le client lance le proxy en ligne de commande, avec l'adresse de l'API, devant
le serveur MCP de test en stdio ; la vraie API reçoit, sur un vrai PostgreSQL,
et le worker projette. L'arrêt suit la fin du serveur : la boîte s'y vide.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_proxy.credentials import ENV_CLIENT_SECRET
from answerlineage_workers.queue import process_batch
from api.tests.issuer import EMITTER_CLIENT, EMITTER_SECRET, serving_issuer
from proxy.tests.conftest import running, stdio_server
from proxy.tests.stdio_server import RESULT_TEXT

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

CALL = {"name": "get_weather", "arguments": {"location": "Séville"}}
SENT = b"".join(
    json.dumps(message, ensure_ascii=False).encode() + b"\n"
    for message in (
        {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": CALL},
    )
)


async def proxy_in_stdio(
    *options: str, record: Path, environment: Mapping[str, str] | None = None
) -> tuple[int, bytes, bytes]:
    """Lance ``answerlineage-proxy [options] -- <serveur>`` comme un client MCP le lance."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "answerlineage_proxy.cli",
        "--server-name",
        "meteo",
        *options,
        "--",
        *stdio_server("--record", str(record)),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **(environment or {})},
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(SENT), timeout=60)
    assert process.returncode is not None
    return process.returncode, stdout, stderr


async def test_a_tool_call_relayed_in_stdio_reaches_the_graph_through_the_outbox(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    # Le proxy obtient son jeton de l'émetteur servi en HTTP, par client_credentials, son
    # secret lu de l'environnement ; l'API vérifie ce jeton par les clés du même émetteur.
    async with (
        serving_issuer() as (issuer, served),
        running(create_app(engine, authentication=issuer.authentication())) as api_url,
    ):
        code, stdout, stderr = await proxy_in_stdio(
            "--api-url",
            api_url,
            "--oidc-issuer",
            issuer.url,
            "--oidc-client-id",
            EMITTER_CLIENT,
            record=tmp_path,
            environment={ENV_CLIENT_SECRET: EMITTER_SECRET},
        )

    assert code == 0
    assert RESULT_TEXT.encode() in stdout
    assert "boîte d'envoi".encode() in stderr
    # Un jeton obtenu, jamais écrit au journal, pas plus que le secret.
    assert len(served.issued) >= 1
    for secret in (EMITTER_SECRET, *served.issued):
        assert secret.encode() not in stderr + stdout
    while not (await process_batch(engine)).empty:
        pass
    async with engine.connect() as conn:
        events = (
            await conn.execute(
                text(
                    "SELECT emitter, payload -> 'run' -> 'facets' -> 'answerlineage_toolCall'"
                    " ->> 'transport' FROM event_raw WHERE tool_call"
                )
            )
        ).all()
        runs = (await conn.execute(text("SELECT count(*) FROM run"))).scalar_one()
        queued = (await conn.execute(text("SELECT count(*) FROM ingest_queue"))).scalar_one()
        reported = (
            await conn.execute(
                text(
                    "SELECT sum(calls_seen), sum(events_emitted), sum(events_lost)"
                    " FROM coverage_report"
                )
            )
        ).one()
    assert [tuple(row) for row in events] == [("mcp://meteo", "stdio")]
    assert (runs, queued) == (1, 0)
    # Le dernier relevé, remis à l'arrêt, dit l'appel vu et son événement remis.
    assert tuple(reported) == (1, 1, 0)
