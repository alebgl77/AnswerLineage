"""La file tenue dans PostgreSQL : ni double traitement, ni perte."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from answerlineage_api.ingest.models import validate_event
from answerlineage_api.ingest.store import AcceptedEvent, event_identifier, store_events
from answerlineage_workers import cli
from answerlineage_workers.queue import BatchResult, process_batch, record_event, run_worker

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


def tool_call(index: int, table: str = "ref.taux_change") -> dict[str, Any]:
    return {
        "eventType": "COMPLETE",
        "eventTime": f"2026-11-02T09:{index // 60:02d}:{index % 60:02d}Z",
        "producer": "https://example.com/p",
        "schemaURL": "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent",
        "run": {
            "runId": str(UUID(int=0x0193A9C57A107C3E9D5E2F1B6C8A0000 + index)),
            "facets": {
                "answerlineage_toolCall": {
                    "_producer": "https://example.com/p",
                    "_schemaURL": "https://example.com/s",
                    "server": "entrepot",
                    "tool": "execute_sql",
                }
            },
        },
        "job": {"namespace": "mcp://entrepot", "name": "execute_sql"},
        "inputs": [{"namespace": "bigquery", "name": table}],
    }


async def ingest(engine: AsyncEngine, payloads: list[dict[str, Any]]) -> None:
    accepted = []
    for payload in payloads:
        event, errors = validate_event(payload)
        assert event is not None, errors
        accepted.append(AcceptedEvent(event_identifier(payload), event, payload))
    async with engine.begin() as conn:
        await store_events(conn, accepted)


async def scalar(engine: AsyncEngine, sql: str) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql))).scalar_one()


async def test_queued_events_are_projected_into_the_graph_and_leave_the_queue(
    engine: AsyncEngine,
) -> None:
    await ingest(engine, [tool_call(index) for index in range(25)])
    assert await scalar(engine, "SELECT count(*) FROM ingest_queue") == 25

    assert await process_batch(engine, batch_size=10) == BatchResult(processed=10)
    assert await process_batch(engine, batch_size=100) == BatchResult(processed=15)
    assert (await process_batch(engine)).empty

    assert await scalar(engine, "SELECT count(*) FROM ingest_queue") == 0
    assert await scalar(engine, "SELECT count(*) FROM run WHERE kind = 'tool'") == 25
    assert await scalar(engine, "SELECT count(*) FROM read_edge") == 25
    assert await scalar(engine, "SELECT count(*) FROM tool") == 1
    # Les événements bruts restent, rejouables.
    assert await scalar(engine, "SELECT count(*) FROM event_raw") == 25


async def test_two_concurrent_workers_never_process_an_event_twice(engine: AsyncEngine) -> None:
    await ingest(engine, [tool_call(index) for index in range(120)])
    seen: Counter[str] = Counter()
    overlapping = 0
    active = 0

    async def counting(
        conn: AsyncConnection, payload: Mapping[str, Any], received_at: datetime
    ) -> None:
        nonlocal active, overlapping
        active += 1
        overlapping = max(overlapping, active)
        seen[payload["run"]["runId"]] += 1
        await record_event(conn, payload, received_at)
        # Laisse l'autre worker réclamer son lot pendant que celui-ci est ouvert.
        await asyncio.sleep(0.002)
        active -= 1

    async def worker() -> int:
        total = 0
        while True:
            result = await process_batch(engine, batch_size=7, handler=counting)
            if result.empty:
                return total
            total += result.processed

    first, second = await asyncio.gather(worker(), worker())

    assert first + second == 120
    assert first > 0 and second > 0, "les deux workers doivent avoir travaillé"
    assert overlapping == 2, "les deux lots doivent avoir été ouverts en même temps"
    assert len(seen) == 120
    assert set(seen.values()) == {1}
    assert await scalar(engine, "SELECT count(*) FROM run") == 120
    assert await scalar(engine, "SELECT count(*) FROM ingest_queue") == 0


async def test_batch_of_a_worker_stopped_midway_is_taken_up_again_without_loss(
    engine: AsyncEngine,
) -> None:
    await ingest(engine, [tool_call(index) for index in range(10)])
    handled = 0

    async def stopped_after_four(
        conn: AsyncConnection, payload: Mapping[str, Any], received_at: datetime
    ) -> None:
        nonlocal handled
        await record_event(conn, payload, received_at)
        handled += 1
        if handled == 4:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await process_batch(engine, handler=stopped_after_four)

    # Rien n'est perdu, et rien n'est resté à moitié écrit.
    assert await scalar(engine, "SELECT count(*) FROM ingest_queue") == 10
    assert await scalar(engine, "SELECT count(*) FROM run") == 0

    assert await process_batch(engine) == BatchResult(processed=10)
    assert await scalar(engine, "SELECT count(*) FROM run") == 10
    assert await scalar(engine, "SELECT count(*) FROM ingest_queue") == 0


async def test_faulty_event_is_postponed_without_blocking_the_batch(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    await ingest(engine, [tool_call(index) for index in range(5)])
    faulty = tool_call(2)["run"]["runId"]

    async def failing_on_one(
        conn: AsyncConnection, payload: Mapping[str, Any], received_at: datetime
    ) -> None:
        await record_event(conn, payload, received_at)
        if payload["run"]["runId"] == faulty:
            raise ValueError("canari : le message d'erreur peut citer l'événement")

    with caplog.at_level(logging.WARNING, logger="answerlineage_workers"):
        result = await process_batch(engine, handler=failing_on_one)

    assert result == BatchResult(processed=4, postponed=1)
    # L'écriture de l'événement fautif est annulée, les autres sont gardées.
    assert await scalar(engine, "SELECT count(*) FROM run") == 4
    async with engine.connect() as conn:
        ((attempts, last_error, later),) = (
            await conn.execute(
                text("SELECT attempts, last_error, available_at > now() FROM ingest_queue")
            )
        ).all()
    assert (attempts, last_error, later) == (1, "ValueError", True)
    assert "ValueError" in caplog.text
    assert "canari" not in caplog.text

    # Pas encore disponible : il n'est pas repris tout de suite.
    assert (await process_batch(engine)).empty
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE ingest_queue SET available_at = now()"))
    assert await process_batch(engine) == BatchResult(processed=1)
    assert await scalar(engine, "SELECT count(*) FROM run") == 5


async def test_retry_delay_doubles_with_each_attempt(engine: AsyncEngine) -> None:
    await ingest(engine, [tool_call(0)])

    async def always_failing(
        conn: AsyncConnection, payload: Mapping[str, Any], received_at: datetime
    ) -> None:
        raise RuntimeError

    delays = []
    for _ in range(3):
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE ingest_queue SET available_at = now()"))
        await process_batch(engine, handler=always_failing)
        delays.append(
            await scalar(
                engine, "SELECT extract(epoch FROM available_at - now())::float FROM ingest_queue"
            )
        )
    assert 4 < delays[0] <= 5 < delays[1] <= 10 < delays[2] <= 20


async def test_the_graph_records_the_reception_of_each_event_not_its_projection(
    engine: AsyncEngine,
) -> None:
    """Reçus l'un après l'autre, projetés ensemble : chacun garde sa réception."""
    await ingest(engine, [tool_call(0)])
    await ingest(engine, [tool_call(1, table="ref.devises")])
    assert await process_batch(engine) == BatchResult(processed=2)

    received = """
        SELECT e.run_id, e.recorded_at, r.recorded_at, x.recorded_at, d.recorded_at
        FROM event_raw e JOIN run r ON r.run_id = e.run_id
        JOIN read_edge x ON x.run_id = e.run_id JOIN dataset d ON d.id = x.dataset_id
        ORDER BY e.recorded_at
    """
    async with engine.connect() as conn:
        before = (await conn.execute(text(received))).all()
    assert [len(set(row[1:])) for row in before] == [1, 1]
    assert before[0].recorded_at < before[1].recorded_at

    # Projeté de nouveau plus tard, l'événement garde la réception d'origine.
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO ingest_queue (event_id) SELECT event_id FROM event_raw")
        )
    assert await process_batch(engine) == BatchResult(processed=2)
    async with engine.connect() as conn:
        assert (await conn.execute(text(received))).all() == before


async def test_replayed_event_changes_nothing_in_the_graph(engine: AsyncEngine) -> None:
    await ingest(engine, [tool_call(0), tool_call(1)])
    await process_batch(engine)
    await ingest(engine, [tool_call(0), tool_call(1), tool_call(0)])
    assert (await process_batch(engine)).empty
    assert await scalar(engine, "SELECT count(*) FROM run") == 2
    assert await scalar(engine, "SELECT count(*) FROM read_edge") == 2


async def test_worker_drains_the_queue_until_it_is_asked_to_stop(engine: AsyncEngine) -> None:
    stop = asyncio.Event()
    worker = asyncio.ensure_future(run_worker(engine, stop, batch_size=3, idle_delay=0.01))
    await ingest(engine, [tool_call(index) for index in range(8)])
    for _ in range(500):
        if await scalar(engine, "SELECT count(*) FROM ingest_queue") == 0:
            break
        await asyncio.sleep(0.01)
    assert await scalar(engine, "SELECT count(*) FROM run") == 8
    assert not worker.done()
    stop.set()
    await asyncio.wait_for(worker, timeout=5)


async def test_worker_survives_a_failed_batch(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Base injoignable ou interblocage : le lot reste dans la file, le worker continue."""
    await ingest(engine, [tool_call(0)])
    real = process_batch
    calls = 0

    async def failing_once(*args: Any, **kwargs: Any) -> BatchResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("canari : base injoignable")
        return await real(*args, **kwargs)

    monkeypatch.setattr("answerlineage_workers.queue.process_batch", failing_once)
    stop = asyncio.Event()
    with caplog.at_level(logging.ERROR, logger="answerlineage_workers"):
        worker = asyncio.ensure_future(run_worker(engine, stop, idle_delay=0.01, error_delay=0.01))
        for _ in range(500):
            if await scalar(engine, "SELECT count(*) FROM ingest_queue") == 0:
                break
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(worker, timeout=5)
    assert await scalar(engine, "SELECT count(*) FROM run") == 1
    assert "ConnectionError" in caplog.text
    assert "canari" not in caplog.text


async def test_command_line_runs_until_stopped(
    database: URL, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSWERLINEAGE_DATABASE_URL", database.render_as_string(hide_password=False))
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(cli.serve(batch_size=5, stop=stop), timeout=5)


def test_command_line_reports_a_missing_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ANSWERLINEAGE_DATABASE_URL", raising=False)
    assert cli.main(["--batch-size", "5"]) == 2
    assert "ANSWERLINEAGE_DATABASE_URL" in capsys.readouterr().err
