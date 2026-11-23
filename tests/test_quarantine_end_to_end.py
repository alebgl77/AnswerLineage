"""La quarantaine de bout en bout : de l'API qui l'active aux proxys qui en apprennent le
périmètre.

La vraie API, sur un vrai PostgreSQL, et deux proxys lancés comme ailleurs : l'un en
HTTP, servi dans le processus du test devant un faux serveur MCP, l'autre en stdio,
lancé par la ligne de commande devant le serveur MCP de test, qui le dit dans son
journal. Leur intervalle est abaissé à 1 s pour le test ; le délai mesuré est réel,
de la réponse de l'API à l'activation jusqu'à ce que chaque proxy connaisse le
périmètre.

- Une quarantaine activée est connue de chaque proxy en moins de 30 s ; levée, elle
  ne l'est plus. L'API signe la diffusion, et les deux proxys, qui partagent son
  secret, la vérifient : ils pourront annoter. Un troisième, réglé sur un autre
  secret, écarte chaque réponse et reste en mode observer.
- L'API injoignable au démarrage, chaque proxy démarre, relaie, et est en mode
  observer ; l'API revenue, il se synchronise.
- Une vue que le graphe connaît, apprise par l'ingestion d'un job qui lit la table
  en quarantaine, entre dans la zone que le worker calcule ; le proxy l'apprend
  avec le périmètre, à sa profondeur, et l'oublie à la levée.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_api.incidents.signature import QuarantineSigning
from answerlineage_proxy import cli
from answerlineage_proxy.app import McpProxy
from answerlineage_proxy.config import ProxySettings
from answerlineage_proxy.credentials import ENV_CLIENT_SECRET
from answerlineage_proxy.quarantine import DerivedDataset, QuarantineScope
from answerlineage_proxy.quarantine_signature import ENV_SECRET as ENV_QUARANTINE_SECRET
from answerlineage_workers.queue import run_worker
from api.tests.events import PRODUCER, SCHEMA_URL
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
# Le secret que l'API et les proxys partagent, tiré pour la session.
SHARED = "partage-" + secrets.token_urlsafe(32)
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
async def http_proxy(
    api_url: str, issuer: LocalIssuer, shared: str = SHARED
) -> AsyncIterator[tuple[McpProxy, str]]:
    """Un proxy HTTP réglé comme la ligne de commande le règle, servi devant un faux serveur."""
    async with running(FakeMcpServer()) as upstream_url:
        options = ["--upstream", upstream_url, "--api-url", api_url, *INTERVAL]
        options += ["--outbox-flush-seconds", "0", *identity(issuer)]
        outbox = cli.outbox_settings_from_args(options, {})
        secret = {ENV_CLIENT_SECRET: EMITTER_SECRET}
        tokens = cli.token_source(cli.credentials_from_args(options, secret, outbox))
        diffusion = {ENV_QUARANTINE_SECRET: shared}
        settings = cli.quarantine_settings_from_args(options, diffusion, outbox)
        sync = cli.quarantine_sync(settings, tokens)
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
        env={**os.environ, ENV_CLIENT_SECRET: EMITTER_SECRET, ENV_QUARANTINE_SECRET: SHARED},
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
    signing = QuarantineSigning.create(SHARED)
    stranger = "autre-" + secrets.token_urlsafe(32)
    async with (
        serving_issuer() as (issuer, _),
        running(
            create_app(engine, authentication=issuer.authentication(), quarantine_signing=signing)
        ) as api_url,
        http_proxy(api_url, issuer) as (proxy, _),
        http_proxy(api_url, issuer, stranger) as (misled, _),
        stdio_proxy(api_url, tmp_path / "stdio", issuer) as stdio,
        httpx.AsyncClient(base_url=api_url, trust_env=False, headers=issuer.headers()) as client,
    ):
        # Les deux proxys se sont synchronisés une première fois : aucune quarantaine.
        await waited(lambda: perimeter(proxy) == (), time.monotonic())
        await waited(lambda: stdio.said("synchronisée : 0 périmètre(s)") == 1, time.monotonic())
        assert proxy.quarantine_status().annotating
        assert stdio.said("seule une diffusion signée par l'API") == 1

        declared = await client.post("/api/v1/incidents", json=DECLARATION)
        incident_id = declared.json()["id"]
        activated = await client.post(
            f"/api/v1/incidents/{incident_id}/quarantine", json={"mode": "warn"}
        )
        assert activated.status_code == 200
        since = time.monotonic()
        # L'heure d'activation, posée par la base, est celle que le proxy apprend.
        activated_at = datetime.fromisoformat(activated.json()["quarantine"]["activated_at"])
        expected = (
            QuarantineScope(
                incident_id,
                "warn",
                frozenset((item["namespace"], item["name"]) for item in DATASETS),
                activated_at,
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
        assert status.annotating
        # Le proxy réglé sur un autre secret n'a rien cru de l'API.
        misled_status = misled.quarantine_status()
        assert (misled_status.mode, misled_status.failing, misled_status.annotating) == (
            "observe",
            True,
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
    # Le journal ne dit que des nombres : ni l'adresse de l'API, ni un nom de jeu, ni le secret.
    journal = "".join(stdio.journal)
    assert api_url not in journal
    assert all(item["name"] not in journal for item in DATASETS)
    assert SHARED not in journal


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
            api = create_app(
                engine,
                authentication=issuer.authentication(),
                quarantine_signing=QuarantineSigning.create(SHARED),
            )
            async with served(api, sock):
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


def job(reads: str, writes: str) -> dict[str, Any]:
    """Un job de transformation, émis en OpenLineage : il lit une table et en écrit une autre."""
    return {
        "eventType": "COMPLETE",
        "eventTime": "2026-11-16T07:00:00Z",
        "producer": PRODUCER,
        "schemaURL": SCHEMA_URL,
        "run": {"runId": str(uuid4()), "facets": {}},
        "job": {"namespace": "dbt://entrepot", "name": writes},
        "inputs": [{"namespace": "bigquery", "name": reads}],
        "outputs": [{"namespace": "bigquery", "name": writes}],
    }


async def written(engine: AsyncEngine, count: int) -> None:
    """Attend que le worker ait projeté ces écritures dans le graphe."""
    for _ in range(3000):
        async with engine.connect() as conn:
            if (await conn.execute(text("SELECT count(*) FROM write_edge"))).scalar_one() >= count:
                return
        await asyncio.sleep(0.01)
    raise AssertionError("les écritures n'ont pas été projetées à temps")


def zone(proxy: McpProxy) -> frozenset[DerivedDataset] | None:
    scopes = perimeter(proxy)
    return None if not scopes or scopes[0].zone_computed_at is None else scopes[0].derived


async def test_a_view_known_to_the_graph_is_learned_by_a_proxy_with_the_perimeter(
    engine: AsyncEngine,
) -> None:
    daily, revenue = "ventes.vues.commandes_du_jour", "finance.marts.chiffre_affaires"
    stop = asyncio.Event()
    worker = asyncio.ensure_future(
        run_worker(engine, stop, idle_delay=0.01, zone_check_every=timedelta(seconds=0.1))
    )
    try:
        async with (
            serving_issuer() as (issuer, _),
            running(
                create_app(
                    engine,
                    authentication=issuer.authentication(),
                    quarantine_signing=QuarantineSigning.create(SHARED),
                )
            ) as api_url,
            http_proxy(api_url, issuer) as (proxy, _),
            httpx.AsyncClient(
                base_url=api_url, trust_env=False, headers=issuer.headers()
            ) as client,
        ):
            # Le graphe apprend, par l'ingestion, une vue de la table et une table de la vue.
            for event in (job(DATASETS[0]["name"], daily), job(daily, revenue)):
                assert (await client.post("/api/v1/lineage", json=event)).status_code == 201
            await written(engine, 2)

            declared = await client.post("/api/v1/incidents", json=DECLARATION)
            incident_id = declared.json()["id"]
            activated = await client.post(
                f"/api/v1/incidents/{incident_id}/quarantine", json={"mode": "warn"}
            )
            assert activated.status_code == 200
            since = time.monotonic()

            delay = await waited(lambda: bool(zone(proxy)), since)
            assert delay < CRITERION_S
            assert zone(proxy) == frozenset(
                {DerivedDataset("bigquery", daily, 1), DerivedDataset("bigquery", revenue, 2)}
            )
            status = proxy.quarantine_status()
            assert (status.scopes, status.datasets, status.derived, status.annotating) == (
                1,
                2,
                2,
                True,
            )
            read = (await client.get(f"/api/v1/incidents/{incident_id}")).json()
            assert read["quarantine"]["zone"]["derived"] == 2

            assert (
                await client.post(f"/api/v1/incidents/{incident_id}/quarantine/lift")
            ).status_code == 200
            await waited(lambda: perimeter(proxy) == (), time.monotonic())
    finally:
        stop.set()
        await asyncio.wait_for(worker, timeout=30)
