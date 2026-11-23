"""La réémission vers un consommateur OpenLineage externe, devant une fausse destination.

Seul ce que la projection intègre au graphe part, tel qu'il a été reçu et
accepté. La file tient dans PostgreSQL : rien ne se perd quand la destination
tombe puis revient, une issue incertaine est renvoyée, deux workers n'envoient
jamais deux fois le même événement, et ce qui déborde est compté et dit. Une
destination en panne ne retarde ni l'ingestion ni la projection, et le journal
ne dit ni l'adresse, ni l'en-tête d'authentification, ni rien d'un événement.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import socket
import threading
import time
from collections import Counter
from collections.abc import AsyncIterator, Mapping
from datetime import timedelta
from random import Random
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_api.store.engine import create_engine
from answerlineage_workers import cli, reemission
from answerlineage_workers.destination import Sender, Timeouts, parse_destination
from answerlineage_workers.projection import project
from answerlineage_workers.queue import BatchResult, process_batch, run_worker
from answerlineage_workers.rebuild import rebuild_graph
from answerlineage_workers.reemission import (
    Reemission,
    ReemitResult,
    reemit_batch,
    retry_delay,
    run_reemission,
    trim,
)
from api.tests.events import mutated, run_event
from workers.tests.catalog import LINEAGE, Answer, FakeCatalog, serving

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

CANARY = "canari3d8f"
BROKEN = "mcp://catalogue"
FAST = Timeouts(connect=1.0, io=1.0, request=1.0)


def event_of(index: int, namespace: str = "mcp://entrepot") -> dict[str, Any]:
    """Un appel d'outil qui lit une table au nom porteur du canari : un contenu."""
    event = mutated(["job", "namespace"], namespace, base=run_event(index))
    event["inputs"][0]["name"] = f"finance.{CANARY}.t{index}"
    return event  # type: ignore[no-any-return]


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(engine))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield client


async def ingested(client: httpx.AsyncClient, events: list[dict[str, Any]]) -> dict[str, Any]:
    response = await client.post("/api/v1/lineage/batch", json=events)
    assert response.status_code in (202, 422), response.text
    body: dict[str, Any] = response.json()
    return body


async def project_all(engine: AsyncEngine, *, reemit: bool = True) -> BatchResult:
    processed = buried = 0
    while not (result := await process_batch(engine, reemit=reemit)).empty:
        processed += result.processed
        buried += result.dead_lettered
    return BatchResult(processed=processed, dead_lettered=buried)


async def rows(engine: AsyncEngine, sql: str, **params: Any) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(text(sql), params)).all())


async def scalar(engine: AsyncEngine, sql: str) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql))).scalar_one()


async def queued(engine: AsyncEngine) -> list[str]:
    found = await rows(engine, "SELECT event_id FROM reemission_queue ORDER BY id")
    return [row.event_id for row in found]


async def stats(engine: AsyncEngine) -> tuple[int, int, int]:
    found = await rows(engine, "SELECT sent, rejected, dropped FROM reemission_stats")
    return (0, 0, 0) if not found else tuple(found[0])


async def drain(engine: AsyncEngine, sender: Sender) -> ReemitResult:
    sent = rejected = dropped = 0
    while not (result := await reemit_batch(engine, sender)).empty:
        assert result.failure is None, result.failure
        sent, rejected, dropped = (
            sent + result.sent,
            rejected + result.rejected,
            dropped + result.dropped,
        )
    return ReemitResult(sent=sent, rejected=rejected, dropped=dropped)


def identifier(event: Mapping[str, Any]) -> str:
    canonical = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def test_only_what_the_projection_integrates_into_the_graph_is_queued_for_reemission(
    client: httpx.AsyncClient, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    sound, refused, buried = event_of(0), mutated(["run", "runId"], CANARY), event_of(1, BROKEN)
    received = await ingested(client, [sound, refused, sound, buried])
    assert (received["accepted"], received["duplicates"], len(received["rejected"])) == (3, 1, 1)

    def projected(payload: Mapping[str, Any]) -> Any:
        if payload["job"]["namespace"] == BROKEN:
            raise KeyError(CANARY)
        return project(payload)

    monkeypatch.setattr("answerlineage_workers.queue.project", projected)
    assert await project_all(engine) == BatchResult(processed=1, dead_lettered=1)

    # Ni l'événement refusé à l'ingestion, ni le doublon, ni celui mis au rebut.
    assert await queued(engine) == [identifier(sound)]
    # Sans destination réglée, un worker ne met rien en file de réémission.
    await ingested(client, [event_of(2)])
    assert await project_all(engine, reemit=False) == BatchResult(processed=1)
    assert await queued(engine) == [identifier(sound)]


async def test_the_event_leaves_as_received_and_accepted_with_nothing_added(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    event = event_of(0)
    # Une autre écriture du même événement : les clés dans un autre ordre.
    reordered = dict(reversed(list(event.items())))
    await ingested(client, [reordered])
    await project_all(engine)
    catalog = FakeCatalog()
    async with serving(catalog) as base:
        sender = Sender(parse_destination(base + LINEAGE, f"Bearer {CANARY}"), timeouts=FAST)
        assert await drain(engine, sender) == ReemitResult(sent=1)
        sender.close()

    (request,) = catalog.received
    assert json.loads(request.body) == event
    # La forme canonique, dont l'empreinte est l'identifiant de l'événement : rien d'ajouté.
    assert hashlib.sha256(request.body).hexdigest() == identifier(event)
    assert set(json.loads(request.body)["run"]["facets"]) == {"parent", "answerlineage_toolCall"}
    assert request.headers["authorization"] == f"Bearer {CANARY}"
    assert await queued(engine) == []
    assert await stats(engine) == (1, 0, 0)


async def test_a_destination_down_then_back_loses_nothing(
    client: httpx.AsyncClient, engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    events = [event_of(index) for index in range(5)]
    await ingested(client, events)
    await project_all(engine)
    catalog = FakeCatalog().then(Answer(503), Answer(503), Answer(503))
    async with serving(catalog) as base:
        sender = Sender(parse_destination(base + LINEAGE), timeouts=FAST)
        # En panne : le lot s'arrête au premier échec, qui compte un essai à cet événement seul.
        assert await reemit_batch(engine, sender) == ReemitResult(failure="HTTP 503")
        waiting = await rows(
            engine, "SELECT attempts, last_error FROM reemission_queue ORDER BY id"
        )
        assert [tuple(row) for row in waiting] == [(1, "HTTP 503")] + [(0, None)] * 4

        stop = asyncio.Event()
        with caplog.at_level(logging.INFO, logger="answerlineage_workers"):
            loop = asyncio.create_task(
                run_reemission(
                    engine,
                    stop,
                    Reemission(sender_destination(base)),
                    sender=sender,
                    idle_delay=0.01,
                    backoff=(0.01, 0.05),
                    random=Random(7),  # noqa: S311 — une gigue reproductible.
                )
            )
            await until(lambda: queued(engine), [])
            stop.set()
            await asyncio.wait_for(loop, timeout=5)

    assert catalog.bodies()[-5:] == [body_of(event) for event in events]
    assert len(catalog.received) == 3 + 5
    assert await stats(engine) == (5, 0, 0)
    assert caplog.text.count("Réémission en échec (HTTP 503)") == 1
    assert "Réémission rétablie après 2 échec(s)" in caplog.text


async def test_an_uncertain_delivery_is_sent_again(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    event = event_of(0)
    await ingested(client, [event])
    await project_all(engine)
    # La destination prend l'événement, mais sa réponse arrive après le délai.
    catalog = FakeCatalog().then(Answer(delay=3))
    async with serving(catalog) as base:
        sender = Sender(parse_destination(base + LINEAGE), timeouts=Timeouts(1.0, 1.0, 0.5))
        assert await reemit_batch(engine, sender) == ReemitResult(failure="TimeoutError")
        assert await drain(engine, sender) == ReemitResult(sent=1)
        sender.close()
    # Au moins une fois : la destination l'a reçu deux fois, il n'est compté remis qu'une.
    assert catalog.bodies() == [body_of(event)] * 2
    assert await stats(engine) == (1, 0, 0)


async def test_an_event_the_destination_refuses_is_dropped_and_counted_lost(
    client: httpx.AsyncClient, engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    await ingested(client, [event_of(index) for index in range(3)])
    await project_all(engine)
    catalog = FakeCatalog().then(Answer(422))
    async with serving(catalog) as base:
        sender = Sender(parse_destination(base + LINEAGE), timeouts=FAST)
        with caplog.at_level(logging.WARNING, logger="answerlineage_workers"):
            assert await reemit_batch(engine, sender) == ReemitResult(sent=2, rejected=1)
        sender.close()
    assert len(catalog.received) == 3
    assert await queued(engine) == []
    assert await stats(engine) == (2, 1, 0)
    assert (
        "Réémission : 1 événement(s) refusé(s) par la destination (HTTP 422) : abandonnés,"
        " comptés perdus." in caplog.text
    )


async def test_the_overflow_of_the_queue_is_counted_and_said(
    client: httpx.AsyncClient, engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    events = [event_of(index) for index in range(6)]
    await ingested(client, events)
    await project_all(engine)
    destination = parse_destination("https://catalogue.interne/api/v1/lineage")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE reemission_queue SET enqueued_at = now() - interval '3 hours'"
                " WHERE event_id = :event_id"
            ),
            {"event_id": identifier(events[0])},
        )

    with caplog.at_level(logging.WARNING, logger="answerlineage_workers"):
        # Trop ancien : le premier ; puis au-delà de trois événements, les plus anciens.
        assert await trim(engine, Reemission(destination, max_age=timedelta(hours=2))) == 1
        assert await trim(engine, Reemission(destination, max_events=3)) == 2
        assert await trim(engine, Reemission(destination, max_events=3)) == 0

    assert await queued(engine) == [identifier(event) for event in events[3:]]
    assert await stats(engine) == (0, 0, 3)
    assert (
        "Réémission : 1 événement(s) abandonné(s), les plus anciens, au-delà des bornes de la file"
        " (1000000 événements, 2 h) : comptés perdus." in caplog.text
    )
    assert "Réémission : 2 événement(s) abandonné(s)" in caplog.text
    assert "(3 événements, 168 h)" in caplog.text


async def test_an_event_purged_before_it_left_is_counted_lost(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    event = event_of(0)
    await ingested(client, [event])
    await project_all(engine)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM event_raw"))
    catalog = FakeCatalog()
    async with serving(catalog) as base:
        sender = Sender(parse_destination(base + LINEAGE), timeouts=FAST)
        assert await drain(engine, sender) == ReemitResult(dropped=1)
        sender.close()
    assert catalog.received == []
    assert await stats(engine) == (0, 0, 1)


async def test_two_workers_never_send_an_event_twice(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    events = [event_of(index) for index in range(60)]
    await ingested(client, events)
    await project_all(engine)
    catalog = FakeCatalog(answer=Answer(delay=0.005))
    async with serving(catalog) as base:
        settings = Reemission(sender_destination(base), batch_size=7)
        stop = asyncio.Event()
        loops = [
            asyncio.create_task(
                run_reemission(
                    engine,
                    stop,
                    settings,
                    sender=Sender(settings.destination, timeouts=FAST),
                    idle_delay=0.01,
                )
            )
            for _ in range(2)
        ]
        await until(lambda: queued(engine), [])
        stop.set()
        await asyncio.wait_for(asyncio.gather(*loops), timeout=5)

    assert Counter(catalog.bodies()) == Counter(body_of(event) for event in events)
    # Les deux workers ont envoyé.
    assert len({request.peer for request in catalog.received}) >= 2
    assert await stats(engine) == (60, 0, 0)


async def test_a_destination_down_slows_neither_the_ingestion_nor_the_projection(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    # Une destination qui prend la connexion et ne répond jamais.
    catalog = FakeCatalog(answer=Answer(delay=60))
    async with serving(catalog) as base:
        stop = asyncio.Event()
        worker = asyncio.create_task(
            run_worker(
                engine,
                stop,
                idle_delay=0.01,
                reemission=Reemission(sender_destination(base)),
            )
        )
        started = time.monotonic()
        for index in range(20):
            assert (await client.post("/api/v1/lineage", json=event_of(index))).status_code == 201
        await until(lambda: scalar(engine, "SELECT count(*) FROM ingest_queue"), 0)
        assert time.monotonic() - started < 10
        assert await scalar(engine, "SELECT count(*) FROM run WHERE kind = 'tool'") == 20
        # Tout attend d'être réémis ; un envoi est en cours, que l'arrêt coupe sans attendre.
        assert len(await queued(engine)) == 20
        stopping = time.monotonic()
        stop.set()
        await asyncio.wait_for(worker, timeout=5)
        assert time.monotonic() - stopping < 3
    assert len(await queued(engine)) == 20
    assert await stats(engine) == (0, 0, 0)


async def test_neither_the_journal_nor_the_base_keep_the_address_the_header_or_any_content(
    client: httpx.AsyncClient, engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    await ingested(client, [event_of(index) for index in range(4)])
    await project_all(engine)
    catalog = FakeCatalog().then(Answer(503), Answer(422))
    async with serving(catalog) as base:
        destination = parse_destination(f"{base}/{CANARY}{LINEAGE}", f"Bearer {CANARY}-jeton")
        settings = Reemission(destination, max_events=2)
        stop = asyncio.Event()
        with caplog.at_level(logging.DEBUG):
            loop = asyncio.create_task(
                run_reemission(
                    engine,
                    stop,
                    settings,
                    sender=Sender(destination, timeouts=FAST),
                    idle_delay=0.01,
                    backoff=(0.01, 0.02),
                )
            )
            await until(lambda: queued(engine), [])
            stop.set()
            await asyncio.wait_for(loop, timeout=5)

    # Les envois sont partis, avec l'en-tête ; le débordement et le refus ont été dits.
    assert all(
        request.headers["authorization"] == f"Bearer {CANARY}-jeton" for request in catalog.received
    )
    assert "abandonné(s)" in caplog.text and "refusé(s)" in caplog.text
    assert "Réémission en échec (HTTP 503)" in caplog.text
    assert CANARY not in caplog.text
    assert base.rpartition(":")[2] not in caplog.text
    everything = await rows(
        engine,
        "SELECT CAST(row_to_json(q) AS text) FROM reemission_queue q"
        " UNION ALL SELECT CAST(row_to_json(s) AS text) FROM reemission_stats s",
    )
    assert all(CANARY not in row[0] for row in everything)


async def test_the_rebuild_does_not_send_again_what_had_already_left(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    await ingested(client, [event_of(index) for index in range(3)])
    await project_all(engine)
    catalog = FakeCatalog()
    async with serving(catalog) as base:
        sender = Sender(parse_destination(base + LINEAGE), timeouts=FAST)
        assert await drain(engine, sender) == ReemitResult(sent=3)
        # Reçu, pas encore projeté : il n'est jamais entré au graphe, il partira.
        waiting = event_of(3)
        await ingested(client, [waiting])
        report = await rebuild_graph(engine, reemit=True)
        assert report.finished
        assert await queued(engine) == [identifier(waiting)]
        assert await drain(engine, sender) == ReemitResult(sent=1)
        sender.close()
    assert len(catalog.received) == 4


async def test_an_unreachable_base_does_not_stop_the_reemission(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    lost = create_engine(f"postgresql://al:{CANARY}@127.0.0.1:{port}/al")
    stop = asyncio.Event()
    settings = Reemission(parse_destination("https://catalogue.interne/api/v1/lineage"))
    try:
        with caplog.at_level(logging.ERROR, logger="answerlineage_workers"):
            loop = asyncio.create_task(
                run_reemission(lost, stop, settings, error_delay=0.01, trim_every=timedelta(0))
            )
            deadline = time.monotonic() + 10
            while caplog.text.count("Lot de réémission non traité") < 2:
                assert not loop.done()
                assert time.monotonic() < deadline
                await asyncio.sleep(0.02)
            stop.set()
            await asyncio.wait_for(loop, timeout=5)
    finally:
        await lost.dispose()
    assert "Bornes de la file de réémission non tenues (OperationalError)" in caplog.text
    assert "Lot de réémission non traité (OperationalError)" in caplog.text
    assert CANARY not in caplog.text


async def test_the_sending_thread_of_the_reemission_says_it_reemits(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    threads: list[str] = []

    class Recorded(Sender):
        def close(self) -> None:
            threads.append(self._executor.submit(lambda: threading.current_thread().name).result())
            super().close()

    monkeypatch.setattr(reemission, "Sender", Recorded)
    stop = asyncio.Event()
    stop.set()
    await run_reemission(
        engine, stop, Reemission(parse_destination("https://catalogue.interne/api/v1/lineage"))
    )
    assert [name.rpartition("_")[0] for name in threads] == ["reemission"]


def test_the_attempts_wait_longer_and_longer_up_to_a_cap_drawn_at_random() -> None:
    random = Random(11)  # noqa: S311 — une gigue reproductible.
    delays = [retry_delay(failures, (1.0, 300.0), random) for failures in range(1, 13)]
    for failures, delay in enumerate(delays, start=1):
        nominal = min(300.0, 2.0 ** (failures - 1))
        assert nominal / 2 <= delay <= nominal
    assert max(delays) <= 300.0
    assert len(set(delays)) == len(delays)
    assert retry_delay(10_000, (1.0, 300.0), random) <= 300.0


@pytest.mark.parametrize(
    "url",
    [
        "https://169.254.169.254/latest/meta-data",
        "https://[fd00:ec2::254]/latest",
        "http://10.0.0.5/api/v1/lineage",
        "https://utilisateur:motdepasse@catalogue.interne/api/v1/lineage",
    ],
)
def test_a_forbidden_destination_stops_the_worker_at_startup_without_being_cited(
    url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started: list[object] = []

    async def serve(batch_size: int, **settings: Any) -> None:
        started.append(settings)

    monkeypatch.setattr(cli, "serve", serve)
    monkeypatch.setenv("ANSWERLINEAGE_REEMIT_AUTHORIZATION", f"Bearer {CANARY}")
    assert cli.main(["--reemit-url", url]) == 2
    monkeypatch.delenv("ANSWERLINEAGE_REEMIT_AUTHORIZATION")
    monkeypatch.setenv("ANSWERLINEAGE_REEMIT_URL", url)
    assert cli.main([]) == 2
    err = capsys.readouterr().err
    assert err.count("Configuration invalide : ") == 2
    for secret in (CANARY, "169.254", "ec2", "10.0.0.5", "motdepasse"):
        assert secret not in err
    assert started == []


def test_the_command_line_sets_the_destination_and_the_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[Any] = []

    async def serve(batch_size: int, **settings: Any) -> None:
        received.append(settings["reemission"])

    monkeypatch.setattr(cli, "serve", serve)
    monkeypatch.delenv("ANSWERLINEAGE_REEMIT_URL", raising=False)
    monkeypatch.delenv("ANSWERLINEAGE_REEMIT_AUTHORIZATION", raising=False)
    assert cli.main([]) == 0
    monkeypatch.setenv("ANSWERLINEAGE_REEMIT_URL", "http://127.0.0.1:5000/api/v1/lineage")
    monkeypatch.setenv("ANSWERLINEAGE_REEMIT_AUTHORIZATION", f"Bearer {CANARY}")
    assert cli.main(["--reemit-max-events", "5000", "--reemit-max-age-hours", "24"]) == 0
    assert received[0] is None
    settings = received[1]
    assert settings == Reemission(
        parse_destination("http://127.0.0.1:5000/api/v1/lineage", f"Bearer {CANARY}"),
        max_events=5000,
        max_age=timedelta(hours=24),
    )


@pytest.mark.parametrize(
    "options",
    [
        ["--reemit-max-events", "0"],
        ["--reemit-max-events", "100000001"],
        ["--reemit-max-age-hours", "0"],
        ["--reemit-max-age-hours", "2161"],
        ["--reemit-max-age-hours", "une"],
    ],
)
def test_the_command_line_refuses_bounds_out_of_range(
    options: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.main(options)
    assert stopped.value.code == 2
    assert "de 1 à" in capsys.readouterr().err


def test_an_authorization_header_without_a_destination_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ANSWERLINEAGE_REEMIT_URL", raising=False)
    monkeypatch.setenv("ANSWERLINEAGE_REEMIT_AUTHORIZATION", f"Bearer {CANARY}")
    assert cli.main(["rebuild"]) == 2
    err = capsys.readouterr().err
    assert "ANSWERLINEAGE_REEMIT_AUTHORIZATION" in err
    assert CANARY not in err


def sender_destination(base: str) -> Any:
    return parse_destination(base + LINEAGE)


def body_of(event: Mapping[str, Any]) -> bytes:
    return json.dumps(
        copy.deepcopy(dict(event)), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


async def until(read: Any, expected: Any, *, timeout: float = 10) -> None:
    """Attend que ``read()`` rende ``expected``."""
    deadline = time.monotonic() + timeout
    while (found := await read()) != expected:
        assert time.monotonic() < deadline, found
        await asyncio.sleep(0.02)
