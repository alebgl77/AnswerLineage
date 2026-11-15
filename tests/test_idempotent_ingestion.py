"""Un événement rejoué est sans effet sur le graphe, et se compte.

Le chemin est le vrai : l'API reçoit, la file attend, un worker projette.
"""

from __future__ import annotations

import itertools
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_workers.queue import process_batch
from api.tests.events import facet, run_event

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


def api(engine: AsyncEngine) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app(engine))
    return httpx.AsyncClient(transport=transport, base_url="http://localhost")


async def rows(engine: AsyncEngine, sql: str) -> list[tuple[Any, ...]]:
    async with engine.connect() as conn:
        return [tuple(row) for row in await conn.execute(text(sql))]


async def drain(engine: AsyncEngine) -> None:
    while not (await process_batch(engine)).empty:
        pass


async def duplicates_counted(client: httpx.AsyncClient) -> int:
    response = await client.get("/api/v1/ingestion/duplicates", params={"limit": 1000})
    assert response.status_code == 200
    return sum(row["count"] for row in response.json()["duplicates"])


async def test_an_event_sent_again_three_times_leaves_one_edge_and_counts_three(
    engine: AsyncEngine,
) -> None:
    event = run_event(0)
    async with api(engine) as client:
        first = await client.post("/api/v1/lineage", json=event)
        assert (first.status_code, first.json()["duplicate"]) == (201, False)
        await drain(engine)
        before = await duplicates_counted(client)
        assert before == 0

        # Trois renvois : un seul, puis un lot qui contient l'événement deux fois.
        alone = await client.post("/api/v1/lineage", json=event)
        assert (alone.status_code, alone.json()["duplicate"]) == (201, True)
        twice = await client.post("/api/v1/lineage/batch", json=[event, event])
        assert twice.json() == {"accepted": 2, "duplicates": 2, "rejected": []}

        # La file était vide : les renvois n'y ont rien remis.
        assert await rows(engine, "SELECT count(*) FROM ingest_queue") == [(0,)]
        await drain(engine)
        assert await duplicates_counted(client) - before == 3

    assert await rows(engine, "SELECT count(*) FROM ingest_queue") == [(0,)]
    assert await rows(engine, "SELECT count(*) FROM event_raw") == [(1,)]
    assert await rows(engine, "SELECT count(*) FROM read_edge") == [(1,)]
    assert await rows(
        engine,
        "SELECT r.run_id::text, d.namespace, d.name FROM read_edge e"
        " JOIN run r ON r.run_id = e.run_id JOIN dataset d ON d.id = e.dataset_id",
    ) == [(event["run"]["runId"], "bigquery", "finance.ref.taux_change")]
    assert await rows(engine, "SELECT count(*) FROM run WHERE kind = 'tool'") == [(1,)]
    assert await rows(engine, "SELECT count(*) FROM dataset") == [(1,)]


async def test_replays_mixed_with_new_events_in_batches_change_nothing(
    engine: AsyncEngine,
) -> None:
    events = [run_event(index) for index in range(4)]
    async with api(engine) as client:
        await client.post("/api/v1/lineage/batch", json=events[:2])
        await drain(engine)
        reference = await graph(engine)

        await client.post("/api/v1/lineage/batch", json=[events[0], events[1], events[0]])
        await drain(engine)
        assert await graph(engine) == reference
        assert await duplicates_counted(client) == 3

        await client.post(
            "/api/v1/lineage/batch", json=[events[1], events[2], events[3], events[3]]
        )
        await drain(engine)
        assert await duplicates_counted(client) == 3 + 2
    assert await rows(engine, "SELECT count(*) FROM read_edge") == [(4,)]
    assert await rows(engine, "SELECT count(*) FROM ingest_queue") == [(0,)]


def lifecycle() -> list[dict[str, Any]]:
    """Trois événements d'un même run d'outil, qui s'enrichissent l'un l'autre."""
    base = run_event(0)
    table = {"namespace": "bigquery", "name": "finance.ref.taux_change"}
    other = {"namespace": "bigquery", "name": "finance.ref.devises"}
    columns = {"schema": facet(fields=[{"name": "devise"}, {"name": "taux"}])}
    start = {
        **base,
        "eventType": "START",
        "eventTime": "2026-11-02T09:00:00Z",
        "inputs": [],
    }
    running = {
        **base,
        "eventType": "RUNNING",
        "eventTime": "2026-11-02T09:00:01Z",
        "inputs": [{**table, "facets": {}}],
    }
    complete = {
        **base,
        "eventType": "COMPLETE",
        "eventTime": "2026-11-02T09:00:02Z",
        "inputs": [{**table, "facets": columns}, {**other, "facets": {}}],
    }
    return [start, running, complete]


async def graph(engine: AsyncEngine) -> dict[str, list[tuple[Any, ...]]]:
    """Le graphe sans ses identifiants ni ses heures d'enregistrement."""
    return {
        "run": await rows(
            engine,
            "SELECT run_id::text, kind, parent_run_id::text, state, state_time, started_at,"
            " ended_at"
            " FROM run ORDER BY run_id",
        ),
        "tool": await rows(engine, "SELECT server, name FROM tool ORDER BY 1, 2"),
        "dataset": await rows(engine, "SELECT namespace, name, kind FROM dataset ORDER BY 1, 2"),
        "read": await rows(
            engine,
            "SELECT e.run_id::text, d.namespace, d.name, e.precision, e.columns, e.origin,"
            " e.event_time FROM read_edge e JOIN dataset d ON d.id = e.dataset_id"
            " ORDER BY 1, 2, 3",
        ),
        "write": await rows(engine, "SELECT count(*) FROM write_edge"),
    }


@pytest.mark.parametrize(
    "drain_between", [True, False], ids=["file vidée à chaque envoi", "file vidée à la fin"]
)
async def test_arrival_order_and_replays_do_not_change_the_graph(
    engine: AsyncEngine, drain_between: bool
) -> None:
    events = lifecycle()
    truncate = text(
        "TRUNCATE ingest_queue, event_raw, ingest_duplicate, read_edge, write_edge,"
        " run, tool, agent, dataset_version, dataset RESTART IDENTITY CASCADE"
    )

    async def receive(arrival: list[dict[str, Any]]) -> None:
        """Les événements arrivent dans cet ordre, chacun suivi du rejeu d'un précédent."""
        async with api(engine) as client:
            for position, event in enumerate(arrival):
                assert (await client.post("/api/v1/lineage", json=event)).status_code == 201
                if drain_between:
                    await drain(engine)
                replayed = arrival[position // 2]
                await client.post("/api/v1/lineage/batch", json=[replayed, event])
        await drain(engine)

    await receive(events)
    reference = await graph(engine)
    assert len(reference["read"]) == 2
    assert reference["run"][0][3] == "COMPLETE"

    for arrival in itertools.permutations(events):
        async with engine.begin() as conn:
            await conn.execute(truncate)
        await receive(list(arrival))
        assert await graph(engine) == reference, [event["eventType"] for event in arrival]
        assert await rows(engine, "SELECT count(*) FROM ingest_queue") == [(0,)]
