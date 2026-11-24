"""Les webhooks des incidents, devant un faux récepteur qui vérifie la signature.

Un changement d'état met son message en file dans sa transaction, quand un
webhook est en service : un changement annulé n'annonce rien. Le message suit
le format publié, sans texte libre ni pseudonyme, et part signé. La file tient
dans PostgreSQL : rien ne se perd quand le récepteur tombe puis revient, un
message renvoyé garde son identifiant de livraison, deux workers n'envoient
jamais deux fois le même message, et ce qui déborde est compté et dit. Le
journal ne dit ni l'adresse, ni le secret, ni rien d'un message.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import socket
import threading
import time
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, timedelta
from pathlib import Path
from random import Random
from typing import Any
from uuid import UUID, uuid4

import jsonschema
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from answerlineage_api.incidents import quarantine
from answerlineage_api.incidents.webhooks import announce
from answerlineage_api.store.engine import create_engine
from answerlineage_workers import cli, incidents, webhooks
from answerlineage_workers.destination import Sender, Timeouts, parse_destination
from answerlineage_workers.incidents import Processed, process_analysis
from answerlineage_workers.queue import run_worker
from answerlineage_workers.webhook_signature import (
    HEADER,
    SIGNATURE_MISMATCH,
    SignatureError,
    verify,
)
from answerlineage_workers.webhooks import (
    Webhook,
    WebhookResult,
    deliver_batch,
    keep_in_service,
    run_webhooks,
    trim,
)
from workers.tests.catalog import Answer, FakeCatalog, Received, serving
from workers.tests.test_incidents import RATES, answering, declared, reading, record

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

CANARY = "canari7c2d"
PATH = "/recepteur"
FAST = Timeouts(connect=1.0, io=1.0, request=1.0)
SCHEMA: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[2] / "spec" / "webhooks" / "v1" / "Message.json").read_text(
        encoding="utf-8"
    )
)


def shared_secret() -> str:
    """Un secret tiré pour le test : aucun secret n'est écrit dans le dépôt."""
    return secrets.token_urlsafe(32)


def webhook_to(base: str, secret: str, **bounds: Any) -> Webhook:
    return Webhook(parse_destination(base + PATH), secret, **bounds)


def offline(secret: str | None = None, **bounds: Any) -> Webhook:
    """Un webhook vers un récepteur que les tests ne joignent pas."""
    return Webhook(
        parse_destination("https://recepteur.interne/webhooks"),
        secret or shared_secret(),
        **bounds,
    )


async def rows(engine: AsyncEngine, sql: str, **params: Any) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(text(sql), params)).all())


async def queued(engine: AsyncEngine) -> list[tuple[str, UUID]]:
    found = await rows(engine, "SELECT event_type, incident_id FROM webhook_queue ORDER BY id")
    return [(row.event_type, row.incident_id) for row in found]


async def stats(engine: AsyncEngine) -> tuple[int, int, int]:
    found = await rows(engine, "SELECT sent, rejected, dropped FROM webhook_stats")
    return (0, 0, 0) if not found else tuple(found[0])


def checked(request: Received, *secrets_known: str) -> dict[str, Any]:
    """Le message reçu, une fois sa signature vérifiée par la fonction de référence."""
    verify(request.headers[HEADER.lower()], request.body, list(secrets_known))
    document: dict[str, Any] = json.loads(request.body)
    jsonschema.Draft202012Validator(SCHEMA).validate(document)
    return document


async def drain(engine: AsyncEngine, sender: Sender, webhook: Webhook) -> WebhookResult:
    sent = rejected = 0
    while not (result := await deliver_batch(engine, sender, webhook)).empty:
        assert result.failure is None, result.failure
        sent, rejected = sent + result.sent, rejected + result.rejected
    return WebhookResult(sent=sent, rejected=rejected)


async def until(read: Any, expected: Any, *, timeout: float = 10) -> None:
    """Attend que ``read()`` rende ``expected``."""
    deadline = time.monotonic() + timeout
    while (found := await read()) != expected:
        assert time.monotonic() < deadline, found
        await asyncio.sleep(0.02)


async def test_without_a_webhook_in_service_no_change_of_state_is_queued(
    engine: AsyncEngine,
) -> None:
    # Aucun worker n'a jamais eu de webhook.
    first = await declared(engine, RATES)
    assert await process_analysis(engine) == Processed(first, "done")
    assert await queued(engine) == []

    webhook = offline(max_age=timedelta(hours=2))
    await keep_in_service(engine, webhook)
    (until_then,) = await rows(engine, "SELECT in_service_until - now() AS left FROM webhook_stats")
    assert timedelta(hours=1, minutes=59) < until_then.left <= timedelta(hours=2)
    second = await declared(engine, RATES)
    assert await queued(engine) == [("incident.declared", second)]

    # La borne passée sans qu'un worker à webhook repasse : il n'est plus en service.
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE webhook_stats SET in_service_until = now()"))
    third = await declared(engine, RATES)
    assert await queued(engine) == [("incident.declared", second)]
    assert third not in {incident for _, incident in await queued(engine)}


async def test_a_change_of_state_that_is_rolled_back_announces_nothing(
    engine: AsyncEngine,
) -> None:
    await keep_in_service(engine, offline())
    incident_id = await declared(engine, RATES)

    class AbandonedError(Exception):
        pass

    # La transaction de la déclaration est annulée après la mise en file.
    with pytest.raises(AbandonedError):
        async with engine.begin() as conn:
            assert await announce(conn, incident_id, "incident.declared")
            assert len(await queued_in(conn)) == 2
            raise AbandonedError
    assert await queued(engine) == [("incident.declared", incident_id)]

    # Une analyse qu'un worker arrêté n'a pas validée n'annonce rien non plus.
    class Stopped(BaseException):
        pass

    async def interrupted(conn: AsyncConnection, *_: Any) -> None:
        raise Stopped

    with pytest.raises(Stopped):
        await process_analysis(engine, analyze=interrupted)
    assert await queued(engine) == [("incident.declared", incident_id)]


async def queued_in(conn: AsyncConnection) -> list[Any]:
    return list((await conn.execute(text("SELECT id FROM webhook_queue"))).all())


async def test_each_change_of_state_is_queued_in_its_transaction_as_it_was_then(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    await keep_in_service(engine, offline())
    agent = uuid4()
    await record(engine, answering(agent, 1, 3.5, ("p-1", "p-2")), reading(agent, 3, RATES))
    done = await declared(engine, RATES)
    assert await process_analysis(engine) == Processed(done, "done")

    failing = await declared(engine, RATES)

    async def broken(conn: AsyncConnection, *_: Any) -> None:
        raise RuntimeError(CANARY)

    monkeypatch.setattr(incidents, "MAX_ATTEMPTS", 2)
    # Remise à plus tard : l'analyse ne change pas d'état, rien n'est annoncé.
    assert await process_analysis(engine, analyze=broken) == Processed(failing, "postponed")
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE incident_analysis SET available_at = now()"))
    assert await process_analysis(engine, analyze=broken) == Processed(failing, "failed")

    found = await rows(
        engine,
        """
        SELECT q.event_type, q.incident_id, q.incident_state, q.severity, q.analysis_state,
               q.analysis_error, q.answers, q.recipients, q.lower_bound, q.attempts,
               q.occurred_at = CASE WHEN q.event_type = 'incident.declared' THEN i.declared_at
                                    ELSE s.finished_at END AS at_the_change
        FROM webhook_queue q
        JOIN incident i ON i.id = q.incident_id
        JOIN incident_analysis s ON s.incident_id = q.incident_id
        ORDER BY q.id
        """,
    )
    assert [tuple(row) for row in found] == [
        (
            "incident.declared",
            done,
            "qualified",
            "critical",
            "pending",
            None,
            None,
            None,
            None,
            0,
            True,
        ),
        ("incident.analysed", done, "qualified", "critical", "done", None, 1, 2, False, 0, True),
        (
            "incident.declared",
            failing,
            "qualified",
            "critical",
            "pending",
            None,
            None,
            None,
            None,
            0,
            True,
        ),
        (
            "incident.analysis_failed",
            failing,
            "qualified",
            "critical",
            "failed",
            "analysis_error",
            None,
            None,
            None,
            0,
            True,
        ),
    ]
    identifiers = await rows(engine, "SELECT delivery_id FROM webhook_queue")
    assert len({row.delivery_id for row in identifiers}) == 4


async def test_the_message_follows_the_published_format_signed_without_any_content(
    engine: AsyncEngine,
) -> None:
    secret = shared_secret()
    receiver = FakeCatalog()
    async with serving(receiver) as base:
        webhook = webhook_to(base, secret)
        await keep_in_service(engine, webhook)
        agent = uuid4()
        await record(
            engine,
            answering(agent, 1, 3.5, (f"{CANARY}-a", f"{CANARY}-b")),
            reading(agent, 3, RATES),
        )
        incident_id = await declared(engine, RATES)
        assert await process_analysis(engine) == Processed(incident_id, "done")
        sender = Sender(webhook.destination, timeouts=FAST)
        before = int(time.time())
        assert await drain(engine, sender, webhook) == WebhookResult(sent=2)
        sender.close()

    declared_message, analysed_message = (checked(request, secret) for request in receiver.received)
    assert {request.path for request in receiver.received} == {PATH}
    assert all(
        request.headers["content-type"] == "application/json" for request in receiver.received
    )
    assert declared_message["type"] == "incident.declared"
    assert declared_message["incident"] == {
        "id": str(incident_id),
        "state": "qualified",
        "severity": "critical",
        "analysis": {"state": "pending", "error": None, "counts": None},
        "quarantine": None,
    }
    assert analysed_message["type"] == "incident.analysed"
    assert analysed_message["incident"]["analysis"] == {
        "state": "done",
        "error": None,
        "counts": {"answers": 1, "recipients": 2, "lower_bound": False},
    }
    for request, document in zip(
        receiver.received, (declared_message, analysed_message), strict=True
    ):
        timestamp = int(request.headers[HEADER.lower()].split(",")[0].removeprefix("t="))
        assert before <= timestamp <= time.time()
        assert document["emitted_at"] == time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))
        # Les destinataires sont comptés, jamais nommés ; aucun nom de jeu de données.
        assert CANARY.encode() not in request.body
        assert b"taux_change" not in request.body
    assert declared_message["delivery_id"] != analysed_message["delivery_id"]
    assert await queued(engine) == []
    assert await stats(engine) == (2, 0, 0)


async def test_the_quarantine_and_its_lift_leave_in_the_published_format_without_the_scope(
    engine: AsyncEngine,
) -> None:
    secret = shared_secret()
    receiver = FakeCatalog()
    async with serving(receiver) as base:
        webhook = webhook_to(base, secret)
        await keep_in_service(engine, webhook)
        incident_id = await declared(engine, RATES)
        async with engine.begin() as conn:
            assert await quarantine.activate(conn, incident_id, "warn") is not None
        assert await process_analysis(engine) == Processed(incident_id, "done")
        async with engine.begin() as conn:
            assert await quarantine.lift(conn, incident_id) is not None
        sender = Sender(webhook.destination, timeouts=FAST)
        assert await drain(engine, sender, webhook) == WebhookResult(sent=4)
        sender.close()

    messages = [checked(request, secret) for request in receiver.received]
    assert [(message["type"], message["incident"]["quarantine"]) for message in messages] == [
        ("incident.declared", None),
        ("incident.quarantined", {"state": "active", "mode": "warn"}),
        # L'analyse faite pendant la quarantaine le dit, telle qu'elle était alors.
        ("incident.analysed", {"state": "active", "mode": "warn"}),
        ("incident.quarantine_lifted", {"state": "lifted", "mode": "warn"}),
    ]
    activated, lifted = (
        await rows(engine, "SELECT activated_at, lifted_at FROM incident_quarantine")
    )[0]
    assert messages[1]["occurred_at"] == activated.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    assert messages[3]["occurred_at"] == lifted.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    # Le périmètre ne part pas : ni nom, ni espace de noms.
    for request in receiver.received:
        assert RATES.name.encode() not in request.body
        assert RATES.namespace.encode() not in request.body


async def test_a_receiver_down_then_back_loses_nothing_and_keeps_the_delivery_id(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    secret = shared_secret()
    await keep_in_service(engine, offline())
    incident_ids = [await declared(engine, RATES) for _ in range(3)]
    receiver = FakeCatalog().then(Answer(503), Answer(503), Answer(503))
    async with serving(receiver) as base:
        webhook = webhook_to(base, secret)
        sender = Sender(webhook.destination, timeouts=FAST)
        # En panne : le lot s'arrête au premier échec, qui compte un essai à ce message seul.
        assert await deliver_batch(engine, sender, webhook) == WebhookResult(failure="HTTP 503")
        waiting = await rows(engine, "SELECT attempts, last_error FROM webhook_queue ORDER BY id")
        assert [tuple(row) for row in waiting] == [(1, "HTTP 503"), (0, None), (0, None)]

        stop = asyncio.Event()
        with caplog.at_level(logging.INFO, logger="answerlineage_workers"):
            loop = asyncio.create_task(
                run_webhooks(
                    engine,
                    stop,
                    webhook,
                    sender=sender,
                    idle_delay=0.01,
                    backoff=(0.01, 0.05),
                    random=Random(7),  # noqa: S311 — une gigue reproductible.
                )
            )
            await until(lambda: queued(engine), [])
            stop.set()
            await asyncio.wait_for(loop, timeout=5)

    documents = [checked(request, secret) for request in receiver.received]
    assert len(documents) == 3 + 3
    # Le premier message, renvoyé trois fois puis remis : un seul identifiant de livraison.
    assert len({document["delivery_id"] for document in documents[:4]}) == 1
    assert [document["incident"]["id"] for document in documents[3:]] == [
        str(incident_id) for incident_id in incident_ids
    ]
    assert len({document["delivery_id"] for document in documents}) == 3
    assert await stats(engine) == (3, 0, 0)
    assert caplog.text.count("Webhook en échec (HTTP 503)") == 1
    assert "Webhook rétabli après 2 échec(s)" in caplog.text


async def test_an_uncertain_delivery_is_sent_again_with_the_same_delivery_id(
    engine: AsyncEngine,
) -> None:
    secret = shared_secret()
    await keep_in_service(engine, offline())
    await declared(engine, RATES)
    # Le récepteur prend le message, mais sa réponse arrive après le délai.
    receiver = FakeCatalog().then(Answer(delay=3))
    async with serving(receiver) as base:
        webhook = webhook_to(base, secret)
        sender = Sender(webhook.destination, timeouts=Timeouts(1.0, 1.0, 0.5))
        assert await deliver_batch(engine, sender, webhook) == WebhookResult(failure="TimeoutError")
        assert await drain(engine, sender, webhook) == WebhookResult(sent=1)
        sender.close()
    first, second = (checked(request, secret) for request in receiver.received)
    # Au moins une fois : reçu deux fois, sous le même identifiant, compté remis une fois.
    assert first["delivery_id"] == second["delivery_id"]
    assert await stats(engine) == (1, 0, 0)


async def test_a_message_the_receiver_refuses_is_dropped_and_counted_lost(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    await keep_in_service(engine, offline())
    for _ in range(3):
        await declared(engine, RATES)
    receiver = FakeCatalog().then(Answer(422))
    async with serving(receiver) as base:
        webhook = webhook_to(base, shared_secret())
        sender = Sender(webhook.destination, timeouts=FAST)
        with caplog.at_level(logging.WARNING, logger="answerlineage_workers"):
            assert await deliver_batch(engine, sender, webhook) == WebhookResult(sent=2, rejected=1)
        sender.close()
    assert await queued(engine) == []
    assert await stats(engine) == (2, 1, 0)
    assert (
        "Webhook : 1 message(s) refusé(s) par le récepteur (HTTP 422) : abandonnés, comptés"
        " perdus." in caplog.text
    )


async def test_the_overflow_of_the_queue_is_counted_and_said(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    await keep_in_service(engine, offline())
    incident_ids = [await declared(engine, RATES) for _ in range(6)]
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE webhook_queue SET enqueued_at = now() - interval '3 hours'"
                " WHERE incident_id = :id"
            ),
            {"id": incident_ids[0]},
        )
    with caplog.at_level(logging.WARNING, logger="answerlineage_workers"):
        # Trop ancien : le premier ; puis au-delà de trois messages, les plus anciens.
        assert await trim(engine, offline(max_age=timedelta(hours=2))) == 1
        assert await trim(engine, offline(max_messages=3)) == 2
        assert await trim(engine, offline(max_messages=3)) == 0
    assert await queued(engine) == [
        ("incident.declared", incident_id) for incident_id in incident_ids[3:]
    ]
    assert await stats(engine) == (0, 0, 3)
    assert (
        "Webhook : 1 message(s) abandonné(s), les plus anciens, au-delà des bornes de la file"
        " (10000 messages, 2 h) : comptés perdus." in caplog.text
    )
    assert "Webhook : 2 message(s) abandonné(s)" in caplog.text
    assert "(3 messages, 168 h)" in caplog.text


async def test_the_sending_thread_of_the_webhooks_says_it_sends_webhooks(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    threads: list[str] = []

    class Recorded(Sender):
        def close(self) -> None:
            threads.append(self._executor.submit(lambda: threading.current_thread().name).result())
            super().close()

    monkeypatch.setattr(webhooks, "Sender", Recorded)
    stop = asyncio.Event()
    stop.set()
    await run_webhooks(engine, stop, offline())
    assert [name.rpartition("_")[0] for name in threads] == ["webhook"]


async def test_two_workers_never_send_a_message_twice(engine: AsyncEngine) -> None:
    secret = shared_secret()
    await keep_in_service(engine, offline())
    for _ in range(40):
        await declared(engine, RATES)
    receiver = FakeCatalog(answer=Answer(delay=0.005))
    async with serving(receiver) as base:
        webhook = webhook_to(base, secret, batch_size=7)
        stop = asyncio.Event()
        loops = [
            asyncio.create_task(
                run_webhooks(
                    engine,
                    stop,
                    webhook,
                    sender=Sender(webhook.destination, timeouts=FAST),
                    idle_delay=0.01,
                )
            )
            for _ in range(2)
        ]
        await until(lambda: queued(engine), [])
        stop.set()
        await asyncio.wait_for(asyncio.gather(*loops), timeout=5)

    delivered = Counter(checked(request, secret)["delivery_id"] for request in receiver.received)
    assert len(delivered) == 40
    assert set(delivered.values()) == {1}
    # Les deux workers ont envoyé.
    assert len({request.peer for request in receiver.received}) >= 2
    assert await stats(engine) == (40, 0, 0)


async def test_a_receiver_that_does_not_answer_slows_neither_the_projection_nor_the_analysis(
    engine: AsyncEngine,
) -> None:
    # Un récepteur qui prend la connexion et ne répond jamais.
    receiver = FakeCatalog(answer=Answer(delay=60))
    async with serving(receiver) as base:
        stop = asyncio.Event()
        worker = asyncio.create_task(
            run_worker(engine, stop, idle_delay=0.01, webhook=webhook_to(base, shared_secret()))
        )
        await until(lambda: rows(engine, "SELECT 1 FROM webhook_stats"), [(1,)])
        started = time.monotonic()
        incident_id = await declared(engine, RATES)
        await until(
            lambda: rows(
                engine,
                "SELECT state FROM incident_analysis WHERE incident_id = :id",
                id=incident_id,
            ),
            [("done",)],
        )
        assert time.monotonic() - started < 10
        # Les deux annonces attendent ; un envoi est en cours, que l'arrêt coupe sans attendre.
        await until(
            lambda: queued(engine),
            [("incident.declared", incident_id), ("incident.analysed", incident_id)],
        )
        stopping = time.monotonic()
        stop.set()
        await asyncio.wait_for(worker, timeout=5)
        assert time.monotonic() - stopping < 3
    assert len(await queued(engine)) == 2
    assert await stats(engine) == (0, 0, 0)


async def test_neither_the_journal_nor_the_base_keep_the_address_the_secret_or_any_content(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    secret = f"{CANARY}-{shared_secret()}"
    previous = f"{CANARY}-precedent-{shared_secret()}"
    await keep_in_service(engine, offline())
    for _ in range(4):
        await declared(engine, RATES)
    receiver = FakeCatalog().then(Answer(503), Answer(422))
    async with serving(receiver) as base:
        webhook = Webhook(
            parse_destination(f"{base}/{CANARY}{PATH}"),
            secret,
            previous_secret=previous,
            max_messages=2,
        )
        stop = asyncio.Event()
        with caplog.at_level(logging.DEBUG):
            loop = asyncio.create_task(
                run_webhooks(
                    engine,
                    stop,
                    webhook,
                    sender=Sender(webhook.destination, timeouts=FAST),
                    idle_delay=0.01,
                    backoff=(0.01, 0.02),
                )
            )
            await until(lambda: queued(engine), [])
            stop.set()
            await asyncio.wait_for(loop, timeout=5)

    # Les messages sont partis, signés sous les deux secrets ; le débordement et le refus dits.
    for request in receiver.received:
        checked(request, secret)
        checked(request, previous)
        assert CANARY.encode() not in request.body
    assert "abandonné(s)" in caplog.text and "refusé(s)" in caplog.text
    assert "Webhook en échec (HTTP 503)" in caplog.text
    assert "signés sous 2 secret(s)" in caplog.text
    assert CANARY not in caplog.text
    assert base.rpartition(":")[2] not in caplog.text
    assert secret not in repr(webhook) and previous not in repr(webhook)
    everything = await rows(
        engine,
        "SELECT CAST(row_to_json(q) AS text) FROM webhook_queue q"
        " UNION ALL SELECT CAST(row_to_json(s) AS text) FROM webhook_stats s",
    )
    assert all(CANARY not in row[0] for row in everything)


async def test_a_message_signed_under_another_secret_is_refused_by_the_receiver(
    engine: AsyncEngine,
) -> None:
    await keep_in_service(engine, offline())
    await declared(engine, RATES)
    receiver = FakeCatalog()
    async with serving(receiver) as base:
        webhook = webhook_to(base, shared_secret())
        sender = Sender(webhook.destination, timeouts=FAST)
        assert await drain(engine, sender, webhook) == WebhookResult(sent=1)
        sender.close()
    (request,) = receiver.received
    with pytest.raises(SignatureError) as refused:
        checked(request, shared_secret())
    assert refused.value.code == SIGNATURE_MISMATCH


async def test_an_unreachable_base_does_not_stop_the_webhooks(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    lost = create_engine(f"postgresql://al:{CANARY}@127.0.0.1:{port}/al")
    stop = asyncio.Event()
    try:
        with caplog.at_level(logging.ERROR, logger="answerlineage_workers"):
            loop = asyncio.create_task(
                run_webhooks(lost, stop, offline(), error_delay=0.01, trim_every=timedelta(0))
            )
            deadline = time.monotonic() + 10
            while caplog.text.count("Lot de webhooks non traité") < 2:
                assert not loop.done()
                assert time.monotonic() < deadline
                await asyncio.sleep(0.02)
            stop.set()
            await asyncio.wait_for(loop, timeout=5)
    finally:
        await lost.dispose()
    assert (
        "Mise en service ou bornes de la file des webhooks non tenues (OperationalError)"
        in caplog.text
    )
    assert "Lot de webhooks non traité (OperationalError)" in caplog.text
    assert CANARY not in caplog.text


# Le réglage, par la ligne de commande du worker.


def started_with(monkeypatch: pytest.MonkeyPatch) -> list[Mapping[str, Any]]:
    started: list[Mapping[str, Any]] = []

    async def serve(batch_size: int, **settings: Any) -> None:
        started.append(settings)

    monkeypatch.setattr(cli, "serve", serve)
    for variable in (
        "ANSWERLINEAGE_WEBHOOK_URL",
        "ANSWERLINEAGE_WEBHOOK_SECRET",
        "ANSWERLINEAGE_WEBHOOK_PREVIOUS_SECRET",
        "ANSWERLINEAGE_REEMIT_URL",
        "ANSWERLINEAGE_REEMIT_AUTHORIZATION",
    ):
        monkeypatch.delenv(variable, raising=False)
    return started


def test_the_command_line_sets_the_webhook_its_secrets_and_its_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = started_with(monkeypatch)
    assert cli.main([]) == 0
    secret, previous = shared_secret(), shared_secret()
    monkeypatch.setenv("ANSWERLINEAGE_WEBHOOK_URL", "http://127.0.0.1:5000/webhooks")
    monkeypatch.setenv("ANSWERLINEAGE_WEBHOOK_SECRET", secret)
    assert cli.main(["--webhook-max-messages", "500", "--webhook-max-age-hours", "24"]) == 0
    monkeypatch.setenv("ANSWERLINEAGE_WEBHOOK_PREVIOUS_SECRET", previous)
    assert cli.main(["--webhook-url", "https://recepteur.interne/hooks"]) == 0

    assert started[0]["webhook"] is None
    assert started[1]["webhook"] == Webhook(
        parse_destination("http://127.0.0.1:5000/webhooks"),
        secret,
        max_messages=500,
        max_age=timedelta(hours=24),
    )
    rotating = started[2]["webhook"]
    assert rotating.destination == parse_destination("https://recepteur.interne/hooks")
    assert rotating.secrets == (secret, previous)


@pytest.mark.parametrize(
    ("variables", "named"),
    [
        ({"ANSWERLINEAGE_WEBHOOK_URL": "https://recepteur.interne/hooks"}, "SECRET"),
        (
            {
                "ANSWERLINEAGE_WEBHOOK_URL": "https://recepteur.interne/hooks",
                "ANSWERLINEAGE_WEBHOOK_SECRET": f"{CANARY}-court",
            },
            "ANSWERLINEAGE_WEBHOOK_SECRET",
        ),
        (
            {
                "ANSWERLINEAGE_WEBHOOK_URL": "https://recepteur.interne/hooks",
                "ANSWERLINEAGE_WEBHOOK_SECRET": f"{CANARY} avec des espaces dans le secret",
            },
            "ANSWERLINEAGE_WEBHOOK_SECRET",
        ),
        (
            {
                "ANSWERLINEAGE_WEBHOOK_URL": "https://recepteur.interne/hooks",
                "ANSWERLINEAGE_WEBHOOK_SECRET": f"{CANARY}-" + "x" * 40,
                "ANSWERLINEAGE_WEBHOOK_PREVIOUS_SECRET": f"{CANARY}-court",
            },
            "ANSWERLINEAGE_WEBHOOK_PREVIOUS_SECRET",
        ),
        ({"ANSWERLINEAGE_WEBHOOK_SECRET": f"{CANARY}-" + "x" * 40}, "sans webhook"),
        ({"ANSWERLINEAGE_WEBHOOK_PREVIOUS_SECRET": f"{CANARY}-" + "x" * 40}, "sans webhook"),
        (
            {
                "ANSWERLINEAGE_WEBHOOK_URL": f"https://169.254.169.254/{CANARY}",
                "ANSWERLINEAGE_WEBHOOK_SECRET": f"{CANARY}-" + "x" * 40,
            },
            "webhook : ",
        ),
        (
            {
                "ANSWERLINEAGE_WEBHOOK_URL": f"http://10.0.0.5/{CANARY}",
                "ANSWERLINEAGE_WEBHOOK_SECRET": f"{CANARY}-" + "x" * 40,
            },
            "webhook : ",
        ),
    ],
)
def test_a_refused_webhook_stops_the_worker_at_startup_without_citing_its_address_or_secret(
    variables: dict[str, str],
    named: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    started = started_with(monkeypatch)
    for variable, value in variables.items():
        monkeypatch.setenv(variable, value)
    assert cli.main([]) == 2
    err = capsys.readouterr().err
    assert err.startswith("Configuration invalide : ")
    assert named in err
    for secret in (CANARY, "169.254", "10.0.0.5"):
        assert secret not in err
    assert started == []


@pytest.mark.parametrize(
    "options",
    [
        ["--webhook-max-messages", "0"],
        ["--webhook-max-messages", "1000001"],
        ["--webhook-max-age-hours", "0"],
        ["--webhook-max-age-hours", "2161"],
        ["--webhook-max-age-hours", "une"],
    ],
)
def test_the_command_line_refuses_webhook_bounds_out_of_range(
    options: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.main(options)
    assert stopped.value.code == 2
    assert "de 1 à" in capsys.readouterr().err


@pytest.mark.parametrize(
    "bounds",
    [
        {"max_messages": 0},
        {"max_age": timedelta(0)},
        {"max_age": timedelta(days=91)},
        {"batch_size": 0},
        {"previous_secret": "court"},
    ],
)
def test_a_webhook_out_of_its_bounds_is_refused(bounds: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r"de 1 à|d'une seconde|de 32 à"):
        offline(**bounds)
