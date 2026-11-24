"""Partitions mensuelles : créées à l'avance, purgées selon la conservation, contrôlées.

L'horloge de la maintenance est injectée : les mois passent sans sommeil. Les
tests prennent des dates antérieures à celle de la base, dont le mois courant
et les deux suivants ont leurs partitions depuis la migration.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_workers import cli, partitions, queue
from answerlineage_workers.partitions import (
    Retention,
    check_partitions,
    maintain_partitions,
)
from answerlineage_workers.queue import run_worker

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

TABLES = ("coverage_report", "event_raw", "ingest_duplicate")
THIRTEEN = Retention()

_PARTITIONS = text(
    """
    SELECT p.relname AS parent, c.relname AS partition
    FROM pg_inherits i
    JOIN pg_class c ON c.oid = i.inhrelid JOIN pg_class p ON p.oid = i.inhparent
    WHERE p.relname IN ('event_raw', 'coverage_report', 'ingest_duplicate')
    """
)
# L'état lisible du contrôle, tel que la documentation le donne.
_ALERTS = text("SELECT table_name, month, signalled_at FROM partition_alert ORDER BY 2, 1")


def at(year: int, month: int, day: int = 15, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


async def months(engine: AsyncEngine) -> dict[str, set[str]]:
    """Les partitions de chaque table, par mois : ``2026_04``."""
    found: dict[str, set[str]] = {table: set() for table in TABLES}
    async with engine.connect() as conn:
        for row in await conn.execute(_PARTITIONS):
            found[row.parent].add(row.partition.removeprefix(f"{row.parent}_"))
    return found


async def execute(engine: AsyncEngine, sql: str, **params: Any) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(sql), params)


async def received_in(engine: AsyncEngine, moment: datetime, rank: int = 0) -> None:
    """Un événement, un relevé et un doublon reçus à ``moment``."""
    month = date(moment.year, moment.month, 1)
    await execute(
        engine,
        """
        INSERT INTO event_raw (event_id, run_id, event_type, event_time, producer, payload,
                               emitter, tool_call, recorded_at, recorded_month)
        VALUES (:event_id, gen_random_uuid(), 'COMPLETE', :at, 'https://exemple.fr', '{}',
                'mcp://entrepot', true, :at, :month)
        """,
        event_id=f"{month:%Y-%m}-{rank}",
        at=moment,
        month=month,
    )
    await execute(
        engine,
        """
        INSERT INTO coverage_report (emitter, window_start, window_end, calls_seen,
                                     events_emitted, events_lost, uninspected,
                                     recorded_at, recorded_month)
        VALUES ('mcp://entrepot', :at, :at + interval '1 minute', 1, 1, 0, 0, :at, :month)
        """,
        at=moment + timedelta(seconds=rank),
        month=month,
    )
    await execute(
        engine,
        """
        INSERT INTO ingest_duplicate (emitter, day, duplicates) VALUES (:emitter, :day, 1)
        """,
        emitter=f"mcp://serveur-{rank}",
        day=moment.date(),
    )


async def test_the_next_month_exists_and_the_month_beyond_retention_is_dropped(
    engine: AsyncEngine,
) -> None:
    """Conservation de 13 mois : le 15 mars 2026, avril est créé et janvier 2025 supprimé."""
    created = await maintain_partitions(engine, now=at(2025, 1, 10), retention=THIRTEEN)
    assert sorted(created.created) == sorted(
        (table, month) for table in TABLES for month in ("2025_01", "2025_02", "2025_03")
    )
    for month in (1, 2):
        await received_in(engine, at(2025, month, 20))

    report = await maintain_partitions(engine, now=at(2026, 3, 15), retention=THIRTEEN)

    found = await months(engine)
    for table in TABLES:
        assert {"2026_03", "2026_04", "2026_05"} <= found[table], table
        # Janvier 2025 finit plus de 13 mois avant le 15 mars 2026 ; février 2025, non.
        assert "2025_01" not in found[table]
        assert "2025_02" in found[table]
    assert sorted(report.dropped) == [(table, f"{table}_2025_01") for table in TABLES]
    async with engine.connect() as conn:
        kept = (await conn.execute(text("SELECT event_id FROM event_raw"))).scalars().all()
    assert kept == ["2025-02-0"]


async def test_a_write_in_a_month_without_partition_succeeds_once_it_is_created_ahead(
    engine: AsyncEngine,
) -> None:
    with pytest.raises(DBAPIError, match="no partition"):
        await received_in(engine, at(2026, 5, 31))

    await maintain_partitions(engine, now=at(2026, 3, 20), retention=THIRTEEN)
    # Le mois courant et les deux suivants : chaque écriture trouve sa partition.
    for month in (3, 4, 5):
        await received_in(engine, at(2026, month, 28, 23), rank=month)


async def test_the_lead_can_be_set(engine: AsyncEngine) -> None:
    await maintain_partitions(engine, now=at(2026, 1, 5), retention=Retention(months=13, ahead=4))
    assert {"2026_01", "2026_02", "2026_03", "2026_04", "2026_05"} <= (await months(engine))[
        "event_raw"
    ]


async def test_a_future_partition_missing_seven_days_ahead_raises_one_alert(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    await maintain_partitions(engine, now=at(2026, 3, 1), retention=THIRTEEN)
    await execute(engine, "DROP TABLE event_raw_2026_04")

    with caplog.at_level(logging.WARNING, logger="answerlineage_workers"):
        # Huit jours avant avril, rien ne presse encore.
        assert await check_partitions(engine, now=at(2026, 3, 24, 0)) == []
        assert caplog.text == ""
        # Sept jours avant : l'alerte nomme la table et le mois.
        first = await check_partitions(engine, now=at(2026, 3, 25, 0))
        again = await check_partitions(engine, now=at(2026, 3, 26, 0))
    assert [(item.table, item.month) for item in first] == [("event_raw", date(2026, 4, 1))]
    assert again == []
    assert caplog.text.count("Partition manquante") == 1
    assert "event_raw" in caplog.text and "2026-04" in caplog.text

    async with engine.connect() as conn:
        alerts = [tuple(row) for row in await conn.execute(_ALERTS)]
    assert alerts == [("event_raw", date(2026, 4, 1), at(2026, 3, 25, 0))]

    # La maintenance recrée la partition : l'épisode est clos, l'état se vide.
    await maintain_partitions(engine, now=at(2026, 3, 27), retention=THIRTEEN)
    async with engine.connect() as conn:
        assert (await conn.execute(_ALERTS)).all() == []


async def test_two_workers_maintain_at_once_without_doing_the_work_twice_nor_waiting(
    engine: AsyncEngine,
) -> None:
    reports = await asyncio.wait_for(
        asyncio.gather(
            *(maintain_partitions(engine, now=at(2025, 6, 2), retention=THIRTEEN) for _ in range(4))
        ),
        timeout=20,
    )
    created = [item for report in reports for item in report.created]
    assert sorted(created) == sorted(
        (table, month) for table in TABLES for month in ("2025_06", "2025_07", "2025_08")
    )
    assert sum(not report.skipped for report in reports) >= 1


async def test_a_maintenance_skipped_while_another_runs_does_not_block(
    engine: AsyncEngine,
) -> None:
    async with engine.begin() as holder:
        # Un autre worker tient la maintenance : celle-ci passe son tour sur-le-champ.
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": partitions.LOCK_KEY}
        )
        skipped = await asyncio.wait_for(
            maintain_partitions(engine, now=at(2025, 6, 2), retention=THIRTEEN), timeout=5
        )
    assert skipped.skipped
    assert "2025_06" not in (await months(engine))["event_raw"]


async def test_a_table_held_by_a_long_reading_is_skipped_without_waiting(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """La création ne fait pas attendre l'ingestion derrière elle : elle renonce, et le dit."""
    async with engine.begin() as holder:
        await holder.execute(text("LOCK TABLE event_raw IN ACCESS SHARE MODE"))
        with caplog.at_level(logging.WARNING, logger="answerlineage_workers"):
            report = await asyncio.wait_for(
                maintain_partitions(engine, now=at(2025, 6, 2), retention=THIRTEEN), timeout=10
            )
    found = await months(engine)
    assert "2025_06" not in found["event_raw"]
    assert "2025_06" in found["ingest_duplicate"]
    assert ("event_raw", date(2025, 6, 1)) in [(item.table, item.month) for item in report.missing]
    assert "LockNotAvailable" in caplog.text or "OperationalError" in caplog.text


async def test_the_purge_never_drops_the_current_month(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        today: datetime = (await conn.execute(text("SELECT now()"))).scalar_one()
    current = f"{today:%Y_%m}"

    # Une horloge de worker très en avance ne fait pas tomber le mois de la base.
    far = await maintain_partitions(engine, now=today + timedelta(days=4 * 365), retention=THIRTEEN)
    assert far.dropped == []
    assert all(current in found for found in (await months(engine)).values())

    # La plus petite conservation admise garde le mois courant et le précédent.
    await maintain_partitions(engine, now=at(2026, 1, 3), retention=THIRTEEN)
    shortest = Retention(months=2, ahead=2)
    report = await maintain_partitions(engine, now=at(2026, 3, 31, 23), retention=shortest)
    assert {partition for _, partition in report.dropped} == set()
    assert {"2026_01", "2026_02", "2026_03"} <= (await months(engine))["event_raw"]
    report = await maintain_partitions(engine, now=at(2026, 4, 1, 0), retention=shortest)
    assert {table for table, _ in report.dropped} == set(TABLES)
    assert "2026_01" not in (await months(engine))["event_raw"]


async def test_a_partition_held_by_a_long_reading_is_purged_at_a_later_pass(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    await maintain_partitions(engine, now=at(2025, 1, 10), retention=THIRTEEN)
    async with engine.begin() as holder:
        await holder.execute(text("LOCK TABLE event_raw_2025_01 IN ACCESS SHARE MODE"))
        with caplog.at_level(logging.WARNING, logger="answerlineage_workers"):
            report = await asyncio.wait_for(
                maintain_partitions(engine, now=at(2026, 3, 15), retention=THIRTEEN), timeout=10
            )
    assert sorted(report.dropped) == [
        ("coverage_report", "coverage_report_2025_01"),
        ("ingest_duplicate", "ingest_duplicate_2025_01"),
    ]
    assert "Partition event_raw_2025_01 non supprimée" in caplog.text

    later = await maintain_partitions(engine, now=at(2026, 3, 15, 13), retention=THIRTEEN)
    assert later.dropped == [("event_raw", "event_raw_2025_01")]


async def test_a_default_partition_covers_no_month_and_is_never_dropped(
    engine: AsyncEngine,
) -> None:
    await execute(engine, "CREATE TABLE event_raw_par_defaut PARTITION OF event_raw DEFAULT")
    report = await maintain_partitions(engine, now=at(2025, 6, 2), retention=THIRTEEN)
    assert ("event_raw", "2025_06") in report.created
    await maintain_partitions(engine, now=at(2030, 6, 2), retention=THIRTEEN)
    assert "par_defaut" in (await months(engine))["event_raw"]


async def test_a_check_skipped_while_the_maintenance_runs_signals_nothing(
    engine: AsyncEngine,
) -> None:
    async with engine.begin() as holder:
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": partitions.LOCK_KEY}
        )
        assert await check_partitions(engine, now=at(2025, 6, 2)) == []
    signalled = await check_partitions(engine, now=at(2025, 6, 2))
    assert {item.table for item in signalled} == set(TABLES)


async def test_unprojected_events_of_a_purged_month_leave_the_queue(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    await maintain_partitions(engine, now=at(2025, 1, 10), retention=THIRTEEN)
    await received_in(engine, at(2025, 1, 20))
    await execute(
        engine,
        "INSERT INTO ingest_queue (event_id, recorded_month) VALUES ('2025-01-0', '2025-01-01')",
    )
    with caplog.at_level(logging.WARNING, logger="answerlineage_workers"):
        await maintain_partitions(engine, now=at(2026, 3, 15), retention=THIRTEEN)
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM ingest_queue"))).scalar_one() == 0
    assert "1 événement(s) jamais projeté(s)" in caplog.text


async def test_dead_letters_of_a_purged_month_leave_the_dead_letters(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    await maintain_partitions(engine, now=at(2025, 1, 10), retention=THIRTEEN)
    await received_in(engine, at(2025, 1, 20))
    await received_in(engine, at(2025, 2, 20))
    await execute(
        engine,
        """
        INSERT INTO dead_letter (event_id, recorded_month, emitter, reason, attempts,
                                 first_failed_at, last_failed_at)
        SELECT event_id, recorded_month, emitter, 'invalid_event', 1, recorded_at, recorded_at
        FROM event_raw
        """,
    )
    with caplog.at_level(logging.WARNING, logger="answerlineage_workers"):
        await maintain_partitions(engine, now=at(2026, 3, 15), retention=THIRTEEN)
    async with engine.connect() as conn:
        kept = await conn.execute(text("SELECT event_id FROM dead_letter"))
        # Février 2025 finit après la limite de la purge : il reste, au rebut comme ailleurs.
        assert kept.scalars().all() == ["2025-02-0"]
    assert "event_raw_2025_01 purgée : 1 événement(s) retiré(s) du rebut" in caplog.text


async def test_events_of_a_purged_month_waiting_to_be_reemitted_are_counted_lost(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    await maintain_partitions(engine, now=at(2025, 1, 10), retention=THIRTEEN)
    await received_in(engine, at(2025, 1, 20))
    await execute(
        engine,
        "INSERT INTO reemission_queue (event_id, recorded_month)"
        " VALUES ('2025-01-0', '2025-01-01')",
    )
    with caplog.at_level(logging.WARNING, logger="answerlineage_workers"):
        await maintain_partitions(engine, now=at(2026, 3, 15), retention=THIRTEEN)
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM reemission_queue"))).scalar_one() == 0
        lost = await conn.execute(text("SELECT sent, rejected, dropped FROM reemission_stats"))
        assert tuple(lost.one()) == (0, 0, 1)
    assert (
        "event_raw_2025_01 purgée : 1 événement(s) jamais réémis retiré(s) de la file de"
        " réémission, comptés perdus." in caplog.text
    )


@pytest.mark.parametrize(
    ("months", "ahead"), [(0, 2), (-1, 2), (3, 4), (13, 1), (13, 25), (1201, 2)]
)
def test_an_absurd_retention_is_refused(months: int, ahead: int) -> None:
    with pytest.raises(ValueError, match=r"conservation|avance"):
        Retention(months=months, ahead=ahead)


async def test_the_worker_loop_runs_the_maintenance_on_its_own_clock(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop = asyncio.Event()
    start = at(2025, 6, 2)
    ticks = iter(start + timedelta(minutes=minute) for minute in (0, 30, 61, 62))

    def clock() -> datetime:
        moment = next(ticks, None)
        if moment is None:
            stop.set()
            return start
        return moment

    runs: list[tuple[datetime, Retention]] = []
    real = maintain_partitions

    async def counted(engine: AsyncEngine, **kwargs: Any) -> Any:
        runs.append((kwargs["now"], kwargs["retention"]))
        return await real(engine, **kwargs)

    monkeypatch.setattr(queue, "maintain_partitions", counted)
    retention = Retention(months=24, ahead=3)
    await asyncio.wait_for(
        run_worker(engine, stop, idle_delay=0, clock=clock, retention=retention), timeout=20
    )
    # Au démarrage, puis dès qu'une heure a passé sur l'horloge du worker.
    assert runs == [(start, retention), (start + timedelta(minutes=61), retention)]
    assert "2025_09" in (await months(engine))["event_raw"]


async def test_a_failed_maintenance_does_not_stop_the_worker(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def failing(engine: AsyncEngine, **kwargs: Any) -> Any:
        raise ConnectionError("canari : base injoignable")

    monkeypatch.setattr(queue, "maintain_partitions", failing)
    stop = asyncio.Event()
    moments = iter([at(2025, 6, 2)] * 3)

    def clock() -> datetime:
        moment = next(moments, None)
        if moment is None:
            stop.set()
            return at(2025, 6, 2)
        return moment

    with caplog.at_level(logging.ERROR, logger="answerlineage_workers"):
        await asyncio.wait_for(run_worker(engine, stop, idle_delay=0, clock=clock), timeout=10)
    assert "Maintenance des partitions en échec (ConnectionError)" in caplog.text
    assert "canari" not in caplog.text


def test_the_command_line_sets_the_retention_and_the_lead(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[Retention] = []

    async def serve(batch_size: int, **settings: Any) -> None:
        received.append(settings["retention"])

    monkeypatch.setattr(cli, "serve", serve)
    assert cli.main([]) == 0
    assert cli.main(["--retention-months", "25", "--partitions-ahead", "6"]) == 0
    assert received == [Retention(months=13, ahead=2), Retention(months=25, ahead=6)]


@pytest.mark.parametrize(
    "options",
    [
        ["--retention-months", "0"],
        ["--retention-months", "-3"],
        ["--retention-months", "treize"],
        ["--retention-months", "3", "--partitions-ahead", "4"],
        ["--partitions-ahead", "1"],
    ],
)
def test_the_command_line_refuses_an_absurd_retention_at_start(
    options: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.main(options)
    assert stopped.value.code == 2
    assert "mois" in capsys.readouterr().err
