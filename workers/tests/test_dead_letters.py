"""La file de rebut : un événement que la projection n'intègre pas cesse de tourner.

Une erreur qui ne se corrigera pas en réessayant met l'événement au rebut au
premier échec ; une autre, au nombre d'essais réglé. Le passage au rebut et le
retrait de la file se font dans la transaction du lot : ni perte, ni double.
Le rebut ne garde ni le message de l'erreur, ni rien du contenu de l'événement.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any, cast

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from answerlineage_api.identifiers import InvalidIdentifierError, identifier_fault
from answerlineage_api.ingest.store import event_identifier
from answerlineage_api.store.graph import DatasetKind, RunRecord
from answerlineage_workers import cli
from answerlineage_workers.projection import project
from answerlineage_workers.queue import (
    DEFAULT_MAX_ATTEMPTS,
    BatchResult,
    failure_reason,
    process_batch,
    record_event,
)
from workers.tests.test_queue import ingest, scalar, tool_call

pytestmark = pytest.mark.anyio
integration = pytest.mark.integration

CANARY = "canari5e2b"
# La table que lit l'événement empoisonné : un contenu, qui ne doit apparaître nulle part.
POISONED = f"ref.{CANARY}"
# L'émetteur de l'événement empoisonné, écrit en forme décomposée : le rebut le garde
# sous sa forme normalisée, celle que le graphe et les compteurs retiennent.
DECOMPOSED = "mcp://société"
EMITTER = "mcp://société"

Defect = Callable[[RunRecord], RunRecord]


def _unknown_identifier(_: RunRecord) -> RunRecord:
    fault = identifier_fault(f"nom {CANARY}", max_length=64)
    assert fault is not None
    raise InvalidIdentifierError(fault)


def _missing_field(_: RunRecord) -> RunRecord:
    raise KeyError(CANARY)


def _refused_kind(record: RunRecord) -> RunRecord:
    """La table lue sous une nature que le schéma du graphe ne connaît pas : sa contrainte la
    refuse, et le message de la base cite la ligne, nom de la table compris."""
    unknown = cast(DatasetKind, "robot")
    reads = tuple(
        replace(read, dataset=replace(read.dataset, kind=unknown)) for read in record.reads
    )
    return replace(record, reads=reads)


def _unstorable_version(record: RunRecord) -> RunRecord:
    version = f"{CANARY}\x00"
    reads = tuple(
        replace(read, dataset=replace(read.dataset, version=version)) for read in record.reads
    )
    return replace(record, reads=reads)


DEFECTS: dict[str, tuple[Defect, str]] = {
    "identifiant-refuse": (_unknown_identifier, "invalid_identifier"),
    "champ-absent": (_missing_field, "invalid_event"),
    "contrainte-violee": (_refused_kind, "constraint_violation"),
    "valeur-refusee": (_unstorable_version, "invalid_value"),
}


def poisoned(index: int) -> dict[str, Any]:
    event = tool_call(index, table=POISONED)
    event["job"]["namespace"] = DECOMPOSED
    return event


def inject(monkeypatch: pytest.MonkeyPatch, defect: Defect) -> None:
    """Un défaut dans la projection, pour les seuls événements qui lisent la table empoisonnée."""

    def projected(payload: Mapping[str, Any]) -> RunRecord:
        record = project(payload)
        if any(read.dataset.name == POISONED for read in record.reads):
            return defect(record)
        return record

    monkeypatch.setattr("answerlineage_workers.queue.project", projected)


async def rows(engine: AsyncEngine, sql: str) -> list[tuple[Any, ...]]:
    async with engine.connect() as conn:
        return [tuple(row) for row in await conn.execute(text(sql))]


async def make_available(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE ingest_queue SET available_at = now()"))


@integration
@pytest.mark.parametrize("defect", list(DEFECTS), ids=list(DEFECTS))
async def test_an_event_the_projection_refuses_goes_to_the_dead_letters_at_once(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    defect: str,
) -> None:
    """Critère : le rebut garde le motif et l'émetteur, et l'événement ne tourne plus."""
    broken, reason = DEFECTS[defect]
    inject(monkeypatch, broken)
    sound, bad = [tool_call(0), tool_call(1)], poisoned(2)
    await ingest(engine, [*sound, bad])

    with caplog.at_level(logging.INFO, logger="answerlineage_workers"):
        result = await process_batch(engine)

    assert result == BatchResult(processed=2, dead_lettered=1)
    assert await rows(
        engine,
        "SELECT event_id, recorded_month = date_trunc('month', now())::date, emitter, reason,"
        " attempts, first_failed_at = last_failed_at FROM dead_letter",
    ) == [(event_identifier(bad), True, EMITTER, reason, 1, True)]
    # Il ne tourne plus : la file est vide, même quand tout y redevient disponible.
    assert await scalar(engine, "SELECT count(*) FROM ingest_queue") == 0
    await make_available(engine)
    assert (await process_batch(engine)).empty
    # L'événement brut reste, désigné par le rebut ; rien de lui n'est entré dans le graphe.
    assert await scalar(engine, "SELECT count(*) FROM event_raw") == 3
    assert await scalar(engine, "SELECT count(*) FROM run") == 2
    assert await scalar(engine, f"SELECT count(*) FROM dataset WHERE name = '{POISONED}'") == 0  # noqa: S608

    # Ni le message de l'erreur ni le contenu de l'événement : ni au rebut, ni au journal.
    stored = await rows(engine, "SELECT CAST(row_to_json(d) AS text) FROM dead_letter d")
    assert CANARY not in str(stored)
    assert CANARY not in caplog.text
    assert f"Événement {event_identifier(bad)} mis au rebut : {reason}" in caplog.text


@integration
@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (OperationalError("SELECT 1", {}, ConnectionError(CANARY)), "database_error"),
        (RuntimeError(CANARY), "projection_error"),
    ],
    ids=["base", "autre"],
)
async def test_a_passing_error_goes_to_the_dead_letters_only_at_the_set_number_of_attempts(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture, error: Exception, reason: str
) -> None:
    await ingest(engine, [tool_call(0)])

    async def failing(conn: AsyncConnection, payload: Mapping[str, Any], at: datetime) -> None:
        raise error

    with caplog.at_level(logging.INFO, logger="answerlineage_workers"):
        for attempt in (1, 2):
            await make_available(engine)
            result = await process_batch(engine, handler=failing, max_attempts=3)
            assert result == BatchResult(postponed=1)
            assert await rows(engine, "SELECT attempts FROM ingest_queue") == [(attempt,)]
            assert await scalar(engine, "SELECT count(*) FROM dead_letter") == 0
        await make_available(engine)
        assert await process_batch(engine, handler=failing, max_attempts=3) == BatchResult(
            dead_lettered=1
        )

    assert await scalar(engine, "SELECT count(*) FROM ingest_queue") == 0
    # La première heure d'échec est celle du premier essai, la dernière celle du troisième.
    assert await rows(
        engine, "SELECT reason, attempts, first_failed_at < last_failed_at FROM dead_letter"
    ) == [(reason, 3, True)]
    assert "essai 2 sur 3" in caplog.text
    assert f"{reason} ({type(error).__name__}), après 3 essai(s)" in caplog.text
    assert CANARY not in caplog.text


@integration
async def test_a_passing_error_is_retried_ten_times_by_default(engine: AsyncEngine) -> None:
    await ingest(engine, [tool_call(0)])

    async def failing(conn: AsyncConnection, payload: Mapping[str, Any], at: datetime) -> None:
        raise RuntimeError

    for _ in range(DEFAULT_MAX_ATTEMPTS - 1):
        await make_available(engine)
        assert await process_batch(engine, handler=failing) == BatchResult(postponed=1)
    await make_available(engine)
    assert await process_batch(engine, handler=failing) == BatchResult(dead_lettered=1)
    assert await rows(engine, "SELECT attempts FROM dead_letter") == [(DEFAULT_MAX_ATTEMPTS,)]


@integration
async def test_two_concurrent_workers_send_each_poisoned_event_to_the_dead_letters_once(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    inject(monkeypatch, _refused_kind)
    events = [poisoned(index) if index % 5 == 0 else tool_call(index) for index in range(60)]
    await ingest(engine, events)
    seen: Counter[str] = Counter()
    overlapping = active = 0

    async def counting(conn: AsyncConnection, payload: Mapping[str, Any], at: datetime) -> None:
        nonlocal active, overlapping
        active += 1
        overlapping = max(overlapping, active)
        seen[payload["run"]["runId"]] += 1
        try:
            await record_event(conn, payload, at)
        finally:
            # Laisse l'autre worker réclamer son lot pendant que celui-ci est ouvert.
            await asyncio.sleep(0.002)
            active -= 1

    async def worker() -> BatchResult:
        processed = buried = 0
        while not (result := await process_batch(engine, batch_size=7, handler=counting)).empty:
            processed += result.processed
            buried += result.dead_lettered
        return BatchResult(processed=processed, dead_lettered=buried)

    first, second = await asyncio.gather(worker(), worker())

    assert overlapping == 2, "les deux lots doivent avoir été ouverts en même temps"
    assert first.processed + second.processed == 48
    assert first.dead_lettered + second.dead_lettered == 12
    assert set(seen.values()) == {1} and len(seen) == 60
    assert await rows(engine, "SELECT count(*), min(attempts), max(attempts) FROM dead_letter") == [
        (12, 1, 1)
    ]
    assert await scalar(engine, "SELECT count(*) FROM run") == 48
    assert await scalar(engine, "SELECT count(*) FROM ingest_queue") == 0


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (InvalidIdentifierError(identifier_fault(" ", max_length=1)), "invalid_identifier"),  # type: ignore[arg-type]
        (KeyError("run"), "invalid_event"),
        (TypeError(), "invalid_event"),
        (ValueError(), "invalid_event"),
        (OperationalError("SELECT 1", {}, ConnectionError()), "database_error"),
        (RuntimeError(), "projection_error"),
        (AttributeError(), "projection_error"),
    ],
)
def test_the_reason_of_a_failure_follows_the_class_of_the_error(
    error: Exception, reason: str
) -> None:
    assert failure_reason(error) == reason


def test_the_command_line_sets_the_number_of_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[int] = []

    async def serve(batch_size: int, max_attempts: int, **_: object) -> None:
        received.append(max_attempts)

    monkeypatch.setattr(cli, "serve", serve)
    assert cli.main([]) == 0
    assert cli.main(["--max-attempts", "3"]) == 0
    assert received == [DEFAULT_MAX_ATTEMPTS, 3]


@pytest.mark.parametrize("value", ["0", "-1", "2.5", "dix", "101"])
def test_the_command_line_refuses_a_number_of_attempts_out_of_bounds(
    value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.main(["--max-attempts", value])
    assert stopped.value.code == 2
    assert "essais" in capsys.readouterr().err
