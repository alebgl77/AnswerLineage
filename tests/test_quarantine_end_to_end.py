"""La quarantaine de bout en bout : de l'API qui l'active aux proxys qui en apprennent le
périmètre.

La vraie API, sur un vrai PostgreSQL, et deux proxys lancés comme ailleurs : l'un en
HTTP, servi dans le processus du test devant un faux serveur MCP, l'autre en stdio,
lancé par la ligne de commande devant le serveur MCP de test, qui le dit dans son
journal. Leur intervalle est abaissé à 1 s pour le test ; le délai mesuré est réel,
de la réponse de l'API à l'activation jusqu'à ce que chaque proxy connaisse le
périmètre.

- Une quarantaine activée est connue de chaque proxy en moins de 30 s ; levée, elle
  ne l'est plus.
- L'API injoignable au démarrage, chaque proxy démarre, relaie, et est en mode
  observer ; l'API revenue, il se synchronise.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_proxy import cli
from answerlineage_proxy.app import McpProxy
from answerlineage_proxy.config import ProxySettings
from answerlineage_proxy.credentials import ENV_CLIENT_SECRET
from answerlineage_proxy.quarantine import QuarantineScope
from api.tests.issuer import EMITTER_CLIENT, EMITTER_SECRET, LocalIssuer, serving_issuer
from proxy.tests.conftest import FakeMcpServer, exchange, running, stdio_server, tool_call
from proxy.tests.stdio_server import RESULT_TEXT
from tests.test_outbox_chaos import reserved_port, served

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

# Le critère : chaque proxy connaît le périmètre moins de 30 s après l'activation.
CRITERION_S = 30.0
INTERVAL = ["--quarantine-interval-seconds", "1"]
DATASETS = [
    {"namespace": "bigquery", "name": "ventes.suivi.commandes"},
    {"namespace": "bigquery", "name": "finance.ref.taux_change"},
]
DECLARATION = {
    "scope": {"datasets": DATASETS},
    "window": {"from": "2026-11-11T06:00:00Z", "to": "2026-11-12T12:00:00Z"},
    "severity": "high",
}
CALL = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "get_weather", "arguments": {}},
    }
).encode()


def identity(issuer: LocalIssuer) -> list[str]:
    """Les options qui donnent au proxy son jeton envers l'API ; le secret, par l'environnement."""
    return ["--oidc-issuer", issuer.url, "--oidc-client-id", EMITTER_CLIENT]


@asynccontextmanager
async def http_proxy(api_url: str, issuer: LocalIssuer) -> AsyncIterator[tuple[McpProxy, str]]:
    """Un proxy HTTP réglé comme la ligne de commande le règle, servi devant un faux serveur."""
    async with running(FakeMcpServer()) as upstream_url:
        options = ["--upstream", upstream_url, "--api-url", api_url, *INTERVAL]
        options += ["--outbox-flush-seconds", "0", *identity(issuer)]
        outbox = cli.outbox_settings_from_args(options, {})
        secret = {ENV_CLIENT_SECRET: EMITTER_SECRET}
        tokens = cli.token_source(cli.credentials_from_args(options, secret, outbox))
        sync = cli.quarantine_sync(cli.quarantine_settings_from_args(options, {}, outbox), tokens)
        assert sync is not None
        proxy = McpProxy(
            ProxySettings.create(upstream_url, "entrepot"),
            cli.event_sink(outbox, tokens),
            quarantine=sync,
        )
        async with running(proxy, transparent=True) as proxy_url:
            yield proxy, proxy_url


@dataclass
class StdioProxy:
    """Un proxy stdio lancé par la ligne de commande ; son journal, lu au fil de l'eau."""

    process: asyncio.subprocess.Process
    journal: list[str] = field(default_factory=list)

    def said(self, text: str) -> int:
        return sum(text in line for line in self.journal)

    async def call(self) -> bytes:
        assert self.process.stdin is not None and self.process.stdout is not None
        self.process.stdin.write(CALL + b"\n")
        await self.process.stdin.drain()
        return await asyncio.wait_for(self.process.stdout.readline(), 10)


@asynccontextmanager
async def stdio_proxy(api_url: str, record: Path, issuer: LocalIssuer) -> AsyncIterator[StdioProxy]:
    record.mkdir(exist_ok=True)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "answerlineage_proxy.cli",
        "--server-name",
        "meteo",
        "--api-url",
        api_url,
        *INTERVAL,
        "--outbox-flush-seconds",
        "0",
        *identity(issuer),
        "--",
        *stdio_server("--record", str(record)),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, ENV_CLIENT_SECRET: EMITTER_SECRET},
    )
    proxy = StdioProxy(process)

    async def read_journal() -> None:
        assert process.stderr is not None
        while line := await process.stderr.readline():
            proxy.journal.append(line.decode("utf-8", errors="replace"))

    reading = asyncio.ensure_future(read_journal())
    try:
        yield proxy
    finally:
        assert process.stdin is not None
        process.stdin.close()
        await asyncio.wait_for(process.wait(), 30)
        await reading


async def waited(condition: Callable[[], bool], since: float, timeout: float = 60) -> float:
    """Attend que la condition tienne ; rend le délai écoulé depuis ``since``."""
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition jamais remplie"
        await asyncio.sleep(0.02)
    return time.monotonic() - since


def perimeter(proxy: McpProxy) -> tuple[QuarantineScope, ...] | None:
    sync = proxy.quarantine
    return None if sync is None or sync.known is None else sync.known.scopes


async def test_a_quarantine_is_known_by_each_proxy_within_thirty_seconds_then_lifted(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    async with (
        serving_issuer() as (issuer, _),
        running(create_app(engine, authentication=issuer.authentication())) as api_url,
        http_proxy(api_url, issuer) as (proxy, _),
        stdio_proxy(api_url, tmp_path / "stdio", issuer) as stdio,
        httpx.AsyncClient(base_url=api_url, trust_env=False, headers=issuer.headers()) as client,
    ):
        # Les deux proxys se sont synchronisés une première fois : aucune quarantaine.
        await waited(lambda: perimeter(proxy) == (), time.monotonic())
        await waited(lambda: stdio.said("synchronisée : 0 périmètre(s)") == 1, time.monotonic())

        declared = await client.post("/api/v1/incidents", json=DECLARATION)
        incident_id = declared.json()["id"]
        activated = await client.post(
            f"/api/v1/incidents/{incident_id}/quarantine", json={"mode": "warn"}
        )
        assert activated.status_code == 200
        since = time.monotonic()
        expected = (
            QuarantineScope(
                incident_id,
                "warn",
                frozenset((item["namespace"], item["name"]) for item in DATASETS),
            ),
        )
        http_delay = await waited(lambda: perimeter(proxy) == expected, since)
        stdio_delay = await waited(
            lambda: stdio.said("synchronisée : 1 périmètre(s) en quarantaine, 2 jeu(x)") == 1,
            since,
        )
        assert http_delay < CRITERION_S
        assert stdio_delay < CRITERION_S
        status = proxy.quarantine_status()
        assert (status.mode, status.scopes, status.datasets, status.failing) == (
            "synced",
            1,
            2,
            False,
        )

        lifted = await client.post(f"/api/v1/incidents/{incident_id}/quarantine/lift")
        assert lifted.status_code == 200
        since = time.monotonic()
        assert await waited(lambda: perimeter(proxy) == (), since) < CRITERION_S
        assert (
            await waited(lambda: stdio.said("synchronisée : 0 périmètre(s)") == 2, since)
            < CRITERION_S
        )
    # Le journal ne dit que des nombres : ni l'adresse de l'API, ni un nom de jeu.
    journal = "".join(stdio.journal)
    assert api_url not in journal
    assert all(item["name"] not in journal for item in DATASETS)


async def test_an_api_down_at_startup_leaves_each_proxy_relaying_in_observe_mode_until_it_is_back(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    with reserved_port() as sock:
        api_url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        started = time.monotonic()
        async with (
            serving_issuer() as (issuer, _),
            http_proxy(api_url, issuer) as (proxy, proxy_url),
            stdio_proxy(api_url, tmp_path / "stdio", issuer) as stdio,
        ):
            # Le proxy a démarré et relaie, sans attendre l'API.
            relayed = await exchange("POST", proxy_url + "/mcp", content=tool_call())
            assert relayed.status == 200
            assert RESULT_TEXT.encode() in await stdio.call()
            assert time.monotonic() - started < 10
            await waited(lambda: proxy.quarantine_status().failing, started)
            status = proxy.quarantine_status()
            assert (status.mode, status.scopes, status.last_sync_age) == ("observe", 0, None)
            await waited(lambda: stdio.said("mode observer, aucun périmètre connu") == 1, started)

            # L'API revient, avec une quarantaine active : chaque proxy se synchronise.
            async with served(create_app(engine, authentication=issuer.authentication()), sock):
                async with httpx.AsyncClient(
                    base_url=api_url, trust_env=False, headers=issuer.headers()
                ) as client:
                    declared = await client.post("/api/v1/incidents", json=DECLARATION)
                    incident_id = declared.json()["id"]
                    activated = await client.post(
                        f"/api/v1/incidents/{incident_id}/quarantine", json={"mode": "warn"}
                    )
                    assert activated.status_code == 200
                back = time.monotonic()
                assert await waited(lambda: proxy.quarantine_status().scopes == 1, back) < 30
                assert (
                    await waited(lambda: stdio.said("synchronisée : 1 périmètre(s)") == 1, back)
                    < 30
                )
                assert stdio.said("API de nouveau joignable") == 1
                assert proxy.quarantine_status().mode == "synced"
                relayed = await exchange("POST", proxy_url + "/mcp", content=tool_call())
                assert relayed.status == 200
