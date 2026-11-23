"""La réémission de bout en bout, vers un vrai Marquez.

Des demandes de l'entreprise synthétique sont reçues par l'API d'ingestion,
projetées par un worker qui a Marquez pour destination, et réémises : chaque
appel d'outil apparaît dans Marquez, lu par l'API de Marquez, avec les jeux de
données qu'il a lus. Renvoyé, un événement ne change rien à ce que Marquez en
garde : la livraison au moins une fois est sans effet chez lui.

Marquez tourne dans son image officielle, épinglée par son empreinte, avec sa
propre base PostgreSQL, sur un réseau que Testcontainers crée pour eux.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterator
from contextlib import ExitStack
from typing import Any
from urllib.parse import quote

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_corpus.enterprise import Request, generate
from answerlineage_corpus.events import request_events
from answerlineage_workers.destination import Sender, parse_destination
from answerlineage_workers.queue import run_worker
from answerlineage_workers.reemission import Reemission, body

# Marquez pèse près d'un gigaoctet : le test ne tourne que là où on le demande, dans une
# seule case de la matrice du pipeline, et sur le poste à la demande.
CATALOG = "ANSWERLINEAGE_TEST_CATALOG"
pytestmark = [
    pytest.mark.integration,
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.environ.get(CATALOG) != "1",
        reason=f"Catalogue non visé : {CATALOG}=1 lance ce test sur un vrai Marquez.",
    ),
]

# Marquez 0.51.1, publié le 27 mars 2025.
MARQUEZ_IMAGE = (
    "marquezproject/marquez:0.51.1"
    "@sha256:0721c976cff17d8b14f7949d85d6dac9c7ea37cb9fe857caa19833730fcb1a50"
)
LINEAGE = "/api/v1/lineage"


@pytest.fixture(scope="module")
def marquez(postgres_image: str) -> Iterator[str]:
    """L'adresse de l'API d'un Marquez jetable, et de sa base."""
    with ExitStack() as stack:
        try:
            from testcontainers.community.postgres import PostgresContainer
            from testcontainers.core.container import DockerContainer
            from testcontainers.core.network import Network
            from testcontainers.core.wait_strategies import HttpWaitStrategy

            network = stack.enter_context(Network())
            stack.enter_context(
                PostgresContainer(
                    postgres_image,
                    # Les identifiants qu'attend la configuration livrée dans l'image :
                    # une base jetable, sur un réseau propre au test.
                    username="marquez",
                    password="marquez",  # noqa: S106
                    dbname="marquez",
                    driver=None,
                )
                .with_network(network)
                .with_network_aliases("marquez-db")
            )
            server = stack.enter_context(
                DockerContainer(MARQUEZ_IMAGE)
                .with_network(network)
                .with_envs(
                    MARQUEZ_CONFIG="marquez.dev.yml",
                    MARQUEZ_PORT="5000",
                    MARQUEZ_ADMIN_PORT="5001",
                    POSTGRES_HOST="marquez-db",
                    POSTGRES_PORT="5432",
                )
                .with_exposed_ports(5000, 5001)
                .waiting_for(HttpWaitStrategy(5001, "/healthcheck").for_status_code(200))
            )
        except Exception as error:
            # En CI, un test d'intégration sauté serait un faux vert.
            if os.environ.get("CI"):
                raise
            pytest.skip(f"Marquez indisponible (Docker requis) : {type(error).__name__}")
        yield f"http://{server.get_container_host_ip()}:{server.get_exposed_port(5000)}"


def requests_with_calls() -> list[Request]:
    enterprise = generate(7, requests=12, days=1, incidents=0)
    chosen = [request for request in enterprise.requests if request.calls][:3]
    assert chosen
    return chosen


async def until_empty(engine: AsyncEngine, *, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while True:
        async with engine.connect() as conn:
            left = (
                await conn.execute(
                    text(
                        "SELECT (SELECT count(*) FROM ingest_queue)"
                        " + (SELECT count(*) FROM reemission_queue)"
                    )
                )
            ).scalar_one()
        if left == 0:
            return
        assert time.monotonic() < deadline, f"{left} événement(s) encore en file"
        await asyncio.sleep(0.1)


async def run_in_marquez(marquez: httpx.AsyncClient, run_id: object) -> dict[str, Any]:
    response = await marquez.get(f"/api/v1/jobs/runs/{run_id}")
    assert response.status_code == 200, response.status_code
    found: dict[str, Any] = response.json()
    return found


async def runs_of_its_job(marquez: httpx.AsyncClient, run: dict[str, Any]) -> list[str]:
    job = run["jobVersion"]
    response = await marquez.get(
        f"/api/v1/namespaces/{quote(job['namespace'], safe='')}"
        f"/jobs/{quote(job['name'], safe='')}/runs"
    )
    assert response.status_code == 200, response.status_code
    return sorted(found["id"] for found in response.json()["runs"])


async def test_a_run_ingested_and_projected_appears_in_marquez_with_its_input_datasets(
    engine: AsyncEngine, marquez: str
) -> None:
    requests = requests_with_calls()
    events = [event for request in requests for event in request_events(request)]
    transport = httpx.ASGITransport(app=create_app(engine))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as api:
        accepted = await api.post("/api/v1/lineage/batch", json=events)
    assert accepted.json() == {"accepted": len(events), "duplicates": 0, "rejected": []}

    stop = asyncio.Event()
    settings = Reemission(parse_destination(marquez + LINEAGE))
    worker = asyncio.create_task(run_worker(engine, stop, idle_delay=0.05, reemission=settings))
    try:
        await until_empty(engine)
    finally:
        stop.set()
        await asyncio.wait_for(worker, timeout=10)
    async with engine.connect() as conn:
        sent = (await conn.execute(text("SELECT sent FROM reemission_stats"))).scalar_one()
    assert sent == len(events)

    async with httpx.AsyncClient(base_url=marquez) as client:
        for request in requests:
            for call in request.calls:
                run = await run_in_marquez(client, call.run_id)
                assert run["state"] == "COMPLETED"
                assert run["jobVersion"]["namespace"] == f"mcp://{call.tool.server}"
                read = {
                    (found["datasetVersionId"]["namespace"], found["datasetVersionId"]["name"])
                    for found in run["inputDatasetVersions"]
                }
                assert read == {(item.table.namespace, item.table.name) for item in call.reads}
                # Les facettes propres d'AnswerLineage voyagent avec l'événement.
                assert run["facets"]["answerlineage_toolCall"]["tool"] == call.tool.name
                assert run["facets"]["parent"]["run"]["runId"] == str(request.run_id)

        # Au moins une fois : renvoyé, un événement ne change rien à ce que Marquez garde.
        request, call = requests[0], requests[0].calls[0]
        before = await run_in_marquez(client, call.run_id)
        runs = await runs_of_its_job(client, before)
        sender = Sender(settings.destination)
        try:
            for event in request_events(request):
                assert (await sender.send(body(event))).verdict == "sent"
        finally:
            sender.close()
        after = await run_in_marquez(client, call.run_id)
        assert after["inputDatasetVersions"] == before["inputDatasetVersions"]
        assert after["state"] == before["state"]
        assert await runs_of_its_job(client, after) == runs
