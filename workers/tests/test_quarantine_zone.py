"""La zone des quarantaines actives : calculée par un worker, rafraîchie, une fois à la fois.

Un worker calcule la zone d'une quarantaine dès son activation, la garde à côté
d'elle, et la rafraîchit quand le graphe apprend une vue ; il ne la calcule ni
pendant une reconstruction du graphe, ni pour une quarantaine levée, ni en même
temps qu'un autre worker. Un échec garde la zone précédente et ne dit que sa classe.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from answerlineage_api.incidents import quarantine
from answerlineage_api.incidents.models import validate_declaration
from answerlineage_api.incidents.store import declare
from answerlineage_api.lineage.reading import Reader, ReadTimedOut
from answerlineage_api.lineage.zone import Derived, Zone, derive
from answerlineage_api.store.graph import DatasetRef, Read, RunRecord, Write, record_run
from answerlineage_workers import quarantine_zone
from answerlineage_workers.gate import GATE_KEY
from answerlineage_workers.quarantine_zone import process_zone, run_zones
from answerlineage_workers.queue import run_worker
from answerlineage_workers.rebuild import rebuild_graph
from workers.tests.test_rebuild import CANARY, drain, received, stopping_after

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

T0 = datetime(2026, 11, 16, 6, 0, tzinfo=UTC)
ORDERS = DatasetRef("bigquery", "ventes.suivi.commandes")
DAILY = DatasetRef("bigquery", "ventes.vues.commandes_du_jour")
REVENUE = DatasetRef("bigquery", "finance.marts.chiffre_affaires")
NOW = timedelta(0)


async def record(engine: AsyncEngine, *records: RunRecord) -> None:
    async with engine.begin() as conn:
        for item in records:
            await record_run(conn, item)


def job(reads: Sequence[DatasetRef], writes: Sequence[DatasetRef]) -> RunRecord:
    return RunRecord(
        run_id=uuid4(),
        kind="job",
        state="COMPLETE",
        event_time=T0,
        reads=tuple(Read(dataset) for dataset in reads),
        writes=tuple(Write(dataset) for dataset in writes),
    )


async def quarantined(engine: AsyncEngine, *datasets: DatasetRef) -> UUID:
    """Un incident déclaré sur ces jeux, mis en quarantaine."""
    document = {
        "scope": {"datasets": [{"namespace": d.namespace, "name": d.name} for d in datasets]},
        "window": {"from": "2026-11-16T06:00:00Z", "to": "2026-11-16T12:00:00Z"},
        "severity": "high",
    }
    declaration, errors = validate_declaration(document)
    assert declaration is not None, errors
    async with engine.begin() as conn:
        incident_id = (await declare(conn, declaration)).id
        assert await quarantine.activate(conn, incident_id, "warn") is not None
    return incident_id


async def lifted(engine: AsyncEngine, incident_id: UUID) -> None:
    async with engine.begin() as conn:
        assert await quarantine.lift(conn, incident_id) is not None


async def rows(engine: AsyncEngine, sql: str, **params: Any) -> list[tuple[Any, ...]]:
    async with engine.connect() as conn:
        return [tuple(row) for row in await conn.execute(text(sql), params)]


async def zone(engine: AsyncEngine, incident_id: UUID) -> list[tuple[Any, ...]]:
    """La zone gardée de la dernière quarantaine de l'incident : ses jeux, par profondeur."""
    return await rows(
        engine,
        """
        SELECT d.namespace, d.name, d.depth FROM incident_quarantine_derived d
        JOIN incident_quarantine q ON q.id = d.quarantine_id
        WHERE q.incident_id = :id ORDER BY d.depth, d.namespace, d.name
        """,
        id=incident_id,
    )


async def times(engine: AsyncEngine, incident_id: UUID) -> tuple[Any, ...]:
    (found,) = await rows(
        engine,
        """
        SELECT z.computed_at, z.checked_at, z.truncated, z.derived, q.activated_at
        FROM incident_quarantine_zone z JOIN incident_quarantine q ON q.id = z.quarantine_id
        WHERE q.incident_id = :id
        """,
        id=incident_id,
    )
    return found


async def test_a_zone_is_computed_once_the_quarantine_is_active_and_kept_beside_it(
    engine: AsyncEngine,
) -> None:
    await record(engine, job([ORDERS], [DAILY]), job([DAILY], [REVENUE]))
    assert await process_zone(engine) is None
    incident_id = await quarantined(engine, ORDERS)

    processed = await process_zone(engine)

    assert processed is not None
    assert (processed.incident_id, processed.outcome, processed.derived) == (
        incident_id,
        "computed",
        2,
    )
    assert await zone(engine, incident_id) == [
        (DAILY.namespace, DAILY.name, 1),
        (REVENUE.namespace, REVENUE.name, 2),
    ]
    computed_at, checked_at, truncated, derived, activated_at = await times(engine, incident_id)
    assert activated_at <= computed_at == checked_at
    assert (truncated, derived) == (False, 2)
    # Rien n'est à refaire avant le rafraîchissement.
    assert await process_zone(engine) is None


async def test_the_zone_is_refreshed_when_the_graph_learns_a_view(engine: AsyncEngine) -> None:
    await record(engine, job([ORDERS], [DAILY]))
    incident_id = await quarantined(engine, ORDERS)
    assert await process_zone(engine) is not None
    first, *_ = await times(engine, incident_id)

    await record(engine, job([DAILY], [REVENUE]))
    # Avant son heure, la zone n'est pas recalculée ; après, elle apprend la nouvelle vue.
    assert await process_zone(engine) is None
    refreshed = await process_zone(engine, every=NOW)

    assert refreshed is not None and refreshed.outcome == "computed"
    assert await zone(engine, incident_id) == [
        (DAILY.namespace, DAILY.name, 1),
        (REVENUE.namespace, REVENUE.name, 2),
    ]
    computed_at, checked_at, *_ = await times(engine, incident_id)
    assert first < computed_at == checked_at


async def test_a_zone_found_unchanged_keeps_its_time_only_its_check_moves(
    engine: AsyncEngine,
) -> None:
    await record(engine, job([ORDERS], [DAILY]))
    incident_id = await quarantined(engine, ORDERS)
    assert await process_zone(engine) is not None
    computed_at, *_ = await times(engine, incident_id)

    again = await process_zone(engine, every=NOW)

    assert again is not None and again.outcome == "unchanged"
    assert again.derived == 1
    later_computed, later_checked, *_ = await times(engine, incident_id)
    assert later_computed == computed_at < later_checked


async def test_an_empty_zone_is_a_zone_and_a_zone_that_shrinks_loses_its_datasets(
    engine: AsyncEngine,
) -> None:
    incident_id = await quarantined(engine, ORDERS)
    assert await process_zone(engine) is not None
    assert await zone(engine, incident_id) == []
    assert (await times(engine, incident_id))[2:4] == (False, 0)
    # Calculée vide, elle se retrouve vide : rien ne change.
    unchanged = await process_zone(engine, every=NOW)
    assert unchanged is not None and unchanged.outcome == "unchanged"

    async def truncated(
        reader: Reader, perimeter: Sequence[tuple[str, str]], *, known_at: datetime | None
    ) -> Zone:
        return Zone((Derived(DAILY.namespace, DAILY.name, 1),), truncated=True)

    assert await process_zone(engine, every=NOW, derive_zone=truncated) is not None
    assert (await times(engine, incident_id))[2:4] == (True, 1)
    shrunk = await process_zone(engine, every=NOW)
    assert shrunk is not None and shrunk.outcome == "computed"
    assert await zone(engine, incident_id) == []
    assert (await times(engine, incident_id))[2:4] == (False, 0)


async def test_no_zone_is_computed_while_the_graph_is_rebuilt(engine: AsyncEngine) -> None:
    await received(engine)
    incident_id = await quarantined(engine, DatasetRef("bigquery", f"ventes.{CANARY}"))
    async with engine.connect() as holder:
        # La reconstruction tient la barrière du graphe.
        await holder.execute(text("SELECT pg_advisory_lock(:key)"), {"key": GATE_KEY})
        assert await process_zone(engine) is None
        await holder.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": GATE_KEY})
    assert await process_zone(engine) is not None
    expected = await zone(engine, incident_id)
    assert expected == [("bigquery", "ventes.syntheses", 1)]

    # Une reconstruction interrompue a rendu la barrière, pas encore le graphe.
    stop = asyncio.Event()
    await rebuild_graph(engine, stop=stop, batch_size=1, handler=stopping_after(1, stop))
    assert await process_zone(engine, every=NOW) is None
    await drain(engine)
    assert await process_zone(engine, every=NOW) is not None
    assert await zone(engine, incident_id) == expected


async def test_a_lifted_quarantine_is_no_longer_refreshed(engine: AsyncEngine) -> None:
    await record(engine, job([ORDERS], [DAILY]))
    incident_id = await quarantined(engine, ORDERS)
    assert await process_zone(engine) is not None
    kept = await times(engine, incident_id)

    await lifted(engine, incident_id)
    await record(engine, job([DAILY], [REVENUE]))

    assert await process_zone(engine, every=NOW) is None
    assert await times(engine, incident_id) == kept
    assert await zone(engine, incident_id) == [(DAILY.namespace, DAILY.name, 1)]


def held(release: asyncio.Event, started: list[int]) -> Callable[..., Awaitable[Zone]]:
    """Un calcul qui attend ``release`` : il garde son verrou pendant ce temps."""

    async def slow(
        reader: Reader, perimeter: Sequence[tuple[str, str]], *, known_at: datetime | None
    ) -> Zone:
        started.append(len(started))
        await release.wait()
        return await derive(reader, perimeter, known_at=known_at)

    return slow


async def test_two_workers_never_compute_the_same_zone(engine: AsyncEngine) -> None:
    await record(engine, job([ORDERS], [DAILY]))
    first = await quarantined(engine, ORDERS)
    release, started = asyncio.Event(), list[int]()
    busy = asyncio.ensure_future(process_zone(engine, derive_zone=held(release, started)))
    while not started:
        await asyncio.sleep(0.01)

    # Le second worker trouve la seule zone à calculer verrouillée : il passe.
    assert await process_zone(engine, derive_zone=held(release, started)) is None
    second = await quarantined(engine, REVENUE)
    other = await process_zone(engine)
    release.set()
    done = await busy

    assert other is not None and other.incident_id == second
    assert done is not None and done.incident_id == first
    assert started == [0]
    # Le calcul fini, la zone n'est plus à faire : un worker qui suit ne la refait pas.
    assert await process_zone(engine) is None


async def test_under_the_lock_a_quarantine_is_read_again_and_left_when_no_longer_due(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    incident_id = await quarantined(engine, ORDERS)
    await lifted(engine, incident_id)
    # Une liste lue avant la levée, ou avant qu'un autre worker finisse : elle la cite encore.
    monkeypatch.setattr(
        quarantine_zone,
        "_CANDIDATES",
        text(
            "SELECT id FROM incident_quarantine"
            " WHERE CAST(:every AS float) >= 0 AND id <> ALL(CAST(:skip AS bigint[]))"
            " ORDER BY id LIMIT :limit"
        ),
    )

    assert await process_zone(engine) is None
    assert await rows(engine, "SELECT 1 FROM incident_quarantine_zone") == []


async def test_a_failure_keeps_the_previous_zone_and_says_only_its_class(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    await record(engine, job([ORDERS], [DAILY]))
    incident_id = await quarantined(engine, ORDERS)
    assert await process_zone(engine) is not None
    kept = await times(engine, incident_id)
    await record(engine, job([DAILY], [REVENUE]))

    async def failing(
        reader: Reader, perimeter: Sequence[tuple[str, str]], *, known_at: datetime | None
    ) -> Zone:
        await derive(reader, perimeter, known_at=known_at)
        raise RuntimeError(f"échec qui cite {CANARY} et {DAILY.name}")

    with caplog.at_level(logging.INFO, logger="answerlineage_workers"):
        failed = await process_zone(engine, every=NOW, derive_zone=failing)
        late = await process_zone(engine, every=NOW, timeout=timedelta(seconds=-1))

    assert failed is not None and (failed.incident_id, failed.outcome) == (incident_id, "failed")
    assert late is not None and late.outcome == "failed"
    assert await times(engine, incident_id) == kept
    assert await zone(engine, incident_id) == [(DAILY.namespace, DAILY.name, 1)]
    assert "RuntimeError" in caplog.text and ReadTimedOut.__name__ in caplog.text
    assert CANARY not in caplog.text and DAILY.name not in caplog.text
    # Ce que le calcul n'a pas pu faire, le suivant le fait.
    assert await process_zone(engine, every=NOW) is not None
    assert len(await zone(engine, incident_id)) == 2


async def test_the_loop_computes_each_zone_due_and_retries_a_failure_at_its_next_pass(
    engine: AsyncEngine,
) -> None:
    await record(engine, job([ORDERS], [DAILY]))
    first, second = await quarantined(engine, ORDERS), await quarantined(engine, REVENUE)
    calls: list[tuple[str, str]] = []

    async def failing_once(
        reader: Reader, perimeter: Sequence[tuple[str, str]], *, known_at: datetime | None
    ) -> Zone:
        calls.append(perimeter[0])
        if calls.count(perimeter[0]) == 1 and perimeter[0] == (ORDERS.namespace, ORDERS.name):
            raise RuntimeError
        return await derive(reader, perimeter, known_at=known_at)

    stop = asyncio.Event()
    loop = asyncio.ensure_future(
        run_zones(engine, stop, check_every=timedelta(seconds=0.05), derive_zone=failing_once)
    )
    try:
        for _ in range(500):
            if len(await rows(engine, "SELECT 1 FROM incident_quarantine_zone")) == 2:
                break
            await asyncio.sleep(0.01)
    finally:
        stop.set()
        await asyncio.wait_for(loop, timeout=10)

    assert await zone(engine, first) == [(DAILY.namespace, DAILY.name, 1)]
    assert await zone(engine, second) == []
    # L'échec n'a pas retenu le passage : l'autre zone a été calculée avant la reprise.
    assert calls[:3] == [
        (ORDERS.namespace, ORDERS.name),
        (REVENUE.namespace, REVENUE.name),
        (ORDERS.namespace, ORDERS.name),
    ]


async def test_an_unreachable_base_is_said_by_its_class_and_the_loop_goes_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    unreachable = create_async_engine(
        "postgresql+psycopg://zone:zone@127.0.0.1:1/zone",
        connect_args={"connect_timeout": 1},
    )
    stop = asyncio.Event()
    try:
        with caplog.at_level(logging.ERROR, logger="answerlineage_workers"):
            loop = asyncio.ensure_future(run_zones(unreachable, stop, error_delay=0.01))
            for _ in range(500):
                if caplog.text.count("Zones des quarantaines non calculées") >= 2:
                    break
                await asyncio.sleep(0.01)
            stop.set()
            await asyncio.wait_for(loop, timeout=10)
    finally:
        await unreachable.dispose()
    assert caplog.text.count("Zones des quarantaines non calculées (OperationalError)") >= 2


async def test_the_worker_computes_the_zone_of_a_new_quarantine_beside_the_projection(
    engine: AsyncEngine,
) -> None:
    await record(engine, job([ORDERS], [DAILY]))
    stop = asyncio.Event()
    worker = asyncio.ensure_future(
        run_worker(engine, stop, idle_delay=0.01, zone_check_every=timedelta(seconds=0.05))
    )
    try:
        incident_id = await quarantined(engine, ORDERS)
        for _ in range(1000):
            if await zone(engine, incident_id):
                break
            await asyncio.sleep(0.01)
    finally:
        stop.set()
        await asyncio.wait_for(worker, timeout=30)
    assert await zone(engine, incident_id) == [(DAILY.namespace, DAILY.name, 1)]
