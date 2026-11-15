"""Le contrôle des émetteurs muets : signalés une fois par épisode de silence, sans dormir."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.ingest.coverage import store_report, validate_report
from answerlineage_workers import cli, queue, silence
from answerlineage_workers.queue import run_worker
from answerlineage_workers.silence import check_silent_emitters

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

WAREHOUSE = "mcp://entrepot"


async def publish(engine: AsyncEngine, emitter: str, minute: int) -> datetime:
    """Publie un relevé ; rend l'heure de sa réception, posée par la base."""
    start = datetime(2026, 11, 3, 6, tzinfo=UTC) + timedelta(minutes=minute)
    raw = {
        "emitter": emitter,
        "window": {"from": start.isoformat(), "to": (start + timedelta(minutes=1)).isoformat()},
        "calls_seen": 0,
        "events_emitted": 0,
        "events_lost": 0,
    }
    report, errors = validate_report(raw)
    assert report is not None, errors
    async with engine.begin() as conn:
        await store_report(conn, report)
        found = await conn.execute(
            text("SELECT last_report_at FROM coverage_emitter WHERE emitter = :emitter"),
            {"emitter": emitter},
        )
        received: datetime = found.scalar_one()
    return received


async def muted(engine: AsyncEngine) -> dict[str, Any]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT emitter, silent_since FROM coverage_emitter"
                " WHERE silent_since = last_report_at"
            )
        )
        return {row.emitter: row.silent_since for row in rows}


async def test_an_emitter_silent_for_more_than_ten_minutes_is_signalled_once(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    received = await publish(engine, WAREHOUSE, 0)

    with caplog.at_level(logging.WARNING, logger="answerlineage_workers"):
        assert await check_silent_emitters(engine, now=received + timedelta(minutes=9)) == []
        signalled = await check_silent_emitters(engine, now=received + timedelta(minutes=11))
        assert [(item.emitter, item.since) for item in signalled] == [(WAREHOUSE, received)]
        # Le même épisode n'est pas signalé deux fois.
        assert await check_silent_emitters(engine, now=received + timedelta(minutes=30)) == []

    lines = [record.getMessage() for record in caplog.records]
    assert len(lines) == 1
    assert WAREHOUSE in lines[0]
    assert await muted(engine) == {WAREHOUSE: received}


async def test_an_emitter_that_publishes_again_is_no_longer_silent(engine: AsyncEngine) -> None:
    first = await publish(engine, WAREHOUSE, 0)
    await check_silent_emitters(engine, now=first + timedelta(minutes=11))

    again = await publish(engine, WAREHOUSE, 1)
    assert await muted(engine) == {}
    assert await check_silent_emitters(engine, now=again + timedelta(minutes=5)) == []
    # Un nouveau silence est un nouvel épisode : il est signalé à son tour.
    signalled = await check_silent_emitters(engine, now=again + timedelta(minutes=11))
    assert [(item.emitter, item.since) for item in signalled] == [(WAREHOUSE, again)]


async def test_the_threshold_can_be_set(engine: AsyncEngine) -> None:
    received = await publish(engine, WAREHOUSE, 0)
    later = received + timedelta(minutes=3)
    assert await check_silent_emitters(engine, now=later) == []
    signalled = await check_silent_emitters(engine, now=later, threshold=timedelta(minutes=2))
    assert [item.emitter for item in signalled] == [WAREHOUSE]


async def test_concurrent_checks_signal_an_episode_once_and_never_wait_on_each_other(
    engine: AsyncEngine,
) -> None:
    received = [await publish(engine, f"mcp://serveur-{rank}", rank) for rank in range(5)]
    now = max(received) + timedelta(minutes=11)

    results = await asyncio.wait_for(
        asyncio.gather(*(check_silent_emitters(engine, now=now) for _ in range(6))), timeout=10
    )
    signalled = [item.emitter for result in results for item in result]
    assert sorted(signalled) == [f"mcp://serveur-{rank}" for rank in range(5)]


async def test_a_check_skipped_while_another_runs_does_not_block(engine: AsyncEngine) -> None:
    received = await publish(engine, WAREHOUSE, 0)
    async with engine.begin() as holder:
        # Un autre worker tient le contrôle : celui-ci passe son tour sur-le-champ.
        await holder.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": silence.LOCK_KEY})
        skipped = await asyncio.wait_for(
            check_silent_emitters(engine, now=received + timedelta(minutes=11)), timeout=5
        )
    assert skipped == []
    signalled = await check_silent_emitters(engine, now=received + timedelta(minutes=11))
    assert [item.emitter for item in signalled] == [WAREHOUSE]


async def test_the_worker_loop_runs_the_check_on_its_own_clock(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    received = await publish(engine, WAREHOUSE, 0)
    stop = asyncio.Event()
    last = received + timedelta(minutes=20)
    ticks = iter(received + timedelta(minutes=minute) for minute in (0, 5, 11, 11, 12))

    def clock() -> datetime:
        moment = next(ticks, None)
        if moment is None:
            stop.set()
            return last
        return moment

    checks: list[datetime] = []
    real = check_silent_emitters

    async def counted(engine: AsyncEngine, **kwargs: Any) -> Any:
        checks.append(kwargs["now"])
        return await real(engine, **kwargs)

    monkeypatch.setattr(queue, "check_silent_emitters", counted)
    await asyncio.wait_for(
        run_worker(
            engine,
            stop,
            idle_delay=0,
            clock=clock,
            silence_every=timedelta(minutes=10),
        ),
        timeout=10,
    )
    # Au démarrage, puis dès que dix minutes ont passé sur l'horloge du worker.
    assert [moment - received for moment in checks] == [timedelta(0), timedelta(minutes=11)]
    assert await muted(engine) == {WAREHOUSE: received}


async def test_a_failed_check_does_not_stop_the_worker(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def failing(engine: AsyncEngine, **kwargs: Any) -> Any:
        raise ConnectionError("canari : base injoignable")

    monkeypatch.setattr(queue, "check_silent_emitters", failing)
    stop = asyncio.Event()
    moments = iter([datetime(2026, 11, 3, tzinfo=UTC)] * 3)

    def clock() -> datetime:
        moment = next(moments, None)
        if moment is None:
            stop.set()
            return datetime(2026, 11, 3, tzinfo=UTC)
        return moment

    with caplog.at_level(logging.ERROR, logger="answerlineage_workers"):
        await asyncio.wait_for(run_worker(engine, stop, idle_delay=0, clock=clock), timeout=10)
    assert "ConnectionError" in caplog.text
    assert "canari" not in caplog.text


def test_the_command_line_sets_the_threshold_in_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[timedelta] = []

    async def serve(batch_size: int, silence_threshold: timedelta) -> None:
        received.append(silence_threshold)

    monkeypatch.setattr(cli, "serve", serve)
    assert cli.main(["--silence-minutes", "3"]) == 0
    assert received == [timedelta(minutes=3)]


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "dix", "10081"])
def test_the_command_line_refuses_a_threshold_that_is_not_a_whole_number_of_minutes(
    value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.main(["--silence-minutes", value])
    assert stopped.value.code == 2
    assert "minutes" in capsys.readouterr().err
