"""La liste de rappel d'un incident : un message par destinataire, les plus exposés d'abord."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.incidents.models import validate_declaration
from answerlineage_api.incidents.store import declare
from answerlineage_api.lineage.exposure import decay, units
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
from answerlineage_workers.recall import Entry, ranked

pytestmark = pytest.mark.anyio
integration = pytest.mark.integration

D0 = datetime(2026, 9, 1, 6, 0, tzinfo=UTC)
# Le graphe est connu peu avant l'heure de l'analyse, qui est l'heure de référence.
RECEIVED = D0 + timedelta(days=59)
KNOWN_AT = D0 + timedelta(days=60)
SALES = AgentRef("agent://ventes", "assistant-commercial")
SQL = ToolRef("entrepot", "execute_sql")
RATES = DatasetRef("bigquery", "finance.ref.taux_change")


def days(count: float) -> datetime:
    return D0 + timedelta(days=count)


def pseudonym(rank: int) -> str:
    return f"hmac-sha256:{rank:064x}"


def fingerprint(rank: int) -> str:
    return f"sha256:{rank:064x}"


def answered(rank: int, read_on: float, produced_on: float, *recipients: str) -> list[RunRecord]:
    """Une réponse produite le jour ``produced_on``, d'un run qui a lu les taux le jour
    ``read_on``, diffusée à ces destinataires."""
    run_id = uuid4()
    delivered = Delivery("slack", f"message-{rank}", recipients=recipients)
    deliveries = (delivered,) if recipients else ()
    return [
        RunRecord(
            run_id=run_id,
            kind="agent",
            state="COMPLETE",
            event_time=days(produced_on),
            agent=SALES,
            answers=(Answer(fingerprint(rank), deliveries=deliveries),),
            recorded_at=RECEIVED,
        ),
        RunRecord(
            run_id=uuid4(),
            kind="tool",
            state="COMPLETE",
            event_time=days(read_on),
            tool=SQL,
            parent_run_id=run_id,
            reads=(Read(RATES),),
            recorded_at=RECEIVED,
        ),
    ]


async def record(engine: AsyncEngine, *answers: list[RunRecord]) -> None:
    async with engine.begin() as conn:
        for runs in answers:
            for item in runs:
                await record_run(conn, item)


async def declared(engine: AsyncEngine) -> UUID:
    """Un incident sur les taux, du premier au trente et unième jour, à analyser à
    ``KNOWN_AT``."""
    document = {
        "scope": {"datasets": [{"namespace": RATES.namespace, "name": RATES.name}]},
        "window": {"from": days(0).isoformat(), "to": days(31).isoformat()},
        "severity": "high",
    }
    declaration, errors = validate_declaration(document)
    assert declaration is not None, errors
    async with engine.begin() as conn:
        incident_id = (await declare(conn, declaration)).id
        await conn.execute(
            text("UPDATE incident_analysis SET known_at = :at WHERE incident_id = :id"),
            {"at": KNOWN_AT, "id": incident_id},
        )
    return incident_id


async def rows(engine: AsyncEngine, sql: str, **params: Any) -> list[tuple[Any, ...]]:
    async with engine.connect() as conn:
        return [tuple(row) for row in await conn.execute(text(sql), params)]


async def entries(engine: AsyncEngine, incident_id: UUID) -> list[tuple[Any, ...]]:
    return await rows(
        engine,
        "SELECT rank, pseudonym, exposure, answers, latest_answer_at FROM recall_entry"
        " WHERE incident_id = :id ORDER BY rank",
        id=incident_id,
    )


async def entry_answers(engine: AsyncEngine, incident_id: UUID) -> list[tuple[Any, ...]]:
    return await rows(
        engine,
        "SELECT pseudonym, fingerprint, produced_at FROM recall_entry_answer"
        " WHERE incident_id = :id ORDER BY pseudonym, fingerprint",
        id=incident_id,
    )


async def recall_list(engine: AsyncEngine, incident_id: UUID) -> tuple[Any, ...] | None:
    found = await rows(
        engine,
        "SELECT reference_at, recipients, answers FROM recall_list WHERE incident_id = :id",
        id=incident_id,
    )
    return found[0] if found else None


def exposure(*produced_on: float) -> int:
    """L'exposition attendue, en millionièmes : les poids du score d'exposition."""
    return units(sum(decay(days(day), KNOWN_AT) for day in produced_on))


@integration
async def test_a_recipient_of_five_touched_answers_appears_once_with_the_five(
    engine: AsyncEngine,
) -> None:
    """Critère : un destinataire de cinq réponses touchées figure une fois dans la liste, avec
    les cinq réponses."""
    camille, dominique = pseudonym(1), pseudonym(2)
    await record(
        engine,
        *(answered(rank, 10, 10 + rank, camille) for rank in range(1, 5)),
        answered(5, 12, 20, camille, dominique),
    )
    incident_id = await declared(engine)

    assert await process_analysis(engine) == Processed(incident_id, "done")

    listed = await entries(engine, incident_id)
    assert [(rank, who, count) for rank, who, _, count, _ in listed] == [
        (1, camille, 5),
        (2, dominique, 1),
    ]
    assert await entry_answers(engine, incident_id) == [
        *((camille, fingerprint(rank), days(10 + rank)) for rank in range(1, 5)),
        (camille, fingerprint(5), days(20)),
        (dominique, fingerprint(5), days(20)),
    ]
    assert await recall_list(engine, incident_id) == (KNOWN_AT, 2, 5)


@integration
async def test_the_most_exposed_recipient_comes_first(engine: AsyncEngine) -> None:
    """Critère : de deux destinataires, celui dont l'exposition est la plus forte vient en
    premier. Une réponse de quinze jours pèse plus que deux réponses de soixante jours."""
    camille, dominique = pseudonym(1), pseudonym(2)
    await record(
        engine,
        answered(1, 0, 0, dominique),
        answered(2, 0, 0, dominique),
        answered(3, 29, 45, camille),
    )
    incident_id = await declared(engine)

    assert await process_analysis(engine) == Processed(incident_id, "done")

    assert await entries(engine, incident_id) == [
        (1, camille, exposure(45), 1, days(45)),
        (2, dominique, exposure(0, 0), 2, days(0)),
    ]
    # Les poids du score d'exposition : 2^(-15/30) pour l'une, deux fois 2^(-60/30) pour l'autre.
    assert (exposure(45), exposure(0, 0)) == (707_107, 500_000)


@integration
async def test_equal_exposures_are_ranked_by_answers_then_latest_answer_then_pseudonym(
    engine: AsyncEngine,
) -> None:
    """Le tri est total : à exposition égale, le plus de réponses, puis la plus récente, puis
    le pseudonyme. Deux réponses de soixante jours pèsent autant qu'une de trente."""
    twice, once, first, second = pseudonym(4), pseudonym(3), pseudonym(1), pseudonym(2)
    await record(
        engine,
        answered(1, 0, 0, twice),
        answered(2, 0, 0, twice),
        answered(3, 20, 30, once, second, first),
    )
    incident_id = await declared(engine)

    assert await process_analysis(engine) == Processed(incident_id, "done")

    listed = await entries(engine, incident_id)
    assert [(rank, who, value) for rank, who, value, _, _ in listed] == [
        (1, twice, 500_000),
        (2, first, 500_000),
        (3, second, 500_000),
        (4, once, 500_000),
    ]


def test_at_equal_exposure_and_answers_the_latest_answer_comes_first() -> None:
    """Une seconde d'écart ne change pas l'exposition au millionième : la réponse la plus
    récente départage, avant le pseudonyme."""
    at = days(60)
    recent, older = pseudonym(9), pseudonym(1)
    found = ranked([(older, at - timedelta(seconds=1)), (recent, at)], at)
    assert found == [
        Entry(recent, 1_000_000, 1, at),
        Entry(older, 1_000_000, 1, at - timedelta(seconds=1)),
    ]
    # L'ordre ne dépend pas de celui des liens.
    assert ranked([(recent, at), (older, at - timedelta(seconds=1))], at) == found


@integration
async def test_rebuilding_the_list_from_the_same_analysis_gives_the_same_list(
    engine: AsyncEngine,
) -> None:
    camille, dominique = pseudonym(1), pseudonym(2)
    await record(engine, answered(1, 3, 4, camille, dominique), answered(2, 5, 9, dominique))
    incident_id = await declared(engine)

    snapshots = []
    for _ in range(2):
        async with engine.begin() as conn:
            await analyze_incident(conn, incident_id, KNOWN_AT, time.monotonic() + 60)
        snapshots.append(
            (
                await recall_list(engine, incident_id),
                await entries(engine, incident_id),
                await entry_answers(engine, incident_id),
            )
        )

    assert snapshots[0] == snapshots[1]
    assert [who for _, who, *_ in snapshots[0][1]] == [dominique, camille]


@integration
async def test_a_replayed_analysis_rebuilds_the_list_from_its_new_result(
    engine: AsyncEngine,
) -> None:
    camille, dominique = pseudonym(1), pseudonym(2)
    await record(engine, answered(1, 3, 4, camille))
    incident_id = await declared(engine)
    assert await process_analysis(engine) == Processed(incident_id, "done")

    # Une réponse connue depuis : l'exploitant rejoue l'analyse sur le graphe d'aujourd'hui.
    await record(engine, answered(2, 5, 50, dominique))
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE incident_analysis SET state = 'pending', available_at = now(),"
                " finished_at = NULL, answers = NULL, recipients = NULL, truncated = NULL,"
                " coverage = NULL, unknown_datasets = NULL, known_at = :at"
                " WHERE incident_id = :id"
            ),
            {"at": KNOWN_AT + timedelta(days=1), "id": incident_id},
        )
    assert await process_analysis(engine) == Processed(incident_id, "done")

    assert [who for _, who, *_ in await entries(engine, incident_id)] == [dominique, camille]
    assert await recall_list(engine, incident_id) == (KNOWN_AT + timedelta(days=1), 2, 2)


@integration
async def test_a_truncated_analysis_has_no_list_and_loses_the_one_it_had(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Une analyse tronquée ne dit pas tous les destinataires : elle n'a pas de liste."""
    await record(engine, answered(1, 3, 4, pseudonym(1)), answered(2, 5, 6, pseudonym(2)))
    incident_id = await declared(engine)
    async with engine.begin() as conn:
        await analyze_incident(conn, incident_id, KNOWN_AT, time.monotonic() + 60)
    assert await recall_list(engine, incident_id) is not None

    monkeypatch.setattr(incidents, "MAX_ANSWERS", 1)
    async with engine.begin() as conn:
        await analyze_incident(conn, incident_id, KNOWN_AT, time.monotonic() + 60)

    assert await rows(
        engine, "SELECT truncated FROM incident_analysis WHERE incident_id = :id", id=incident_id
    ) == [(True,)]
    assert await recall_list(engine, incident_id) is None
    assert await entries(engine, incident_id) == []
    assert await entry_answers(engine, incident_id) == []


@integration
async def test_answers_without_recipients_leave_an_empty_list(engine: AsyncEngine) -> None:
    """Une réponse touchée sans destinataire connu n'a personne à prévenir : la liste existe,
    vide, et dit qu'il n'y a rien à rappeler."""
    await record(engine, answered(1, 3, 4))
    incident_id = await declared(engine)

    assert await process_analysis(engine) == Processed(incident_id, "done")

    assert await rows(
        engine, "SELECT answers FROM incident_analysis WHERE incident_id = :id", id=incident_id
    ) == [(1,)]
    assert await recall_list(engine, incident_id) == (KNOWN_AT, 0, 0)
    assert await entries(engine, incident_id) == []
