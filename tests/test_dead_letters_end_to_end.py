"""La file de rebut de bout en bout : ingestion, worker, API de lecture, ligne de commande.

Un défaut injecté dans la projection lui fait refuser les événements d'un
émetteur, comme le ferait une erreur de schéma. Ils arrivent au rebut avec leur
motif et leur émetteur, et cessent de tourner. Le défaut corrigé, le rejeu les
intègre au graphe, une fois et une seule. Ni le message de l'erreur ni le
contenu des événements n'atteignent le rebut, les réponses ou le journal.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from typing import Any, cast

import anyio
import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_api.ingest.store import event_identifier
from answerlineage_api.store.graph import DatasetKind, RunRecord
from answerlineage_cli.exits import Exit
from answerlineage_workers.projection import project
from answerlineage_workers.queue import BatchResult, process_batch
from api.tests.events import mutated, run_event
from cli.tests.fakes import Run, run
from proxy.tests.conftest import running

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

DEAD_LETTERS = "/api/v1/ingestion/dead-letters"
REPLAY = f"{DEAD_LETTERS}/replay"
CANARY = "canari0a4c"
BROKEN = "mcp://catalogue"


def event_of(index: int, namespace: str = "mcp://entrepot") -> dict[str, Any]:
    """Un appel d'outil qui lit une table au nom porteur du canari : un contenu."""
    event = mutated(["job", "namespace"], namespace, base=run_event(index))
    event["inputs"][0]["name"] = f"catalogue.{CANARY}.t{index}"
    return event  # type: ignore[no-any-return]


def schema_error(record: RunRecord) -> RunRecord:
    """Une erreur de schéma : la projection range la table lue sous une nature que le graphe
    refuse. La base le dit par un message qui cite la ligne, nom de la table compris."""
    unknown = cast(DatasetKind, "catalogue")
    return replace(
        record,
        reads=tuple(
            replace(read, dataset=replace(read.dataset, kind=unknown)) for read in record.reads
        ),
    )


def missing_field(_: RunRecord) -> RunRecord:
    raise KeyError(CANARY)


def broken_projection(monkeypatch: pytest.MonkeyPatch, defect: Any) -> None:
    """La projection déployée a un défaut pour les événements de ``BROKEN``."""

    def projected(payload: Mapping[str, Any]) -> RunRecord:
        record = project(payload)
        return defect(record) if payload["job"]["namespace"] == BROKEN else record

    monkeypatch.setattr("answerlineage_workers.queue.project", projected)


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(engine))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield client


async def scalar(engine: AsyncEngine, sql: str) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql))).scalar_one()


async def drain(engine: AsyncEngine) -> BatchResult:
    processed = buried = 0
    while not (result := await process_batch(engine)).empty:
        processed += result.processed
        buried += result.dead_lettered
    return BatchResult(processed=processed, dead_lettered=buried)


async def queues(engine: AsyncEngine) -> str:
    """La file et le rebut, ligne par ligne : ce qu'ils gardent d'un échec."""
    async with engine.connect() as conn:
        found = await conn.execute(
            text(
                "SELECT CAST(row_to_json(d) AS text) FROM dead_letter d"
                " UNION ALL SELECT CAST(row_to_json(q) AS text) FROM ingest_queue q"
            )
        )
        return "\n".join(found.scalars().all())


async def test_a_refused_event_waits_in_the_dead_letters_then_a_replay_integrates_it_once(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sound, refused = event_of(0), event_of(1, BROKEN)
    accepted = await client.post("/api/v1/lineage/batch", json=[sound, refused])
    assert accepted.json() == {"accepted": 2, "duplicates": 0, "rejected": []}
    broken_projection(monkeypatch, schema_error)

    with caplog.at_level(logging.DEBUG):
        assert await drain(engine) == BatchResult(processed=1, dead_lettered=1)

        # Critère : la file de rebut dit le motif et l'émetteur.
        listed = (await client.get(DEAD_LETTERS)).json()
        [waiting] = listed["dead_letters"]
        assert (waiting["event_id"], waiting["emitter"], waiting["reason"]) == (
            event_identifier(refused),
            BROKEN,
            "constraint_violation",
        )
        assert waiting["run_id"] == refused["run"]["runId"]
        assert {item["reason"]: item["count"] for item in listed["counts"]}[
            "constraint_violation"
        ] == 1
        # Ni le contenu de l'événement, ni le message de l'erreur, qui cite la ligne refusée.
        assert CANARY not in await queues(engine)
        assert CANARY not in json.dumps(listed)
        # Il ne tourne plus dans la file, même rendu disponible.
        assert await scalar(engine, "SELECT count(*) FROM ingest_queue") == 0
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE ingest_queue SET available_at = now()"))
        assert (await process_batch(engine)).empty

        # Critère : le correctif déployé, le rejeu intègre l'événement au graphe.
        monkeypatch.undo()
        replayed = await client.post(REPLAY, json={"reason": "constraint_violation"})
        assert (replayed.status_code, replayed.json()) == (202, {"replayed": 1, "more": False})
        assert await drain(engine) == BatchResult(processed=1)
    # Le journal nomme l'événement, son motif et la classe de l'erreur, jamais son message.
    assert "mis au rebut : constraint_violation (IntegrityError)" in caplog.text
    assert CANARY not in caplog.text

    run_id = refused["run"]["runId"]
    assert await scalar(engine, f"SELECT count(*) FROM run WHERE run_id = '{run_id}'") == 1  # noqa: S608
    assert await scalar(engine, "SELECT count(*) FROM read_edge") == 2
    assert (await client.get(DEAD_LETTERS)).json()["dead_letters"] == []
    # Rejouer encore ne trouve rien ; renvoyer l'événement intégré est un doublon, sans effet.
    assert (await client.post(REPLAY, json={})).json() == {"replayed": 0, "more": False}
    again = await client.post("/api/v1/lineage", json=refused)
    assert again.json()["duplicate"] is True
    assert (await drain(engine)).empty
    assert await scalar(engine, "SELECT count(*) FROM read_edge") == 2


async def test_a_replayed_event_that_fails_again_comes_back_with_its_new_reason(
    client: httpx.AsyncClient, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    await client.post("/api/v1/lineage/batch", json=[event_of(0, BROKEN)])
    broken_projection(monkeypatch, schema_error)
    await drain(engine)

    # Le correctif déployé en introduit un autre : l'événement rejoué échoue autrement.
    broken_projection(monkeypatch, missing_field)
    assert (await client.post(REPLAY, json={"emitter": BROKEN})).json()["replayed"] == 1
    assert await drain(engine) == BatchResult(dead_lettered=1)

    [back] = (await client.get(DEAD_LETTERS, params={"emitter": BROKEN})).json()["dead_letters"]
    assert (back["reason"], back["attempts"]) == ("invalid_event", 1)
    assert await scalar(engine, "SELECT count(*) FROM run") == 0


async def command(monkeypatch: pytest.MonkeyPatch, url: str, *argv: str) -> Run:
    return await anyio.to_thread.run_sync(lambda: run([*argv, "--url", url], None, monkeypatch))


async def test_the_command_lists_and_replays_the_dead_letters_of_the_real_api(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with running(create_app(engine)) as url:
        async with httpx.AsyncClient(base_url=url) as client:
            await client.post("/api/v1/lineage/batch", json=[event_of(0), event_of(1, BROKEN)])
        broken_projection(monkeypatch, schema_error)
        await drain(engine)
        monkeypatch.undo()

        listed = await command(monkeypatch, url, "dead-letters", "list", "--emitter", BROKEN)
        assert listed.code == Exit.WAITING, listed.err
        assert f"Rebut de {BROKEN} : 1 événement" in listed.out
        assert f"constraint_violation  {BROKEN}" in listed.out
        assert event_identifier(event_of(1, BROKEN)) in listed.out
        shown = await command(monkeypatch, url, "dead-letters", "list", "--format", "json")
        assert json.loads(shown.out)["dead_letters"][0]["emitter"] == BROKEN

        replayed = await command(
            monkeypatch, url, "dead-letters", "replay", "--reason", "constraint_violation"
        )
        assert replayed.code == Exit.OK, replayed.err
        assert "1 événement remis en file" in replayed.out
        assert await drain(engine) == BatchResult(processed=1)

        empty = await command(monkeypatch, url, "dead-letters", "list")
        assert (empty.code, empty.out) == (Exit.OK, "Rebut vide : aucun événement n'attend.\n")
        nothing = await command(monkeypatch, url, "dead-letters", "replay", "--all")
        assert nothing.code == Exit.NOTHING_TO_REPLAY
