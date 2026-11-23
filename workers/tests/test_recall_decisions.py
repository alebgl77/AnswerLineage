"""Une liste de rappel sans décision remonte au responsable de données ; décidée, elle ne
change plus."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_workers import cli, incidents, queue
from answerlineage_workers.incidents import Processed, analyze_incident, process_analysis
from answerlineage_workers.queue import run_worker
from answerlineage_workers.recall import DEFAULT_DECISION_DELAY, checked_delay, escalate
from workers.tests.test_recall import (
    KNOWN_AT,
    answered,
    declared,
    entries,
    entry_answers,
    pseudonym,
    recall_list,
    record,
    rows,
)

pytestmark = pytest.mark.anyio
integration = pytest.mark.integration

_DECIDED = text(
    "INSERT INTO recall_decision (incident_id, decision, author_issuer, author_subject, template)"
    " VALUES (:id, 'validated', 'https://idp.exemple.test', 'responsable-1', 'standard')"
)


async def listed(engine: AsyncEngine, *recipients: str) -> UUID:
    """Un incident analysé, et sa liste de rappel faite à l'instant."""
    await record(engine, answered(1, 3, 4, *recipients))
    incident_id = await declared(engine)
    assert await process_analysis(engine) == Processed(incident_id, "done")
    return incident_id


async def aged(engine: AsyncEngine, incident_id: UUID, hours: float) -> None:
    """La liste a été faite il y a ``hours`` heures."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE recall_list SET built_at = now() - make_interval(secs => :seconds)"
                " WHERE incident_id = :id"
            ),
            {"seconds": hours * 3600, "id": incident_id},
        )


async def journal(engine: AsyncEngine, incident_id: UUID) -> list[tuple[Any, ...]]:
    return await rows(
        engine,
        "SELECT decision, author_issuer, author_subject, template, reason,"
        " decided_at <= now() FROM recall_decision WHERE incident_id = :id ORDER BY id",
        id=incident_id,
    )


@integration
async def test_a_list_without_decision_past_the_delay_escalates_once_to_the_data_steward(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """Critère : une liste sans décision après le délai fixé remonte au responsable de
    données ; la remontée est journalisée, une fois."""
    incident_id = await listed(engine, pseudonym(1))
    await aged(engine, incident_id, 47)
    assert await escalate(engine) == []

    await aged(engine, incident_id, 49)
    with caplog.at_level(logging.WARNING, logger="answerlineage_workers"):
        assert await escalate(engine) == [incident_id]
    assert await escalate(engine) == []

    assert await journal(engine, incident_id) == [("escalated", None, None, None, None, True)]
    assert f"incident {incident_id} sans décision depuis plus de 48 h" in caplog.text
    assert pseudonym(1) not in caplog.text


@integration
async def test_the_delay_is_set_by_the_worker(engine: AsyncEngine) -> None:
    incident_id = await listed(engine, pseudonym(1))
    await aged(engine, incident_id, 2)

    assert await escalate(engine, delay=timedelta(hours=3)) == []
    assert await escalate(engine, delay=timedelta(hours=1)) == [incident_id]


@integration
async def test_a_decided_list_does_not_escalate(engine: AsyncEngine) -> None:
    incident_id = await listed(engine, pseudonym(1))
    await aged(engine, incident_id, 72)
    async with engine.begin() as conn:
        await conn.execute(_DECIDED, {"id": incident_id})

    assert await escalate(engine) == []
    assert [decision for decision, *_ in await journal(engine, incident_id)] == ["validated"]


@integration
async def test_an_empty_list_has_nothing_to_decide_and_does_not_escalate(
    engine: AsyncEngine,
) -> None:
    incident_id = await listed(engine)
    await aged(engine, incident_id, 72)

    assert await escalate(engine) == []
    assert await journal(engine, incident_id) == []


@integration
async def test_two_workers_escalating_together_escalate_a_list_once(engine: AsyncEngine) -> None:
    incident_id = await listed(engine, pseudonym(1))
    await aged(engine, incident_id, 49)

    first, second = await asyncio.gather(escalate(engine), escalate(engine))

    assert sorted([*first, *second]) == [incident_id]
    assert len(await journal(engine, incident_id)) == 1


@integration
async def test_a_replayed_analysis_keeps_a_decided_list_as_it_was_decided(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Ce qui a été validé est ce qui partira : une analyse rejouée, même tronquée, ne refait
    ni ne retire une liste décidée."""
    incident_id = await listed(engine, pseudonym(1))
    async with engine.begin() as conn:
        await conn.execute(_DECIDED, {"id": incident_id})
    decided = (
        await recall_list(engine, incident_id),
        await entries(engine, incident_id),
        await entry_answers(engine, incident_id),
    )
    await record(engine, answered(2, 5, 6, pseudonym(2)))

    with caplog.at_level(logging.INFO, logger="answerlineage_workers"):
        async with engine.begin() as conn:
            await analyze_incident(conn, incident_id, KNOWN_AT, time.monotonic() + 60)
        monkeypatch.setattr(incidents, "MAX_ANSWERS", 1)
        async with engine.begin() as conn:
            await analyze_incident(conn, incident_id, KNOWN_AT, time.monotonic() + 60)

    assert (
        await recall_list(engine, incident_id),
        await entries(engine, incident_id),
        await entry_answers(engine, incident_id),
    ) == decided
    assert f"incident {incident_id} déjà décidée : gardée telle quelle" in caplog.text


@integration
async def test_the_worker_loop_escalates_overdue_lists_and_survives_a_failed_pass(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    incident_id = await listed(engine, pseudonym(1))
    await aged(engine, incident_id, 2)
    stop = asyncio.Event()
    task = asyncio.ensure_future(
        run_worker(engine, stop, idle_delay=0.01, recall_delay=timedelta(hours=1))
    )
    try:
        for _ in range(500):
            if await journal(engine, incident_id):
                break
            await asyncio.sleep(0.01)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=30)
    assert [decision for decision, *_ in await journal(engine, incident_id)] == ["escalated"]

    async def failing(*_: object, **__: object) -> list[UUID]:
        raise ConnectionError

    monkeypatch.setattr(queue, "escalate", failing)
    stop = asyncio.Event()
    with caplog.at_level(logging.ERROR, logger="answerlineage_workers"):
        task = asyncio.ensure_future(run_worker(engine, stop, idle_delay=0.01))
        await asyncio.sleep(0.2)
        stop.set()
        await asyncio.wait_for(task, timeout=30)
    assert "Remontée des listes de rappel en échec (ConnectionError)." in caplog.text


@pytest.mark.parametrize("hours", [0.5, 721])
async def test_a_delay_out_of_its_bounds_is_refused_before_any_batch(
    hours: float, engine: AsyncEngine
) -> None:
    delay = timedelta(hours=hours)
    with pytest.raises(ValueError, match="720 heures"):
        checked_delay(delay)
    with pytest.raises(ValueError, match="720 heures"):
        await run_worker(engine, asyncio.Event(), recall_delay=delay)


def test_the_command_line_sets_the_delay_in_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[timedelta] = []

    async def serve(batch_size: int, recall_delay: timedelta, **_: object) -> None:
        received.append(recall_delay)

    monkeypatch.setattr(cli, "serve", serve)
    assert cli.main(["--recall-decision-hours", "6"]) == 0
    assert cli.main([]) == 0
    assert received == [timedelta(hours=6), DEFAULT_DECISION_DELAY]


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "deux", "721"])
def test_the_command_line_refuses_a_delay_that_is_not_a_whole_number_of_hours(
    value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.main(["--recall-decision-hours", value])
    assert stopped.value.code == 2
    assert "heures" in capsys.readouterr().err
