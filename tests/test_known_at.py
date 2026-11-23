"""Le graphe tel que connu à une date : ce qui a été enregistré après en est exclu.

Le chemin est le vrai : l'API reçoit, la file attend, un worker projette. Deux
envois successifs reçoivent deux heures de réception ; une lecture « telle que
connue » entre les deux ne voit que le premier.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_api.lineage.trace import answer_key
from answerlineage_corpus.events import agent_event, answer_delivered, dataset_read, tool_event
from answerlineage_workers.queue import process_batch

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

T0 = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
JOB = {"namespace": "agent://ventes", "name": "assistant-commercial"}
KNOWN, LATE = (
    UUID(int=0x0193A9C57A107C3E9D5E2F1B6C8A1000),
    UUID(int=0x0193A9C57A107C3E9D5E2F1B6C8A2000),
)
RATES = ("bigquery", "finance.ref.taux_change")
ORDERS = ("bigquery", "ventes.suivi.commandes")
INCIDENT = {
    "namespace": RATES[0],
    "name": RATES[1],
    "from": "2026-11-03T08:00:00Z",
    "to": "2026-11-03T12:00:00Z",
}


def request(run_id: UUID, rank: int, *tables: tuple[str, str]) -> list[dict[str, Any]]:
    """Une demande à un agent : ses appels d'outils, puis sa réponse diffusée."""
    answer = answer_delivered(
        JOB, fingerprint=f"sha256:{rank:064x}", channel="slack", message_ref=f"m-{rank}",
        recipients=[f"p-{rank}"],
    )  # fmt: skip
    return [
        *(tool_call(run_id, index, table) for index, table in enumerate(tables, start=1)),
        agent_event("COMPLETE", T0 + timedelta(minutes=rank), run_id, JOB, outputs=[answer]),
    ]


def tool_call(parent: UUID, index: int, table: tuple[str, str]) -> dict[str, Any]:
    return tool_event(
        T0,
        UUID(int=parent.int + index),
        server="entrepot",
        tool="execute_sql",
        parent=(parent, JOB),
        inputs=[dataset_read(*table)],
    )


def api(engine: AsyncEngine) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app(engine))
    return httpx.AsyncClient(transport=transport, base_url="http://localhost")


async def receive(engine: AsyncEngine, events: list[dict[str, Any]]) -> datetime:
    """Reçoit un envoi, le projette, et rend son heure de réception."""
    async with api(engine) as client:
        assert (await client.post("/api/v1/lineage/batch", json=events)).status_code == 202
    while not (await process_batch(engine)).empty:
        pass
    async with engine.connect() as conn:
        received: datetime = (
            await conn.execute(text("SELECT max(recorded_at) FROM event_raw"))
        ).scalar_one()
    return received


@pytest.fixture
async def received(engine: AsyncEngine) -> datetime:
    """Un premier envoi, puis un second : une lecture de plus, et une réponse de plus."""
    first = await receive(engine, request(KNOWN, 1, RATES))
    later = [tool_call(KNOWN, 2, ORDERS), *request(LATE, 2, RATES)]
    assert await receive(engine, later) > first
    return first


async def get(engine: AsyncEngine, path: str, **params: Any) -> dict[str, Any]:
    async with api(engine) as client:
        response = await client.get(path, params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def fingerprints(view: dict[str, Any]) -> list[str]:
    return [answer["fingerprint"] for answer in view["answers"]]


async def test_a_propagation_as_known_at_a_date_leaves_out_what_was_recorded_after(
    engine: AsyncEngine, received: datetime
) -> None:
    today = await get(engine, "/api/v1/impact", **INCIDENT)
    assert fingerprints(today["graph"]) == [f"sha256:{2:064x}", f"sha256:{1:064x}"]
    assert "known_at" not in today

    then = await get(engine, "/api/v1/impact", **INCIDENT, known_at=received.isoformat())
    assert fingerprints(then["graph"]) == [f"sha256:{1:064x}"]
    assert then["counts"] == {"answers": 1, "recipients": 1, "lower_bound": False}
    # La lecture de la commande, enregistrée après, n'est pas dans la chaîne de la réponse.
    assert [dataset["name"] for dataset in then["graph"]["datasets"]] == [RATES[1]]
    assert then["known_at"] == then["graph"]["known_at"]
    assert datetime.fromisoformat(then["known_at"]) == received


async def test_the_sources_of_an_answer_as_known_at_a_date(
    engine: AsyncEngine, received: datetime
) -> None:
    path = f"/api/v1/answers/{answer_key(KNOWN, f'sha256:{1:064x}')}/sources"
    today = await get(engine, path)
    assert [dataset["name"] for dataset in today["datasets"]] == [RATES[1], ORDERS[1]]

    then = await get(engine, path, known_at=received.isoformat())
    assert [dataset["name"] for dataset in then["datasets"]] == [RATES[1]]
    assert [tool["calls"] for tool in then["tools"]] == [1]
    assert datetime.fromisoformat(then["known_at"]) == received

    # Une réponse enregistrée après la date n'était pas connue.
    async with api(engine) as client:
        response = await client.get(
            f"/api/v1/answers/{answer_key(LATE, f'sha256:{2:064x}')}/sources",
            params={"known_at": received.isoformat()},
        )
    assert (response.status_code, response.json()) == (404, {"error": "unknown_answer"})


async def test_the_overview_the_search_and_the_datasets_as_known_at_a_date(
    engine: AsyncEngine, received: datetime
) -> None:
    at = received.isoformat()
    assert fingerprints(await get(engine, "/api/v1/graph")) == [
        f"sha256:{2:064x}",
        f"sha256:{1:064x}",
    ]
    then = await get(engine, "/api/v1/graph", known_at=at)
    assert fingerprints(then) == [f"sha256:{1:064x}"]
    assert [dataset["name"] for dataset in then["datasets"]] == [RATES[1]]

    async with api(engine) as client:
        one = await client.get(
            "/api/v1/graph",
            params={"answer": answer_key(LATE, f"sha256:{2:064x}"), "known_at": at},
        )
    assert (one.status_code, one.json()) == (404, {"error": "unknown_answer"})

    search = {"channel": "slack", "message_ref": "m-2"}
    assert len((await get(engine, "/api/v1/answers", **search))["answers"]) == 1
    assert (await get(engine, "/api/v1/answers", **search, known_at=at))["answers"] == []

    known = await get(engine, "/api/v1/datasets", known_at=at)
    assert [dataset["name"] for dataset in known["datasets"]] == [RATES[1]]
    assert datetime.fromisoformat(known["known_at"]) == received
    assert "known_at" not in await get(engine, "/api/v1/datasets")


async def test_the_exposure_as_known_at_a_date_is_scored_at_that_date(
    engine: AsyncEngine, received: datetime
) -> None:
    at = (T0 + timedelta(hours=1)).isoformat()
    today = await get(engine, "/api/v1/exposure", at=at)
    assert [(item["name"], item["answers"]) for item in today["datasets"]] == [
        (RATES[1], 2),
        (ORDERS[1], 1),
    ]
    assert "known_at" not in today

    then = await get(engine, "/api/v1/exposure", at=at, known_at=received.isoformat())
    assert [(item["name"], item["answers"]) for item in then["datasets"]] == [(RATES[1], 1)]
    assert datetime.fromisoformat(then["known_at"]) == received
    assert datetime.fromisoformat(then["at"]) == T0 + timedelta(hours=1)

    # Sans heure de référence, le score se prend à la date demandée.
    dated = await get(engine, "/api/v1/exposure", known_at=received.isoformat())
    assert datetime.fromisoformat(dated["at"]) == received


async def test_events_sent_again_change_no_recorded_time(
    engine: AsyncEngine, received: datetime
) -> None:
    recorded = " UNION ALL ".join(
        f"SELECT '{table}', recorded_at FROM {table}"  # noqa: S608 — noms constants.
        for table in (
            "run", "read_edge", "answer", "delivery", "delivery_recipient", "dataset",
            "recipient", "agent", "tool",
        )
    )  # fmt: skip
    async with engine.connect() as conn:
        before = sorted((await conn.execute(text(recorded))).all())
        receptions = set(
            (await conn.execute(text("SELECT recorded_at FROM event_raw"))).scalars().all()
        )
    # Les deux envois, et rien d'autre : ni l'heure du worker, ni celle d'un rejeu.
    assert {row[1] for row in before} == receptions
    assert len(receptions) == 2 and received in receptions

    async with api(engine) as client:
        for events in (request(KNOWN, 1, RATES), request(LATE, 2, RATES)):
            response = await client.post("/api/v1/lineage/batch", json=events)
            assert response.json()["duplicates"] == len(events)
    while not (await process_batch(engine)).empty:
        pass
    async with engine.connect() as conn:
        assert sorted((await conn.execute(text(recorded))).all()) == before


ROUTES = (
    "/api/v1/impact",
    "/api/v1/graph",
    "/api/v1/datasets",
    "/api/v1/answers",
    "/api/v1/exposure",
    f"/api/v1/answers/{answer_key(KNOWN, f'sha256:{1:064x}')}/sources",
)


@pytest.mark.parametrize(
    "value",
    ["2026-11-03T09:00:00", "hier<script>", "1969-12-31T23:59:59Z", "10000-01-01T00:00:00Z"],
)
async def test_a_date_without_timezone_or_out_of_range_is_refused_without_being_echoed(
    engine: AsyncEngine, value: str
) -> None:
    params = {**INCIDENT, "channel": "slack", "message_ref": "m-1", "known_at": value}
    async with api(engine) as client:
        for path in ROUTES:
            response = await client.get(path, params=params)
            assert response.status_code == 422, path
            (error,) = response.json()["errors"]
            assert error["path"] == "/query/known_at"
            assert value not in response.text


async def test_a_date_before_any_recording_names_its_reason(engine: AsyncEngine) -> None:
    async with api(engine) as client:
        response = await client.get("/api/v1/graph", params={"known_at": "1969-12-31T23:59:59Z"})
    (error,) = response.json()["errors"]
    assert error["type"] == "known_at_out_of_range"
