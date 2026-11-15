"""Propager : d'une table fausse aux réponses et aux destinataires touchés."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_api.lineage import impact
from answerlineage_api.store.graph import (
    AgentRef,
    Answer,
    DatasetRef,
    Delivery,
    Read,
    RunRecord,
    ToolRef,
    record_run,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

T0 = datetime(2026, 11, 3, 6, 0, tzinfo=UTC)
SALES = AgentRef("agent://ventes", "assistant-commercial")
FINANCE = AgentRef("agent://finance", "analyste-tresorerie")
SQL = ToolRef("entrepot", "execute_sql")
RATES = DatasetRef("bigquery", "finance.ref.taux_change")
ORDERS = DatasetRef("bigquery", "ventes.suivi.commandes")
WINDOW = {
    "namespace": "bigquery",
    "name": "finance.ref.taux_change",
    "from": "2026-11-03T06:00:00Z",
    "to": "2026-11-03T16:00:00Z",
}


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(engine))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield client


async def record(engine: AsyncEngine, *records: RunRecord) -> None:
    async with engine.begin() as conn:
        for item in records:
            await record_run(conn, item)


def hours(count: float) -> datetime:
    return T0 + timedelta(hours=count)


def fingerprint(rank: int) -> str:
    return f"sha256:{rank:064x}"


def answering(
    run_id: UUID,
    rank: int,
    at: float,
    *,
    agent: AgentRef = SALES,
    recipients: tuple[str, ...] = ("p-1",),
    parent: UUID | None = None,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        kind="agent",
        state="COMPLETE",
        event_time=hours(at),
        agent=agent,
        parent_run_id=parent,
        answers=(
            Answer(
                fingerprint(rank),
                deliveries=(Delivery("slack", f"message-{rank}", recipients=recipients),),
            ),
        ),
    )


def reading(
    parent: UUID, at: float, *datasets: DatasetRef, run_id: UUID | None = None
) -> RunRecord:
    return RunRecord(
        run_id=run_id or uuid4(),
        kind="tool",
        state="COMPLETE",
        event_time=hours(at),
        tool=SQL,
        parent_run_id=parent,
        reads=tuple(Read(dataset) for dataset in datasets),
    )


async def propagate(client: httpx.AsyncClient, **changes: Any) -> dict[str, Any]:
    response = await client.get("/api/v1/impact", params={**WINDOW, **changes})
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def touched(result: dict[str, Any]) -> list[str]:
    return sorted(answer["fingerprint"] for answer in result["graph"]["answers"])


async def test_answers_and_recipients_reached_by_a_wrong_table_during_a_window(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    morning, afternoon, other = uuid4(), uuid4(), uuid4()
    await record(
        engine,
        answering(morning, 1, 3.5, recipients=("p-1", "p-2")),
        reading(morning, 3, RATES, ORDERS),
        answering(afternoon, 2, 9.5, agent=FINANCE, recipients=("p-2", "p-3")),
        reading(afternoon, 9, RATES),
        answering(other, 3, 4.5, recipients=("p-4",)),
        reading(other, 4, ORDERS),
    )

    result = await propagate(client)

    assert result["dataset"] == {
        "namespace": "bigquery",
        "name": "finance.ref.taux_change",
        "kind": "table",
    }
    assert result["window"] == {"from": "2026-11-03T06:00:00Z", "to": "2026-11-03T16:00:00Z"}
    assert result["counts"] == {"answers": 2, "recipients": 3}
    assert result["coverage"] == {"status": "unknown"}
    graph = result["graph"]
    assert [answer["fingerprint"] for answer in graph["answers"]] == [
        fingerprint(2),
        fingerprint(1),
    ]
    assert [recipient["pseudonym"] for recipient in graph["recipients"]] == ["p-1", "p-2", "p-3"]
    assert [agent["name"] for agent in graph["agents"]] == [
        "analyste-tresorerie",
        "assistant-commercial",
    ]
    # La chaîne de chaque réponse touchée est entière : ses autres lectures y figurent.
    assert [dataset["name"] for dataset in graph["datasets"]] == [
        "finance.ref.taux_change",
        "ventes.suivi.commandes",
    ]
    assert (graph["more"], graph["truncated"], result["truncated"]) == (False, False, False)


async def test_the_window_includes_its_start_and_excludes_its_end(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    runs = [uuid4() for _ in range(4)]
    await record(
        engine,
        answering(runs[0], 1, 0.5),
        reading(runs[0], 0, RATES),
        # Lue juste avant la fin, la réponse est rendue après : elle est touchée.
        answering(runs[1], 2, 11),
        reading(runs[1], 9.99, RATES),
        answering(runs[2], 3, 10.5),
        reading(runs[2], 10, RATES),
        answering(runs[3], 4, -0.5),
        reading(runs[3], -1, RATES),
    )
    assert touched(await propagate(client)) == [fingerprint(1), fingerprint(2)]


async def test_a_read_made_by_a_delegated_run_reaches_every_answer_above_it(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    top, delegated, sibling = uuid4(), uuid4(), uuid4()
    await record(
        engine,
        answering(top, 1, 5, agent=FINANCE),
        answering(delegated, 2, 4, parent=top),
        reading(delegated, 3, RATES),
        # Un run voisin, sous le même parent, n'a rien lu de la table.
        answering(sibling, 3, 4, parent=top),
        reading(sibling, 3, ORDERS),
    )
    assert touched(await propagate(client)) == [fingerprint(1), fingerprint(2)]


async def test_a_run_that_read_the_table_without_answering_has_nothing_to_recall(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    silent = uuid4()
    await record(
        engine,
        RunRecord(run_id=silent, kind="agent", state="FAIL", event_time=hours(3), agent=SALES),
        reading(silent, 2, RATES),
    )
    result = await propagate(client)
    assert result["counts"] == {"answers": 0, "recipients": 0}
    assert result["graph"]["answers"] == []


async def test_an_answer_without_recipient_is_counted_as_an_answer(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    run_id = uuid4()
    await record(engine, answering(run_id, 1, 3, recipients=()), reading(run_id, 2, RATES))
    assert (await propagate(client))["counts"] == {"answers": 1, "recipients": 0}


async def test_counts_cover_the_whole_incident_and_the_list_is_paged(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    for rank in range(1, 6):
        run_id = uuid4()
        await record(
            engine,
            answering(run_id, rank, rank + 0.5, recipients=(f"p-{rank}",)),
            reading(run_id, rank, RATES),
        )

    first = await propagate(client, limit=2)
    assert first["counts"] == {"answers": 5, "recipients": 5}
    assert touched(first) == [fingerprint(4), fingerprint(5)]
    assert first["graph"]["more"] is True

    older = await propagate(client, limit=2, before=first["graph"]["answers"][-1]["produced_at"])
    assert older["counts"] == {"answers": 5, "recipients": 5}
    assert touched(older) == [fingerprint(2), fingerprint(3)]

    last = await propagate(client, limit=2, before=older["graph"]["answers"][-1]["produced_at"])
    assert touched(last) == [fingerprint(1)]
    assert last["graph"]["more"] is False


async def test_an_unknown_dataset_is_not_found(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    run_id = uuid4()
    await record(engine, answering(run_id, 1, 3), reading(run_id, 2, RATES))
    for changes in ({"name": "finance.ref.inconnue"}, {"namespace": "snowflake"}):
        response = await client.get("/api/v1/impact", params={**WINDOW, **changes})
        assert (response.status_code, response.json()) == (404, {"error": "unknown_dataset"})


@pytest.mark.parametrize(
    ("changes", "path"),
    [
        ({"namespace": None}, "/query/namespace"),
        ({"name": ""}, "/query/name"),
        ({"name": "n" * 501}, "/query/name"),
        ({"name": "a\x00b"}, "/query/name"),
        ({"from": None}, "/query/from"),
        ({"to": "demain"}, "/query/to"),
        ({"from": "2026-11-03T06:00:00"}, "/query/from"),
        ({"to": "2026-11-03T06:00:00Z"}, "/query/to"),
        ({"to": "2026-11-03T05:00:00Z"}, "/query/to"),
        ({"limit": "0"}, "/query/limit"),
        ({"limit": "201"}, "/query/limit"),
        ({"before": "hier"}, "/query/before"),
    ],
)
async def test_an_incident_names_a_dataset_and_a_window_that_is_not_empty(
    client: httpx.AsyncClient, changes: dict[str, str | None], path: str
) -> None:
    params = {key: value for key, value in {**WINDOW, **changes}.items() if value is not None}
    response = await client.get("/api/v1/impact", params=params)
    assert response.status_code == 422
    (error,) = response.json()["errors"]
    assert error["path"] == path
    assert set(error) == {"path", "type", "message"}
    assert "demain" not in response.text


async def test_runs_that_name_each_other_as_parent_do_not_hang_the_propagation(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    first, second = uuid4(), uuid4()
    await record(
        engine,
        answering(first, 1, 4, parent=second),
        answering(second, 2, 4, parent=first),
        reading(first, 3, RATES),
    )
    result = await propagate(client)
    assert touched(result) == [fingerprint(1), fingerprint(2)]
    assert result["truncated"] is False


async def test_a_propagation_beyond_the_bound_is_reported_as_truncated(
    client: httpx.AsyncClient, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    for rank in range(1, 4):
        run_id = uuid4()
        await record(engine, answering(run_id, rank, rank + 0.5), reading(run_id, rank, RATES))

    monkeypatch.setattr(impact, "MAX_RUNS", 6)
    assert (await propagate(client))["truncated"] is False
    monkeypatch.setattr(impact, "MAX_RUNS", 4)
    bounded = await propagate(client)
    assert bounded["truncated"] is True
    assert bounded["counts"]["answers"] <= 3
