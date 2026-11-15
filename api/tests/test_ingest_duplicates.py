"""Le compteur de doublons : par émetteur et par jour, dans la transaction de réception."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_api.ingest.duplicates import emitter_of
from answerlineage_api.ingest.models import validate_event
from answerlineage_api.ingest.store import AcceptedEvent, event_identifier, store_events
from api.tests.events import mutated, run_event

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

BATCH = "/api/v1/lineage/batch"
SINGLE = "/api/v1/lineage"
DUPLICATES = "/api/v1/ingestion/duplicates"


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(engine))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield client


def event_of(namespace: str, index: int = 0) -> dict[str, Any]:
    event: dict[str, Any] = mutated(["job", "namespace"], namespace, base=run_event(index))
    return event


def accepted(payload: dict[str, Any]) -> AcceptedEvent:
    event, errors = validate_event(payload)
    assert event is not None, errors
    return AcceptedEvent(event_identifier(payload), event, payload)


async def counted(client: httpx.AsyncClient, **query: Any) -> list[dict[str, Any]]:
    response = await client.get(DUPLICATES, params=query)
    assert response.status_code == 200
    return response.json()["duplicates"]  # type: ignore[no-any-return]


async def table(engine: AsyncEngine) -> list[tuple[Any, ...]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT emitter, day::text, duplicates FROM ingest_duplicate ORDER BY 1, 2")
        )
        return [tuple(row) for row in rows]


async def test_an_event_known_before_counts_every_time_it_is_sent_again(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    event = event_of("mcp://entrepot")
    assert (await client.post(SINGLE, json=event)).json()["duplicate"] is False
    assert await table(engine) == []

    for _ in range(2):
        assert (await client.post(SINGLE, json=event)).json()["duplicate"] is True
    assert (await client.post(BATCH, json=[event])).json()["duplicates"] == 1

    [(emitter, _, duplicates)] = await table(engine)
    assert (emitter, duplicates) == ("mcp://entrepot", 3)


async def test_an_unknown_event_present_n_times_in_a_batch_counts_n_minus_one(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    fresh = event_of("agent://ventes", 1)
    response = await client.post(BATCH, json=[fresh, fresh, fresh])
    assert response.json() == {"accepted": 3, "duplicates": 2, "rejected": []}
    assert [(row[0], row[2]) for row in await table(engine)] == [("agent://ventes", 2)]

    # Connu désormais : chaque présence compte.
    response = await client.post(BATCH, json=[fresh, fresh])
    assert response.json()["duplicates"] == 2
    assert [(row[0], row[2]) for row in await table(engine)] == [("agent://ventes", 4)]


async def test_the_response_and_the_counter_say_the_same_thing(
    client: httpx.AsyncClient,
) -> None:
    known = event_of("mcp://entrepot", 0)
    await client.post(SINGLE, json=known)
    batch = [
        known,
        known,
        event_of("agent://ventes", 1),
        event_of("agent://ventes", 1),
        event_of("agent://ventes", 2),
        mutated(["run", "runId"], "pas-un-uuid"),
    ]
    response = (await client.post(BATCH, json=batch)).json()

    counts = {row["emitter"]: row["count"] for row in await counted(client)}
    assert counts == {"mcp://entrepot": 2, "agent://ventes": 1}
    assert response["duplicates"] == sum(counts.values()) == 3
    assert len(response["rejected"]) == 1


async def test_a_batch_without_duplicate_neither_reads_nor_writes_the_counter(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Table verrouillée : un envoi qui la toucherait, même en lecture, attendrait."""
    event = event_of("mcp://entrepot")
    await client.post(SINGLE, json=event)
    async with engine.connect() as locker:
        await locker.execute(text("LOCK TABLE ingest_duplicate IN ACCESS EXCLUSIVE MODE"))
        fresh = [event_of("mcp://entrepot", index) for index in range(1, 6)]
        response = await asyncio.wait_for(client.post(BATCH, json=fresh), timeout=10)
        assert response.json() == {"accepted": 5, "duplicates": 0, "rejected": []}

        # Témoin : un doublon, lui, doit passer par la table.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(client.post(SINGLE, json=event), timeout=1)
        await locker.rollback()


async def test_the_counter_follows_the_transaction_that_receives_the_event(
    engine: AsyncEngine,
) -> None:
    event = accepted(event_of("mcp://entrepot"))
    async with engine.begin() as conn:
        assert await store_events(conn, [event]) == 1

    async with engine.connect() as conn:
        assert await store_events(conn, [event, event]) == 0
        await conn.rollback()
    assert await table(engine) == []

    async with engine.begin() as conn:
        assert await store_events(conn, [event, event]) == 0
    assert [(row[0], row[2]) for row in await table(engine)] == [("mcp://entrepot", 2)]


async def test_each_day_has_its_own_line_and_the_most_recent_comes_first(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    event = event_of("mcp://entrepot")
    await client.post(SINGLE, json=event)
    await client.post(SINGLE, json=event)
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE ingest_duplicate SET day = day - 2"))
    await client.post(BATCH, json=[event, event, event])
    other = event_of("agent://ventes", 1)
    await client.post(SINGLE, json=other)
    await client.post(SINGLE, json=other)

    rows = await counted(client)
    assert [(row["emitter"], row["count"]) for row in rows] == [
        ("agent://ventes", 1),
        ("mcp://entrepot", 3),
        ("mcp://entrepot", 1),
    ]
    days = [row["day"] for row in rows]
    assert days == sorted(days, reverse=True)
    assert days[0] == days[1] != days[2]

    assert [row["count"] for row in await counted(client, limit=1)] == [1]
    assert len(await counted(client, limit=2)) == 2


async def test_the_day_is_the_utc_date_of_reception(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    event = event_of("mcp://entrepot")
    await client.post(SINGLE, json=event)
    await client.post(SINGLE, json=event)
    async with engine.connect() as conn:
        same_day = (
            await conn.execute(
                text("SELECT day = (now() AT TIME ZONE 'UTC')::date FROM ingest_duplicate")
            )
        ).scalar_one()
    assert same_day is True


async def test_nothing_counted_yet_is_an_empty_list(client: httpx.AsyncClient) -> None:
    assert await counted(client) == []


@pytest.mark.parametrize("limit", ["0", "-1", "1001", "beaucoup", "1.5", ""])
async def test_an_invalid_limit_is_rejected_without_echoing_it(
    client: httpx.AsyncClient, limit: str
) -> None:
    response = await client.get(DUPLICATES, params={"limit": limit})
    assert response.status_code == 422
    (error,) = response.json()["errors"]
    assert error["path"] == "/query/limit"
    if limit:
        assert limit not in response.text


@pytest.mark.parametrize(
    "namespace",
    [
        "mcp://entrepot",
        "agent://ventes",
        "postgres://hote:5432",
        "abfss://conteneur@compte.dfs.core.windows.net",
    ],
)
def test_the_emitter_is_the_namespace_of_the_job(namespace: str) -> None:
    event, _ = validate_event(event_of(namespace))
    assert event is not None
    assert emitter_of(event) == namespace


async def test_two_spellings_of_a_namespace_count_under_one_emitter(
    client: httpx.AsyncClient,
) -> None:
    """Composé ou décomposé, un espace de noms désigne un seul émetteur, comme dans le graphe."""
    composed, decomposed = "mcp://entrep\u00f4t", "mcp://entrepo\u0302t"
    for namespace in (composed, decomposed):
        assert (await client.post(SINGLE, json=event_of(namespace))).status_code == 201
        assert (await client.post(SINGLE, json=event_of(namespace))).json()["duplicate"] is True

    assert [(row["emitter"], row["count"]) for row in await counted(client)] == [(composed, 2)]


@pytest.mark.parametrize(
    "namespace",
    ["", "mcp://entrepot et son mot de passe", "mcp://entrepot\n", "mcp://" + "x" * 600],
)
async def test_an_emitter_without_the_form_of_a_name_never_reaches_the_counter(
    client: httpx.AsyncClient, engine: AsyncEngine, namespace: str
) -> None:
    """L'ingestion le rejette : rien n'est enregistré, donc rien n'est compté ni recopié."""
    hostile = event_of(namespace)
    for _ in range(2):
        assert (await client.post(SINGLE, json=hostile)).status_code == 422

    assert await counted(client) == []
    assert await table(engine) == []
