"""Un test de qualité en échec propose un incident signalé, sans rien déclencher.

La projection relève les échecs dans la transaction du lot ; un pas de la boucle
des workers en fait des propositions, une par jeu, sous verrou et sous leurs
bornes. Une proposition n'écrit ni demande d'analyse, ni annonce, ni incident :
elle attend un humain.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_workers import gate, proposals, quality, queue
from answerlineage_workers.proposals import Maintenance, maintain_proposals, process_signals
from answerlineage_workers.queue import process_batch, record_event, run_worker
from answerlineage_workers.rebuild import rebuild_graph
from workers.tests.dbt import (
    CUSTOMERS,
    ORDERS,
    T0,
    WAREHOUSE,
    assertion,
    dbt_test,
    failing,
    model,
    passing,
)
from workers.tests.test_queue import ingest

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def rows(engine: AsyncEngine, sql: str, **params: Any) -> list[tuple[Any, ...]]:
    async with engine.connect() as conn:
        return [tuple(row) for row in await conn.execute(text(sql), params)]


async def scalar(engine: AsyncEngine, sql: str, **params: Any) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar_one()


async def work(engine: AsyncEngine) -> None:
    """Un worker passe : il projette la file, puis fait les propositions."""
    while not (await process_batch(engine)).empty:
        pass
    while await process_signals(engine):
        pass


async def received(engine: AsyncEngine, *events: dict[str, Any]) -> None:
    await ingest(engine, list(events))
    await work(engine)


async def in_service(engine: AsyncEngine) -> None:
    """Un worker à webhook est passé : un changement d'état d'incident serait mis en file."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO webhook_stats (id, in_service_until)"
                " VALUES (true, now() + interval '1 hour')"
            )
        )


def hours(count: float) -> datetime:
    return T0 + timedelta(hours=count)


PROPOSALS = """
    SELECT state, namespace, name, whole_table, classes, severity, emitter,
           first_failed_at, last_failed_at, window_start, window_basis, decided_at, incident_id
    FROM incident_proposal ORDER BY name
"""


async def test_a_failing_quality_test_proposes_a_signaled_incident_bound_to_its_column(
    engine: AsyncEngine,
) -> None:
    """Critère : l'échec d'un test de qualité sur une colonne d'une table propose un incident
    signalé, sur cette table et cette colonne, lisible par l'API ; rien d'autre ne part."""
    await in_service(engine)
    await received(engine, failing(1, 3, "email"))

    assert await rows(engine, PROPOSALS) == [
        (
            "signaled",
            WAREHOUSE,
            CUSTOMERS,
            False,
            ["not_null"],
            "error",
            "dbt",
            hours(3),
            hours(3),
            hours(3) - timedelta(days=7),
            "lookback",
            None,
            None,
        )
    ]
    assert await rows(engine, "SELECT column_name FROM incident_proposal_column") == [("email",)]
    # Une proposition attend un humain : ni incident, ni analyse, ni annonce.
    for table in ("incident", "incident_analysis", "webhook_queue", "incident_quarantine"):
        assert await scalar(engine, f"SELECT count(*) FROM {table}") == 0, table  # noqa: S608

    transport = httpx.ASGITransport(app=create_app(engine))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        listed = (await client.get("/api/v1/incident-proposals")).json()
        (proposal,) = listed["proposals"]
        assert proposal["state"] == "signaled"
        assert proposal["dataset"] == {"namespace": WAREHOUSE, "name": CUSTOMERS}
        assert proposal["columns"] == ["email"]
        assert (await client.get("/api/v1/incidents")).json()["incidents"] == []


async def test_the_event_of_a_test_is_projected_like_any_other(engine: AsyncEngine) -> None:
    await received(engine, failing(1, 3))
    assert await rows(engine, "SELECT kind, state FROM run") == [("job", "COMPLETE")]
    assert await scalar(engine, "SELECT count(*) FROM read_edge") == 1


async def test_failures_of_one_run_on_one_column_make_one_signal(engine: AsyncEngine) -> None:
    event = dbt_test(
        1,
        3,
        model(
            [
                assertion("not_null", False, "email", "warn"),
                assertion("unique", False, "email", "error"),
            ]
        ),
    )
    await received(engine, event)
    assert await rows(
        engine, "SELECT column_name, classes, severity, state FROM quality_signal"
    ) == [("email", ["not_null", "unique"], "error", "attached")]
    assert await rows(engine, "SELECT classes, severity FROM incident_proposal") == [
        (["not_null", "unique"], "error")
    ]


async def test_columns_and_runs_of_one_dataset_enrich_one_proposal(engine: AsyncEngine) -> None:
    await received(engine, failing(1, 3, "email", kind="unique", severity="warn"))
    await received(engine, failing(2, 5, "pays"), failing(3, 4, "email"))

    assert await rows(
        engine, "SELECT classes, severity, first_failed_at, last_failed_at FROM incident_proposal"
    ) == [(["not_null", "unique"], "error", hours(3), hours(5))]
    assert await rows(
        engine, "SELECT column_name FROM incident_proposal_column ORDER BY column_name"
    ) == [("email",), ("pays",)]
    assert await scalar(engine, "SELECT count(*) FROM quality_signal WHERE state = 'attached'") == 3


async def test_each_dataset_has_its_own_proposal(engine: AsyncEngine) -> None:
    await received(engine, failing(1, 3, "email"), failing(2, 3, "montant", name=ORDERS))
    assert await rows(engine, "SELECT name FROM incident_proposal ORDER BY name") == [
        (CUSTOMERS,),
        (ORDERS,),
    ]


async def test_a_failure_of_the_whole_table_makes_a_whole_table_proposal(
    engine: AsyncEngine,
) -> None:
    await received(engine, failing(1, 3, "email"), failing(2, 4, None, kind="row_count"))
    assert await rows(engine, "SELECT whole_table, classes FROM incident_proposal") == [
        (True, ["not_null", "row_count"])
    ]
    assert await rows(engine, "SELECT column_name FROM incident_proposal_column") == [("email",)]


async def test_replaying_an_event_or_receiving_its_start_signals_nothing_twice(
    engine: AsyncEngine,
) -> None:
    complete = failing(1, 3, "email")
    start = failing(1, 2, "email", kind="unique") | {"eventType": "START"}
    await received(engine, complete, start)
    async with engine.begin() as conn:
        await record_event(conn, complete, datetime.now(UTC))
    await work(engine)

    assert await rows(
        engine, "SELECT column_name, classes, event_time, state FROM quality_signal"
    ) == [("email", ["not_null", "unique"], hours(2), "attached")]
    assert await scalar(engine, "SELECT count(*) FROM incident_proposal") == 1


async def test_a_rebuild_of_the_graph_proposes_nothing_twice(engine: AsyncEngine) -> None:
    await received(engine, failing(1, 3, "email"), passing(2, 1, "email"))
    before = await rows(engine, PROPOSALS)

    await rebuild_graph(engine)
    await work(engine)

    assert await rows(engine, PROPOSALS) == before
    assert await scalar(engine, "SELECT count(*) FROM quality_signal") == 1


async def test_the_window_starts_at_the_last_known_pass_before_the_first_failure(
    engine: AsyncEngine,
) -> None:
    await received(engine, passing(1, 1, "email"), passing(2, 2, "pays"))
    await received(engine, failing(3, 3, "email"))
    assert await rows(engine, "SELECT window_start, window_basis FROM incident_proposal") == [
        (hours(1), "last_pass")
    ]


async def test_a_pass_received_after_the_failure_does_not_open_the_window(
    engine: AsyncEngine,
) -> None:
    await received(engine, passing(1, 5, "email"))
    await received(engine, failing(2, 3, "email"))
    assert await rows(engine, "SELECT window_start, window_basis FROM incident_proposal") == [
        (hours(3) - timedelta(days=7), "lookback")
    ]


async def test_the_window_reaches_back_at_most_a_month(engine: AsyncEngine) -> None:
    await received(engine, passing(1, -40 * 24, "email"))
    await received(engine, failing(2, 3, "email"))
    assert await rows(engine, "SELECT window_start, window_basis FROM incident_proposal") == [
        (hours(3) - timedelta(days=31), "last_pass")
    ]


async def test_an_old_failure_proposes_nothing(engine: AsyncEngine) -> None:
    await received(engine, failing(1, -40 * 24, "email"))
    assert await rows(engine, "SELECT state FROM quality_signal") == [("stale",)]
    assert await scalar(engine, "SELECT count(*) FROM incident_proposal") == 0
    assert await rows(engine, "SELECT stage, stale FROM quality_stats") == [("projection", 1)]


async def test_a_signal_that_waited_too_long_proposes_nothing(engine: AsyncEngine) -> None:
    await ingest(engine, [failing(1, 3, "email")])
    while not (await process_batch(engine)).empty:
        pass
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE quality_signal SET recorded_at = now() - interval '32 days'")
        )
    assert await process_signals(engine) == 1

    assert await rows(engine, "SELECT state FROM quality_signal") == [("stale",)]
    assert await scalar(engine, "SELECT count(*) FROM incident_proposal") == 0
    assert await rows(engine, "SELECT stage, stale FROM quality_stats") == [("proposals", 1)]


async def test_after_a_decision_failures_of_the_dataset_are_muted_for_a_day(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    await received(engine, failing(1, 3, "email"))
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE incident_proposal SET state = 'dismissed', decided_at = now()")
        )
    await received(engine, failing(2, 4, "email"))
    assert await rows(engine, "SELECT state FROM quality_signal ORDER BY event_time") == [
        ("attached",),
        ("muted",),
    ]
    assert await scalar(engine, "SELECT count(*) FROM incident_proposal") == 1

    # Une décision ne change plus : c'est la sourdine qu'on raccourcit, pour la voir finir.
    monkeypatch.setattr(proposals, "MUTE", timedelta(0))
    await received(engine, failing(3, 5, "email"))
    assert await rows(engine, "SELECT state FROM incident_proposal ORDER BY proposed_at") == [
        ("dismissed",),
        ("signaled",),
    ]


async def test_beyond_its_bound_of_columns_a_proposal_covers_the_whole_table(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(proposals, "MAX_PROPOSAL_COLUMNS", 3)
    await received(engine, failing(1, 3, "a"), failing(2, 3, "b"))
    assert await rows(engine, "SELECT whole_table FROM incident_proposal") == [(False,)]
    await received(engine, failing(3, 4, "c"), failing(4, 4, "d"))
    assert await rows(engine, "SELECT whole_table FROM incident_proposal") == [(True,)]
    assert await scalar(engine, "SELECT count(*) FROM incident_proposal_column") == 3


async def test_beyond_the_bound_of_open_proposals_a_signal_is_refused_and_said_once(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(proposals, "MAX_OPEN_PROPOSALS", 2)
    with caplog.at_level(logging.INFO, logger="answerlineage_workers"):
        await received(
            engine, *(failing(rank, 3, name=f"analytique.public.t{rank}") for rank in range(4))
        )
        await received(engine, failing(9, 4, name="analytique.public.t9"))
        # La proposition ouverte d'un jeu s'enrichit encore à la borne.
        await received(engine, failing(10, 5, name="analytique.public.t0"))

    assert await scalar(engine, "SELECT count(*) FROM incident_proposal") == 2
    assert await rows(
        engine, "SELECT state, count(*) FROM quality_signal GROUP BY state ORDER BY state"
    ) == [("attached", 3), ("refused", 3)]
    assert await rows(engine, "SELECT refused, refused_since IS NOT NULL FROM quality_stats") == [
        (3, True)
    ]
    refusals = [record for record in caplog.records if "Borne des propositions" in record.message]
    assert len(refusals) == 1
    assert "analytique" not in caplog.text

    # Une proposition décidée libère une place : l'épisode se clôt.
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE incident_proposal SET state = 'dismissed', decided_at = now()"
                " WHERE name = 'analytique.public.t0'"
            )
        )
    await received(engine, failing(11, 6, name="analytique.public.t11"))
    assert await rows(engine, "SELECT refused_since FROM quality_stats") == [(None,)]


async def test_beyond_the_bound_of_pending_signals_new_ones_are_lost_and_counted(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(quality, "MAX_PENDING_SIGNALS", 1)
    await ingest(engine, [failing(1, 3, "email"), failing(2, 4, "pays")])
    while not (await process_batch(engine)).empty:
        pass
    assert await scalar(engine, "SELECT count(*) FROM quality_signal") == 1
    assert await rows(engine, "SELECT dropped FROM quality_stats") == [(1,)]


async def test_beyond_the_bound_of_tracked_columns_only_known_ones_are_updated(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(quality, "MAX_TRACKED_COLUMNS", 1)
    await received(engine, passing(1, 1, "email"))
    await received(engine, passing(2, 2, "pays"), failing(3, 3, "email"))
    assert await rows(
        engine, "SELECT column_name, last_passed_at, last_failed_at FROM quality_column"
    ) == [("email", hours(1), hours(3))]


async def test_a_bounded_event_is_counted_and_said_without_its_values(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    assertions = [assertion("not_null", False, "lower(email)")] * 1_001
    with caplog.at_level(logging.WARNING, logger="answerlineage_workers"):
        await received(engine, dbt_test(1, 3, model(assertions)))
    assert await rows(
        engine, "SELECT stage, invalid_columns, assertions_truncated FROM quality_stats"
    ) == [("projection", 1_000, 1)]
    assert "1 assertion(s) non lue(s)" in caplog.text
    assert "lower" not in caplog.text


async def test_a_failure_to_record_the_quality_neither_buries_nor_loses_the_event(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(*_: Any) -> None:
        raise ValueError("canari5e3a")

    monkeypatch.setattr(quality, "record", broken)
    with caplog.at_level(logging.ERROR, logger="answerlineage_workers"):
        await received(engine, failing(1, 3, "email"))

    assert await scalar(engine, "SELECT count(*) FROM read_edge") == 1
    assert await scalar(engine, "SELECT count(*) FROM dead_letter") == 0
    assert await scalar(engine, "SELECT count(*) FROM ingest_queue") == 0
    assert await rows(engine, "SELECT dropped FROM quality_stats") == [(1,)]
    assert "Signaux de qualité non relevés (ValueError)" in caplog.text
    assert "canari5e3a" not in caplog.text


async def test_a_deadlock_while_recording_the_quality_takes_the_batch_again(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    record = quality.record

    async def once_deadlocked(*arguments: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            async with arguments[0].begin_nested() as nested:
                await arguments[0].execute(
                    text("DO $$ BEGIN RAISE EXCEPTION USING ERRCODE = '40P01'; END $$")
                )
                await nested.rollback()
        await record(*arguments)

    monkeypatch.setattr(quality, "record", once_deadlocked)
    await received(engine, failing(1, 3, "email"))
    assert calls == 2
    assert await scalar(engine, "SELECT count(*) FROM quality_signal") == 1
    assert await scalar(engine, "SELECT count(*) FROM dead_letter") == 0


async def test_two_workers_make_one_proposal_for_one_dataset(engine: AsyncEngine) -> None:
    await ingest(engine, [failing(rank, 3, f"c{rank}") for rank in range(40)])
    while not (await process_batch(engine)).empty:
        pass

    async def worker() -> int:
        total = 0
        while processed := await process_signals(engine, limit=1):
            total += processed
        return total

    first, second = await asyncio.gather(worker(), worker())
    assert first + second == 40
    assert await scalar(engine, "SELECT count(*) FROM incident_proposal") == 1
    assert await scalar(engine, "SELECT count(*) FROM incident_proposal_column") == 40


async def test_no_proposal_is_made_during_a_rebuild_of_the_graph(engine: AsyncEngine) -> None:
    await ingest(engine, [failing(1, 3, "email")])
    while not (await process_batch(engine)).empty:
        pass
    async with engine.connect() as holder:
        await holder.execute(text("SELECT pg_advisory_lock(:key)"), {"key": gate.GATE_KEY})
        assert await process_signals(engine) == 0
        await holder.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": gate.GATE_KEY})
    assert await process_signals(engine) == 1


async def test_the_worker_loop_makes_the_proposals(engine: AsyncEngine) -> None:
    await ingest(engine, [failing(1, 3, "email")])
    stop = asyncio.Event()
    worker = asyncio.create_task(run_worker(engine, stop, idle_delay=0.05))
    try:
        for _ in range(200):
            if await scalar(engine, "SELECT count(*) FROM incident_proposal"):
                break
            await asyncio.sleep(0.05)
    finally:
        stop.set()
        await worker
    assert await scalar(engine, "SELECT count(*) FROM incident_proposal") == 1


# L'entretien : expiration, purge.


async def age(engine: AsyncEngine, sql: str, **params: Any) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(sql), params)


async def test_a_proposal_without_a_new_failure_for_a_week_expires_and_announces_nothing(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    await in_service(engine)
    await received(engine, failing(1, 3, "email"), failing(2, 3, "montant", name=ORDERS))
    aged = "UPDATE incident_proposal SET signaled_at = now() - :ago WHERE name = :name"
    await age(engine, aged, ago=timedelta(days=8), name=CUSTOMERS)
    await age(engine, aged, ago=timedelta(days=6), name=ORDERS)
    with caplog.at_level(logging.INFO, logger="answerlineage_workers"):
        assert await maintain_proposals(engine) == Maintenance(expired=1, purged=0)

    assert await rows(
        engine, "SELECT name, state, decided_at IS NOT NULL FROM incident_proposal ORDER BY name"
    ) == [(CUSTOMERS, "expired", True), (ORDERS, "signaled", False)]
    assert await scalar(engine, "SELECT count(*) FROM webhook_queue") == 0
    assert "expirée, sans nouvel échec depuis 7 jours" in caplog.text
    assert "analytique" not in caplog.text

    # Une proposition expirée ne met pas en sourdine : un nouvel échec en ouvre une autre.
    await received(engine, failing(3, 6, "email"))
    assert await rows(
        engine,
        "SELECT state FROM incident_proposal WHERE name = :name ORDER BY proposed_at",
        name=CUSTOMERS,
    ) == [("expired",), ("signaled",)]


async def test_a_new_failure_keeps_a_proposal_from_expiring(engine: AsyncEngine) -> None:
    await received(engine, failing(1, 3, "email"))
    await age(engine, "UPDATE incident_proposal SET signaled_at = now() - interval '8 days'")
    await received(engine, failing(2, 4, "email"))
    assert await maintain_proposals(engine) == Maintenance(expired=0, purged=0)
    assert await rows(engine, "SELECT state FROM incident_proposal") == [("signaled",)]


async def test_signals_received_more_than_two_months_ago_are_purged(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    await received(engine, *(failing(rank, 3, f"c{rank}") for rank in range(4)))
    await age(
        engine,
        "UPDATE quality_signal SET recorded_at = now() - interval '63 days'"
        " WHERE column_name IN ('c0', 'c1', 'c2')",
    )
    monkeypatch.setattr(proposals, "PURGE_BATCH", 2)
    assert await maintain_proposals(engine) == Maintenance(expired=0, purged=2)
    assert await maintain_proposals(engine) == Maintenance(expired=0, purged=1)
    assert await rows(engine, "SELECT column_name FROM quality_signal") == [("c3",)]
    # La proposition garde ce qu'elle a réuni.
    assert await scalar(engine, "SELECT count(*) FROM incident_proposal_column") == 4


async def test_the_worker_loop_keeps_the_proposals(engine: AsyncEngine) -> None:
    await received(engine, failing(1, 3, "email"))
    await age(engine, "UPDATE incident_proposal SET signaled_at = now() - interval '8 days'")
    stop = asyncio.Event()
    worker = asyncio.create_task(run_worker(engine, stop, idle_delay=0.05))
    try:
        for _ in range(200):
            if await scalar(
                engine, "SELECT count(*) FROM incident_proposal WHERE state = 'expired'"
            ):
                break
            await asyncio.sleep(0.05)
    finally:
        stop.set()
        await worker
    assert await rows(engine, "SELECT state FROM incident_proposal") == [("expired",)]


async def test_a_failing_upkeep_of_the_proposals_does_not_stop_the_worker(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(_: AsyncEngine) -> Maintenance:
        raise RuntimeError("canari5e3a")

    monkeypatch.setattr(queue, "maintain_proposals", broken)
    await ingest(engine, [failing(1, 3, "email")])
    stop = asyncio.Event()
    worker = asyncio.create_task(run_worker(engine, stop, idle_delay=0.05))
    try:
        for _ in range(200):
            if await scalar(engine, "SELECT count(*) FROM incident_proposal"):
                break
            await asyncio.sleep(0.05)
    finally:
        stop.set()
        await worker
    assert await scalar(engine, "SELECT count(*) FROM incident_proposal") == 1
    assert "Entretien des propositions d'incident en échec (RuntimeError)" in caplog.text
    assert "canari5e3a" not in caplog.text
