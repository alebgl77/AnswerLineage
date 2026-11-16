"""L'analyse d'un incident déclaré : propagée par un worker, une fois, sans doublon."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from answerlineage_api.incidents.models import validate_declaration
from answerlineage_api.incidents.store import declare
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
from answerlineage_workers import incidents
from answerlineage_workers.incidents import Processed, analyze_incident, process_analysis
from answerlineage_workers.queue import run_worker

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

T0 = datetime(2026, 11, 3, 6, 0, tzinfo=UTC)
SALES = AgentRef("agent://ventes", "assistant-commercial")
SQL = ToolRef("entrepot", "execute_sql")
RATES = DatasetRef("bigquery", "finance.ref.taux_change")
ORDERS = DatasetRef("bigquery", "ventes.suivi.commandes")


def later() -> float:
    """Une échéance qu'aucune analyse de ces tests n'atteint."""
    return time.monotonic() + 60


def hours(count: float) -> datetime:
    return T0 + timedelta(hours=count)


def fingerprint(rank: int) -> str:
    return f"sha256:{rank:064x}"


def answering(
    run_id: UUID, rank: int, at: float, recipients: tuple[str, ...], **changes: Any
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        kind="agent",
        state="COMPLETE",
        event_time=hours(at),
        agent=SALES,
        answers=(
            Answer(
                fingerprint(rank),
                deliveries=(Delivery("slack", f"message-{rank}", recipients=recipients),),
            ),
        ),
        **changes,
    )


def reading(parent: UUID, at: float, *datasets: DatasetRef, **changes: Any) -> RunRecord:
    return RunRecord(
        run_id=uuid4(),
        kind="tool",
        state="COMPLETE",
        event_time=hours(at),
        tool=SQL,
        parent_run_id=parent,
        reads=tuple(Read(dataset) for dataset in datasets),
        **changes,
    )


async def record(engine: AsyncEngine, *records: RunRecord) -> None:
    async with engine.begin() as conn:
        for item in records:
            await record_run(conn, item)


async def declared(
    engine: AsyncEngine, *datasets: DatasetRef, start: float = 0, end: float = 10
) -> UUID:
    document = {
        "scope": {"datasets": [{"namespace": d.namespace, "name": d.name} for d in datasets]},
        "window": {"from": hours(start).isoformat(), "to": hours(end).isoformat()},
        "severity": "critical",
    }
    declaration, errors = validate_declaration(document)
    assert declaration is not None, errors
    async with engine.begin() as conn:
        return (await declare(conn, declaration)).id


async def rows(engine: AsyncEngine, sql: str, **params: Any) -> list[tuple[Any, ...]]:
    async with engine.connect() as conn:
        return [tuple(row) for row in await conn.execute(text(sql), params)]


async def analysis(engine: AsyncEngine, incident_id: UUID) -> dict[str, Any]:
    async with engine.connect() as conn:
        found = await conn.execute(
            text("SELECT * FROM incident_analysis WHERE incident_id = :id"), {"id": incident_id}
        )
        return dict(found.one()._mapping)


async def result(engine: AsyncEngine, incident_id: UUID) -> tuple[list[Any], list[Any]]:
    answers = await rows(
        engine,
        "SELECT run_id, fingerprint, produced_at FROM incident_answer WHERE incident_id = :id"
        " ORDER BY produced_at",
        id=incident_id,
    )
    recipients = await rows(
        engine,
        "SELECT fingerprint, pseudonym FROM incident_recipient WHERE incident_id = :id"
        " ORDER BY fingerprint, pseudonym",
        id=incident_id,
    )
    return answers, recipients


async def test_a_declared_incident_is_analyzed_into_its_touched_answers_and_recipients(
    engine: AsyncEngine,
) -> None:
    morning, afternoon, both, outside = uuid4(), uuid4(), uuid4(), uuid4()
    await record(
        engine,
        answering(morning, 1, 3.5, ("p-1", "p-2")),
        reading(morning, 3, RATES),
        answering(afternoon, 2, 9.5, ("p-2", "p-3")),
        reading(afternoon, 9, ORDERS),
        # Touchée par les deux jeux du périmètre : comptée une fois.
        answering(both, 3, 5.5, ("p-4",)),
        reading(both, 5, RATES, ORDERS),
        # Une lecture après la fenêtre ne touche rien.
        answering(outside, 4, 12.5, ("p-5",)),
        reading(outside, 12, RATES),
    )
    unknown = DatasetRef("bigquery", "ventes.suivi.inconnue")
    incident_id = await declared(engine, RATES, ORDERS, unknown)

    assert await process_analysis(engine) == Processed(incident_id, "done")
    assert await process_analysis(engine) is None

    state = await analysis(engine, incident_id)
    assert (state["state"], state["error"], state["attempts"]) == ("done", None, 0)
    assert (state["answers"], state["recipients"], state["truncated"]) == (3, 4, False)
    assert state["unknown_datasets"] == [{"namespace": "bigquery", "name": "ventes.suivi.inconnue"}]
    # Sans relevé d'aucun émetteur, la couverture n'est pas mesurée.
    assert state["coverage"]["status"] == "unknown"
    assert state["known_at"] <= state["finished_at"]

    answers, recipients = await result(engine, incident_id)
    assert answers == [
        (morning, fingerprint(1), hours(3.5)),
        (both, fingerprint(3), hours(5.5)),
        (afternoon, fingerprint(2), hours(9.5)),
    ]
    assert recipients == [
        (fingerprint(1), "p-1"),
        (fingerprint(1), "p-2"),
        (fingerprint(2), "p-2"),
        (fingerprint(2), "p-3"),
        (fingerprint(3), "p-4"),
    ]


async def test_the_analysis_reads_the_graph_as_known_at_its_first_attempt(
    engine: AsyncEngine,
) -> None:
    early, late = uuid4(), uuid4()
    received = datetime(2026, 11, 3, 18, 0, tzinfo=UTC)
    await record(
        engine,
        answering(early, 1, 3.5, ("p-1",), recorded_at=received),
        reading(early, 3, RATES, recorded_at=received),
        answering(late, 2, 4.5, ("p-2",)),
        reading(late, 4, RATES),
    )
    incident_id = await declared(engine, RATES)
    # Un premier essai a fixé l'heure du graphe lu, avant que la seconde réponse soit connue.
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE incident_analysis SET known_at = :at WHERE incident_id = :id"),
            {"at": received + timedelta(minutes=1), "id": incident_id},
        )

    assert await process_analysis(engine) == Processed(incident_id, "done")

    answers, recipients = await result(engine, incident_id)
    assert [answer[0] for answer in answers] == [early]
    assert recipients == [(fingerprint(1), "p-1")]
    assert (await analysis(engine, incident_id))["known_at"] == received + timedelta(minutes=1)


async def test_replaying_an_analysis_gives_the_same_result_without_duplicates(
    engine: AsyncEngine,
) -> None:
    run = uuid4()
    await record(engine, answering(run, 1, 3.5, ("p-1", "p-2")), reading(run, 3, RATES))
    incident_id = await declared(engine, RATES)
    known_at = datetime.now(UTC)

    snapshots = []
    for _ in range(2):
        async with engine.begin() as conn:
            await analyze_incident(conn, incident_id, known_at, later())
        state = await analysis(engine, incident_id)
        state.pop("finished_at")
        snapshots.append((state, await result(engine, incident_id)))

    assert snapshots[0] == snapshots[1]
    answers, recipients = snapshots[0][1]
    assert len(answers) == 1
    assert recipients == [(fingerprint(1), "p-1"), (fingerprint(1), "p-2")]


async def test_two_concurrent_workers_never_analyze_an_incident_twice(
    engine: AsyncEngine,
) -> None:
    run = uuid4()
    await record(engine, answering(run, 1, 3.5, ("p-1",)), reading(run, 3, RATES))
    declared_ids = {await declared(engine, RATES, start=0, end=rank + 4) for rank in range(8)}
    seen: Counter[UUID] = Counter()
    overlapping = 0
    active = 0

    async def counting(
        conn: AsyncConnection, incident_id: UUID, known_at: datetime, deadline: float
    ) -> None:
        nonlocal active, overlapping
        active += 1
        overlapping = max(overlapping, active)
        seen[incident_id] += 1
        await analyze_incident(conn, incident_id, known_at, deadline)
        # Laisse l'autre worker réclamer sa demande pendant que celle-ci est ouverte.
        await asyncio.sleep(0.01)
        active -= 1

    async def worker() -> int:
        done = 0
        while (await process_analysis(engine, analyze=counting)) is not None:
            done += 1
        return done

    first, second = await asyncio.gather(worker(), worker())

    assert first + second == 8
    assert first > 0 and second > 0, "les deux workers doivent avoir travaillé"
    assert overlapping == 2, "les deux analyses doivent avoir été ouvertes en même temps"
    assert set(seen) == declared_ids
    assert set(seen.values()) == {1}
    assert await rows(engine, "SELECT DISTINCT state FROM incident_analysis") == [("done",)]
    assert await rows(engine, "SELECT count(*) FROM incident_answer") == [(8,)]


async def test_a_worker_stopped_midway_leaves_the_request_untouched(engine: AsyncEngine) -> None:
    run = uuid4()
    await record(engine, answering(run, 1, 3.5, ("p-1",)), reading(run, 3, RATES))
    incident_id = await declared(engine, RATES)

    async def stopped(
        conn: AsyncConnection, incident_id: UUID, known_at: datetime, deadline: float
    ) -> None:
        await analyze_incident(conn, incident_id, known_at, deadline)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await process_analysis(engine, analyze=stopped)

    state = await analysis(engine, incident_id)
    assert (state["state"], state["attempts"], state["known_at"]) == ("pending", 0, None)
    assert await result(engine, incident_id) == ([], [])
    assert await process_analysis(engine) == Processed(incident_id, "done")


async def test_a_failing_analysis_is_postponed_with_its_code_then_failed(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    incident_id = await declared(engine, RATES)
    canary = "canari-0b7e"

    async def failing(conn: AsyncConnection, *_: Any) -> None:
        raise RuntimeError(canary)

    monkeypatch.setattr(incidents, "MAX_ATTEMPTS", 2)
    with caplog.at_level(logging.INFO, logger="answerlineage_workers"):
        assert await process_analysis(engine, analyze=failing) == Processed(
            incident_id, "postponed"
        )
        postponed = await analysis(engine, incident_id)
        assert (postponed["state"], postponed["attempts"]) == ("pending", 1)
        assert postponed["error"] == "analysis_error"
        assert postponed["known_at"] is not None
        assert postponed["finished_at"] is None
        # Remise à plus tard : aucun worker ne la reprend tout de suite.
        assert await process_analysis(engine, analyze=failing) is None

        async with engine.begin() as conn:
            await conn.execute(text("UPDATE incident_analysis SET available_at = now()"))
        assert await process_analysis(engine, analyze=failing) == Processed(incident_id, "failed")

    failed = await analysis(engine, incident_id)
    assert (failed["state"], failed["attempts"], failed["error"]) == ("failed", 2, "analysis_error")
    assert failed["finished_at"] is not None
    # L'heure du graphe lu reste celle du premier essai.
    assert failed["known_at"] == postponed["known_at"]
    assert canary not in caplog.text
    assert "RuntimeError" in caplog.text


async def test_an_analysis_beyond_its_deadline_is_postponed_as_a_timeout(
    engine: AsyncEngine,
) -> None:
    run = uuid4()
    await record(engine, answering(run, 1, 3.5, ("p-1",)), reading(run, 3, RATES))
    incident_id = await declared(engine, RATES)

    processed = await process_analysis(engine, timeout=timedelta(0))

    assert processed == Processed(incident_id, "postponed")
    state = await analysis(engine, incident_id)
    assert (state["state"], state["error"]) == ("pending", "query_timeout")
    assert await result(engine, incident_id) == ([], [])


async def test_a_result_beyond_its_bounds_says_it_is_truncated(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = [uuid4() for _ in range(3)]
    await record(
        engine,
        *(answering(run, rank, 3.5 + rank, (f"p-{rank}", "p-9")) for rank, run in enumerate(runs)),
        *(reading(run, 3 + rank, RATES) for rank, run in enumerate(runs)),
    )
    incident_id = await declared(engine, RATES)
    monkeypatch.setattr(incidents, "MAX_ANSWERS", 2)

    assert await process_analysis(engine) == Processed(incident_id, "done")

    state = await analysis(engine, incident_id)
    assert (state["answers"], state["truncated"]) == (2, True)
    answers, _ = await result(engine, incident_id)
    # Sous la borne, les réponses les plus récentes restent.
    assert [answer[0] for answer in answers] == runs[1:]

    monkeypatch.setattr(incidents, "MAX_ANSWERS", 10)
    monkeypatch.setattr(incidents, "MAX_LINKS", 3)
    async with engine.begin() as conn:
        await analyze_incident(conn, incident_id, state["known_at"], later())
    state = await analysis(engine, incident_id)
    assert (state["answers"], state["truncated"]) == (3, True)
    _, recipients = await result(engine, incident_id)
    assert len(recipients) == 3


async def test_the_worker_loop_analyzes_a_declared_incident(engine: AsyncEngine) -> None:
    run = uuid4()
    await record(engine, answering(run, 1, 3.5, ("p-1",)), reading(run, 3, RATES))
    incident_id = await declared(engine, RATES)
    stop = asyncio.Event()
    worker = asyncio.ensure_future(run_worker(engine, stop, idle_delay=0.01))
    try:
        for _ in range(500):
            if (await analysis(engine, incident_id))["state"] == "done":
                break
            await asyncio.sleep(0.01)
    finally:
        stop.set()
        await asyncio.wait_for(worker, timeout=10)

    assert (await analysis(engine, incident_id))["answers"] == 1
