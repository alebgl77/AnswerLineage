"""La couverture du lignage de bout en bout : ingestion, workers, relevé, lecture.

Un émetteur a relayé 1 000 appels ; 990 de leurs événements arrivent dans le
graphe par le chemin réel. La couverture lue vaut 99 %, et la fenêtre de la
perte est indiquée, que l'émetteur ait vu la perte ou non.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_workers.queue import process_batch
from api.tests.events import run_event

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

WINDOW = {"from": "2026-11-02T09:00:00Z", "to": "2026-11-02T09:01:00Z"}
INCIDENT = {
    "namespace": "bigquery",
    "name": "finance.ref.taux_change",
    "from": "2026-11-02T08:00:00Z",
    "to": "2026-11-02T09:01:00Z",
}


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(engine))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield client


async def deliver_990_calls(client: httpx.AsyncClient, engine: AsyncEngine) -> None:
    """990 appels de ``mcp://entrepot`` dans la fenêtre, projetés par les workers."""
    events = [run_event(index) for index in range(990)]
    assert {event["job"]["namespace"] for event in events} == {"mcp://entrepot"}
    assert all(event["eventTime"].startswith("2026-11-02T09:00:") for event in events)
    response = await client.post("/api/v1/lineage/batch", json=events)
    assert response.json() == {"accepted": 990, "duplicates": 0, "rejected": []}
    while not (await process_batch(engine)).empty:
        pass


async def publish(client: httpx.AsyncClient, *, emitted: int, lost: int) -> None:
    report = {
        "emitter": "mcp://entrepot",
        "window": WINDOW,
        "calls_seen": 1000,
        "events_emitted": emitted,
        "events_lost": lost,
    }
    assert (await client.post("/api/v1/coverage", json=report)).status_code == 201


async def read(client: httpx.AsyncClient) -> tuple[dict[str, Any], dict[str, Any]]:
    """La couverture de l'émetteur, et celle du résultat d'impact sur la même période."""
    response = await client.get("/api/v1/coverage", params=WINDOW)
    [emitter] = response.json()["emitters"]
    impact = await client.get("/api/v1/impact", params=INCIDENT)
    assert impact.status_code == 200
    return emitter["coverage"], impact.json()["coverage"]


def holes(coverage: dict[str, Any]) -> list[tuple[str, dict[str, str], int | None]]:
    return [(hole["kind"], hole["window"], hole["count"]) for hole in coverage["holes"]]


async def test_1000_calls_with_10_events_lost_give_99_percent_and_the_window_of_the_loss(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    await deliver_990_calls(client, engine)
    await publish(client, emitted=990, lost=10)

    by_emitter, in_impact = await read(client)
    assert by_emitter["status"] == "measured"
    assert by_emitter["rate"] == 0.99
    assert (by_emitter["calls_seen"], by_emitter["events_in_graph"]) == (1000, 990)
    assert holes(by_emitter) == [("events_lost", WINDOW, 10), ("missing_from_graph", WINDOW, 10)]
    # Le résultat d'impact porte la même mesure : sa fenêtre commence avant le premier
    # relevé, il dit donc aussi que ce début n'est pas mesuré.
    assert in_impact["rate"] == 0.99
    assert in_impact["status"] == "partial"
    assert holes(in_impact) == [
        ("before_first_report", {"from": INCIDENT["from"], "to": WINDOW["from"]}, None),
        ("events_lost", WINDOW, 10),
        ("missing_from_graph", WINDOW, 10),
    ]


async def test_events_lost_on_the_way_show_even_when_the_emitter_believes_it_sent_them(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    await deliver_990_calls(client, engine)
    await publish(client, emitted=1000, lost=0)

    by_emitter, in_impact = await read(client)
    assert by_emitter["rate"] == in_impact["rate"] == 0.99
    assert holes(by_emitter) == [("missing_from_graph", WINDOW, 10)]
    assert ("missing_from_graph", WINDOW, 10) in holes(in_impact)
