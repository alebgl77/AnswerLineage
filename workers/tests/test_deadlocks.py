"""Plusieurs workers sur une file chargée : ni interblocage, ni essai perdu, ni événement perdu.

Les événements d'une entreprise partagent des lignes du graphe : les mêmes jeux de
données et leurs versions du jour, les mêmes outils, agents et destinataires, les
mêmes runs parents. Projetés par des lots concurrents, chacun dans sa transaction,
ils les créent et les datent ensemble.

Un interblocage ou un échec de sérialisation qui reste ne dit rien de l'événement :
le lot est repris sans qu'un essai soit compté. Une autre erreur de la base, comme
une connexion perdue, en compte un.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import psycopg.errors
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from answerlineage_corpus.enterprise import generate
from answerlineage_corpus.events import render
from answerlineage_workers.queue import (
    MAX_BATCH_RETRIES,
    BatchResult,
    process_batch,
    record_event,
)
from answerlineage_workers.rebuild import rebuild_graph
from workers.tests.test_queue import ingest, scalar, tool_call
from workers.tests.test_rebuild import graph_rows, rows

pytestmark = pytest.mark.anyio
integration = pytest.mark.integration

WORKERS = 4
# Le message d'une erreur de la base peut citer l'événement : il n'atteint jamais le journal.
CANARY = "canari40p1"


async def worker(engine: AsyncEngine, batch_size: int) -> BatchResult:
    """Un worker réclame des lots tant que la file en rend."""
    processed = postponed = buried = retried = 0
    while not (result := await process_batch(engine, batch_size=batch_size)).empty:
        processed += result.processed
        postponed += result.postponed
        buried += result.dead_lettered
        retried += result.retried
    return BatchResult(
        processed=processed, postponed=postponed, dead_lettered=buried, retried=retried
    )


@integration
async def test_concurrent_workers_on_a_loaded_queue_lose_no_attempt_and_no_event(
    engine: AsyncEngine,
) -> None:
    """Critère : quatre workers vident la file sans qu'un événement valide aille au rebut,
    sans perte ni double, et sans qu'un essai soit compté."""
    events = render(generate(3, requests=250, days=5))
    # Reçus en envois successifs : chaque envoi a sa propre heure de réception.
    for start in range(0, len(events), 20):
        await ingest(engine, events[start : start + 20])

    results = await asyncio.gather(*(worker(engine, batch_size=25) for _ in range(WORKERS)))

    assert all(result.processed for result in results), "chaque worker a projeté"
    # Chaque événement a quitté la file une fois, dans un lot validé : ni perte, ni double.
    assert sum(result.processed for result in results) == len(events)
    assert sum(result.postponed for result in results) == 0
    assert sum(result.dead_lettered for result in results) == 0
    assert await scalar(engine, "SELECT count(*) FROM ingest_queue") == 0
    assert await scalar(engine, "SELECT count(*) FROM dead_letter") == 0
    # Le graphe est celui que la reconstruction rejoue seule, ligne à ligne, heures comprises.
    graph = await graph_rows(engine)
    await rebuild_graph(engine)
    assert await graph_rows(engine) == graph


def database_error(error: psycopg.Error) -> OperationalError:
    """Une erreur de la base, telle que SQLAlchemy la rend."""
    return OperationalError("SELECT 1", {}, error)


@integration
@pytest.mark.parametrize(
    "error",
    [psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure],
    ids=["interblocage", "serialisation"],
)
async def test_a_transient_error_takes_the_batch_back_without_counting_an_attempt(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture, error: type[psycopg.Error]
) -> None:
    await ingest(engine, [tool_call(0), tool_call(1)])
    victim = tool_call(1)["run"]["runId"]
    failures = 0

    async def failing_once(conn: AsyncConnection, payload: Mapping[str, Any], at: datetime) -> None:
        nonlocal failures
        await record_event(conn, payload, at)
        if payload["run"]["runId"] == victim and not failures:
            failures += 1
            raise database_error(error(CANARY))

    with caplog.at_level(logging.INFO, logger="answerlineage_workers"):
        result = await process_batch(engine, handler=failing_once)

    assert result == BatchResult(processed=2, retried=1)
    assert await scalar(engine, "SELECT count(*) FROM ingest_queue") == 0
    assert await scalar(engine, "SELECT count(*) FROM dead_letter") == 0
    assert await scalar(engine, "SELECT count(*) FROM run") == 2
    sqlstate = error.sqlstate
    assert f"Lot de 2 événement(s) repris sans compter d'essai : {sqlstate}" in caplog.text
    assert f"reprise 1 sur {MAX_BATCH_RETRIES}" in caplog.text
    assert CANARY not in caplog.text


@integration
async def test_a_deadlock_with_another_transaction_takes_the_batch_back(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """Un vrai interblocage : le lot et une autre transaction s'attendent l'un l'autre, et
    PostgreSQL annule le lot, qui attendait le premier. Repris, il passe, sans essai compté."""
    await ingest(engine, [tool_call(0)])
    rival_holds = asyncio.Event()
    waiting = text(
        "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND NOT granted)"
    )
    calls = 0

    async def entangled(conn: AsyncConnection, payload: Mapping[str, Any], at: datetime) -> None:
        nonlocal calls
        calls += 1
        await record_event(conn, payload, at)
        if calls == 1:
            await rival_holds.wait()
            await conn.execute(text("SELECT pg_advisory_xact_lock(1)"))
            await conn.execute(text("SELECT pg_advisory_xact_lock(2)"))

    async def rival() -> None:
        async with engine.connect() as conn, conn.begin():
            await conn.execute(text("SELECT pg_advisory_xact_lock(2)"))
            rival_holds.set()
            while not (await conn.execute(waiting)).scalar_one():
                await asyncio.sleep(0.01)
            await conn.execute(text("SELECT pg_advisory_xact_lock(1)"))

    with caplog.at_level(logging.INFO, logger="answerlineage_workers"):
        result, _ = await asyncio.gather(process_batch(engine, handler=entangled), rival())

    assert (result, calls) == (BatchResult(processed=1, retried=1), 2)
    assert "repris sans compter d'essai : 40P01 (OperationalError), reprise 1 sur" in caplog.text
    assert await scalar(engine, "SELECT count(*) FROM ingest_queue") == 0
    assert await scalar(engine, "SELECT count(*) FROM run") == 1


@integration
async def test_a_transient_error_that_persists_counts_an_attempt_after_the_last_retry(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """La reprise est bornée : au-delà, l'erreur compte un essai à l'événement qu'elle touche,
    comme toute erreur de la base, et un événement qui ne passe jamais finit au rebut."""
    await ingest(engine, [tool_call(0), tool_call(1)])
    victim = tool_call(1)["run"]["runId"]

    async def deadlocked(conn: AsyncConnection, payload: Mapping[str, Any], at: datetime) -> None:
        await record_event(conn, payload, at)
        if payload["run"]["runId"] == victim:
            raise database_error(psycopg.errors.DeadlockDetected(CANARY))

    with caplog.at_level(logging.INFO, logger="answerlineage_workers"):
        result = await process_batch(engine, handler=deadlocked, max_attempts=1)

    assert result == BatchResult(processed=1, dead_lettered=1, retried=MAX_BATCH_RETRIES)
    assert await rows(engine, "SELECT reason, attempts FROM dead_letter") == [("database_error", 1)]
    assert await scalar(engine, "SELECT count(*) FROM run") == 1
    assert f"reprise {MAX_BATCH_RETRIES} sur {MAX_BATCH_RETRIES}" in caplog.text
    assert CANARY not in caplog.text


@integration
async def test_an_unreachable_database_still_counts_an_attempt(engine: AsyncEngine) -> None:
    await ingest(engine, [tool_call(0)])

    async def disconnected(conn: AsyncConnection, payload: Mapping[str, Any], at: datetime) -> None:
        raise database_error(psycopg.errors.ConnectionFailure(CANARY))

    assert await process_batch(engine, handler=disconnected) == BatchResult(postponed=1)
    assert await rows(engine, "SELECT attempts, last_error FROM ingest_queue") == [
        (1, "OperationalError")
    ]
