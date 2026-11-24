"""Le graphe reconstruit depuis les événements bruts est celui que les workers avaient bâti.

Le chemin est le vrai : l'API reçoit, la file attend, un worker projette ; puis
la reconstruction vide le graphe et rejoue les événements bruts. La comparaison
porte sur toutes les lignes de toutes les tables du graphe, heures d'événement
et d'enregistrement comprises, et sur le reste de la base, qui ne doit pas
changer (``workers.tests.test_rebuild``).

L'entreprise est reçue d'un seul envoi : ses événements ont la même heure de
réception, et la reconstruction les rejoue dans l'ordre de leur identifiant,
pas dans celui où les workers les avaient projetés. L'écriture du graphe étant
commutative, le graphe est le même.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from answerlineage_corpus.enterprise import generate
from answerlineage_corpus.events import render
from answerlineage_corpus.scenarios import load_corpus
from answerlineage_corpus.truth import truth
from answerlineage_workers.incidents import process_analysis
from answerlineage_workers.queue import process_batch, record_event
from answerlineage_workers.rebuild import RebuildReport, rebuild_graph
from tests.test_corpus_to_graph import api, ingest, report_coverage
from workers.tests.test_queue import scalar
from workers.tests.test_rebuild import drain, graph_rows, other_rows, poisoned, rows

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

EngineFactory = Callable[[], AbstractAsyncContextManager[AsyncEngine]]


async def corrupt(engine: AsyncEngine) -> None:
    """Une corruption : des diffusions perdues, des lectures et des runs altérés."""
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM delivery_recipient WHERE delivery_id IN (SELECT id FROM delivery)")
        )
        await conn.execute(text("DELETE FROM read_edge WHERE id % 3 = 0"))
        await conn.execute(text("UPDATE read_edge SET columns = '{}', precision = 'table'"))
        await conn.execute(text("UPDATE run SET state = 'ABORT', recorded_at = now()"))


async def test_the_synthetic_enterprise_is_rebuilt_identical_from_its_raw_events(
    engine: AsyncEngine,
) -> None:
    """Critère : le graphe vidé, les événements bruts rejoués, le graphe est l'original."""
    enterprise = generate(1)
    events = render(enterprise)
    await ingest(engine, events)
    await report_coverage(engine, enterprise)
    incident = truth(enterprise)["incidents"][0]
    async with api(engine) as client:
        # Un doublon compté, un incident déclaré et analysé : rien de cela n'est du graphe.
        resent = await client.post("/api/v1/lineage", json=events[0])
        assert resent.json()["duplicate"] is True
        declared = await client.post(
            "/api/v1/incidents",
            json={
                "scope": {"datasets": [incident["dataset"]]},
                "window": {"from": incident["from"], "to": incident["to"]},
                "severity": "critical",
            },
        )
        assert declared.status_code == 201, declared.text
    assert await process_analysis(engine) is not None
    refused = await poisoned(engine)
    graph = await graph_rows(engine)
    others = await other_rows(engine)
    await corrupt(engine)
    assert await graph_rows(engine) != graph

    report = await rebuild_graph(engine)

    assert await graph_rows(engine) == graph
    assert await other_rows(engine) == others
    assert await rows(engine, "SELECT event_id, reason FROM dead_letter") == [
        (refused, "invalid_identifier")
    ]
    assert report.finished
    assert (report.replayed, report.dead_letters, report.buried) == (len(events), 1, 0)
    print("\n".join(report.lines()))


async def test_every_scenario_of_the_truth_corpus_is_rebuilt_identical(
    fresh_engine: EngineFactory,
) -> None:
    for scenario in load_corpus():
        async with fresh_engine() as engine:
            await ingest(engine, scenario.events())
            graph = await graph_rows(engine)
            others = await other_rows(engine)

            report = await rebuild_graph(engine)

            assert await graph_rows(engine) == graph, scenario.name
            assert await other_rows(engine) == others, scenario.name
            assert report.replayed == len(scenario.events()), scenario.name


async def test_events_received_during_the_rebuild_end_as_if_there_had_been_none(
    engine: AsyncEngine,
) -> None:
    """L'ingestion continue pendant la reconstruction ; ce qu'elle reçoit est projeté comme
    d'habitude, et le graphe obtenu est celui que donnent tous les événements reçus."""
    events = render(generate(7, requests=60, days=5, incidents=2))
    before, during = events[: len(events) // 2], events[len(events) // 2 :]
    await ingest(engine, before)
    received = asyncio.Event()
    statuses: list[int] = []

    async def receiving_meanwhile() -> None:
        while not await scalar(engine, "SELECT count(*) FROM graph_rebuild"):
            await asyncio.sleep(0.01)
        async with api(engine) as client:
            # Les événements nouveaux, et des renvois d'événements déjà reçus.
            for start in range(0, len(during), 20):
                sent = [*during[start : start + 20], before[start % len(before)]]
                statuses.append((await client.post("/api/v1/lineage/batch", json=sent)).status_code)
                # Un worker tourne : il s'écarte tant que la reconstruction projette.
                await process_batch(engine)
        received.set()

    async def waiting_for_the_ingestion(
        conn: AsyncConnection, payload: Mapping[str, Any], received_at: datetime
    ) -> None:
        await received.wait()
        await record_event(conn, payload, received_at)

    rebuilt: RebuildReport
    rebuilt, _ = await asyncio.gather(
        rebuild_graph(engine, batch_size=5, handler=waiting_for_the_ingestion),
        receiving_meanwhile(),
    )
    await drain(engine)

    assert set(statuses) == {202}
    assert rebuilt.finished and rebuilt.replayed == len(before)
    assert rebuilt.finished_at is not None
    (arrived,) = await rows(
        engine,
        "SELECT count(*) FROM event_raw WHERE recorded_at > :start AND recorded_at < :end",
        start=rebuilt.started_at,
        end=rebuilt.finished_at,
    )
    assert arrived == (len(during),)
    graph = await graph_rows(engine)
    assert len(graph["run"]) == len({event["run"]["runId"] for event in events})

    # Les mêmes événements, rejoués sans rien d'autre, donnent le même graphe.
    await rebuild_graph(engine)
    assert await graph_rows(engine) == graph


async def test_workers_finishing_an_interrupted_rebuild_in_another_order_give_the_original(
    engine: AsyncEngine,
) -> None:
    """Deux workers concurrents reprennent la file d'une reconstruction interrompue : ils
    projettent dans un autre ordre que la première fois, et le graphe est l'original."""
    events = render(generate(7, requests=60, days=5, incidents=2))
    await ingest(engine, events)
    graph = await graph_rows(engine)
    stop = asyncio.Event()
    handled = 0

    async def stopping(
        conn: AsyncConnection, payload: Mapping[str, Any], received_at: datetime
    ) -> None:
        nonlocal handled
        await record_event(conn, payload, received_at)
        handled += 1
        if handled == 30:
            stop.set()

    interrupted = await rebuild_graph(engine, stop=stop, batch_size=10, handler=stopping)
    assert not interrupted.finished and interrupted.remaining == len(events) - 30

    async def worker() -> None:
        while not (await process_batch(engine, batch_size=7)).empty:
            await asyncio.sleep(0)

    await asyncio.gather(worker(), worker())

    # Les deux workers ne se sont pas interbloqués : rien n'est remis à plus tard.
    assert await scalar(engine, "SELECT count(*) FROM ingest_queue") == 0
    assert await scalar(engine, "SELECT count(*) FROM dead_letter") == 0
    assert await graph_rows(engine) == graph
    assert (await rebuild_graph(engine)).resumed
    assert await graph_rows(engine) == graph
