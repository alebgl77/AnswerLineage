"""La boîte d'envoi du proxy : bornée, asynchrone, et chaque perte dite.

L'API d'ingestion est simulée ici par un transport HTTP en mémoire, réglé
réponse par réponse ; les tests d'intégration (``tests/test_outbox_chaos.py``)
la remplacent par la vraie, sur un vrai PostgreSQL.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import socket
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from random import Random
from typing import Any

import httpx
import pytest

from answerlineage_emitter import outbox as outbox_module
from answerlineage_proxy import cli
from answerlineage_proxy.app import McpProxy
from answerlineage_proxy.config import ProxyConfigurationError, ProxySettings
from answerlineage_proxy.fingerprint import FingerprintKey
from answerlineage_proxy.outbox import (
    BATCH_BYTES,
    BATCH_EVENTS,
    DEFAULT_FLUSH_SECONDS,
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_EVENTS,
    RETRY_AFTER_CAP,
    Outbox,
    OutboxSettings,
    retry_delay,
)
from answerlineage_proxy.sink import DeferredSink, MemorySink, NullSink, receipt
from proxy.tests.conftest import TOOL_RESULT, FakeMcpServer, exchange, running, tool_call

pytestmark = pytest.mark.anyio

API = "http://ingestion.test"
CANARY = "canari-boite-9c41"


def event(rank: int, *, size: int = 0) -> dict[str, Any]:
    """Un événement de forme réaliste ; ``size`` l'alourdit d'une facette de remplissage."""
    moment = datetime(2026, 11, 11, 9, 0, tzinfo=UTC) + timedelta(milliseconds=rank)
    body: dict[str, Any] = {
        "eventType": "COMPLETE",
        "eventTime": moment.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "run": {"runId": f"00000000-0000-8000-8000-{rank:012d}", "facets": {}},
        "job": {"namespace": "mcp://meteo", "name": "get_weather"},
        "producer": "https://example.invalid/proxy",
        "schemaURL": "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent",
    }
    if size:
        body["run"]["facets"]["remplissage"] = {"x": "x" * size}
    return body


def report(start: str = "09:00", *, lost: int = 0, emitted: int = 0) -> dict[str, Any]:
    return {
        "emitter": "mcp://meteo",
        "window": {
            "from": f"2026-11-11T{start}:00.000000Z",
            "to": f"2026-11-11T{start}:59.000000Z",
        },
        "calls_seen": 10,
        "events_emitted": emitted,
        "events_lost": lost,
        "uninspected": 0,
    }


@dataclass
class Posted:
    path: str
    headers: httpx.Headers
    body: Any
    at: float


Reply = httpx.Response | BaseException | Callable[[list[Any]], Awaitable[httpx.Response]]


@dataclass
class FakeApi:
    """L'API d'ingestion simulée : elle enregistre chaque envoi et rend la réponse prévue.

    Par défaut, un lot est accepté en entier et un relevé enregistré. ``replies``
    donne, dans l'ordre, les réponses des prochains envois de lots ; ``report_replies``
    celles des prochains relevés.
    """

    posted: list[Posted] = field(default_factory=list)
    replies: list[Reply] = field(default_factory=list)
    report_replies: list[Reply] = field(default_factory=list)

    @property
    def batches(self) -> list[list[Any]]:
        return [item.body for item in self.posted if item.path.endswith("/lineage/batch")]

    @property
    def reports(self) -> list[Any]:
        return [item.body for item in self.posted if item.path.endswith("/coverage")]

    @property
    def delivered(self) -> list[Any]:
        return [entry for batch in self.batches for entry in batch]

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        content = await request.aread()
        if request.headers.get("content-encoding") == "gzip":
            content = gzip.decompress(content)
        body = json.loads(content)
        self.posted.append(Posted(request.url.path, request.headers, body, time.monotonic()))
        pending = self.report_replies if request.url.path.endswith("/coverage") else self.replies
        if pending:
            reply = pending.pop(0)
            if isinstance(reply, BaseException):
                raise reply
            if callable(reply):
                return await reply(body)
            return reply
        if request.url.path.endswith("/coverage"):
            return httpx.Response(201, json=body)
        return httpx.Response(202, json={"accepted": len(body), "duplicates": 0, "rejected": []})


@dataclass
class Outcomes:
    """Ce que la boîte dit de chaque événement : remis ou perdu, par reçu."""

    seen: list[tuple[str, bool]] = field(default_factory=list)

    def __call__(self, receipt: str, delivered: bool) -> None:
        self.seen.append((receipt, delivered))

    @property
    def delivered(self) -> list[str]:
        return [receipt for receipt, delivered in self.seen if delivered]

    @property
    def lost(self) -> list[str]:
        return [receipt for receipt, delivered in self.seen if not delivered]

    def counted(self, count: int) -> Callable[[], bool]:
        return lambda: len(self.seen) == count


def settings(**changes: Any) -> OutboxSettings:
    return OutboxSettings.create(API, **changes)


def settings_for(url: str, **changes: Any) -> OutboxSettings:
    return OutboxSettings.create(url, **changes)


@asynccontextmanager
async def outbox(
    api: FakeApi | None = None, *, start: bool = True, **options: Any
) -> AsyncIterator[tuple[Outbox, FakeApi, Outcomes]]:
    api = api or FakeApi()
    box_settings = options.pop("box_settings", None) or settings()
    client = httpx.AsyncClient(transport=httpx.MockTransport(api))
    options.setdefault("backoff", (0.01, 0.05))
    options.setdefault("random", Random(7))  # noqa: S311 — une gigue reproductible.
    box = Outbox(box_settings, client=client, **options)
    outcomes = Outcomes()
    box.watch(outcomes)
    if start:
        await box.start()
    try:
        yield box, api, outcomes
    finally:
        await box.aclose()
        await client.aclose()


async def until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition jamais remplie")
        await asyncio.sleep(0.005)


def times(events: list[Any]) -> list[str]:
    return [item["eventTime"] for item in events]


# Prise en charge : un ajout, rien de plus.


async def test_emit_only_appends_the_event_and_waits_for_nothing() -> None:
    async with outbox(start=False, box_settings=settings(flush_seconds=0)) as (box, api, _):
        started = time.perf_counter()
        for rank in range(1_000):
            box.emit(event(rank))
        elapsed = time.perf_counter() - started
        assert box.queued == 1_000
        assert api.posted == []
        # Mille prises en charge, sans entrée-sortie ni attente : quelques millisecondes.
        assert elapsed < 0.5


async def test_the_outbox_is_a_deferred_sink_and_the_others_are_not() -> None:
    box = Outbox(settings(flush_seconds=0))
    assert isinstance(box, DeferredSink)
    assert not isinstance(MemorySink(), DeferredSink)
    assert not isinstance(NullSink(), DeferredSink)
    # Sans personne pour l'écouter, la boîte compte ses pertes sans les dire.
    box.emit(event(0))
    await box.aclose()
    assert box.stats.dropped_shutdown == 1


def test_the_receipt_of_an_event_is_its_time() -> None:
    assert receipt(event(3)) == "2026-11-11T09:00:00.003000Z"
    assert receipt({"eventTime": 3}) == ""
    assert receipt({}) == ""


# Envoi : par lots bornés, compressés, à la route des lots.


async def test_events_leave_by_batches_to_the_batch_route_compressed() -> None:
    async with outbox() as (box, api, outcomes):
        for rank in range(1_200):
            box.emit(event(rank))
        await until(lambda: len(outcomes.seen) == 1_200)

        assert [len(batch) for batch in api.batches] == [500, 500, 200]
        assert all(item.path == "/api/v1/lineage/batch" for item in api.posted)
        assert {item.headers["content-encoding"] for item in api.posted} == {"gzip"}
        assert {item.headers["content-type"] for item in api.posted} == {"application/json"}
        # Dans l'ordre de prise en charge, chacun une fois, et dit remis.
        assert times(api.delivered) == [receipt(event(rank)) for rank in range(1_200)]
        assert outcomes.delivered == times(api.delivered)
        assert box.stats.sent == 1_200
        assert box.queued == 0


async def test_a_batch_weighs_at_most_one_mebibyte_before_compression() -> None:
    async with outbox() as (box, api, outcomes):
        for rank in range(12):
            box.emit(event(rank, size=200_000))
        await until(lambda: len(outcomes.seen) == 12)
    sizes = [len(json.dumps(batch, separators=(",", ":")).encode()) for batch in api.batches]
    assert len(api.batches) >= 3
    assert all(size <= BATCH_BYTES for size in sizes)
    assert sum(len(batch) for batch in api.batches) == 12


async def test_an_event_heavier_than_a_batch_leaves_alone() -> None:
    async with outbox() as (box, api, outcomes):
        box.emit(event(0))
        box.emit(event(1, size=BATCH_BYTES + 10))
        box.emit(event(2))
        await until(lambda: len(outcomes.seen) == 3)
    assert outcomes.lost == []
    assert [len(batch) for batch in api.batches] in ([1, 1, 1], [1, 2])


# Bornes : pleine, la boîte abandonne le plus ancien, et le dit.


async def test_a_full_outbox_drops_the_oldest_event_and_counts_it() -> None:
    box_settings = settings(max_events=3)
    async with outbox(start=False, box_settings=box_settings) as (box, api, outcomes):
        for rank in range(5):
            box.emit(event(rank))
        assert box.queued == 3
        assert box.stats.dropped_overflow == 2
        assert outcomes.lost == [receipt(event(0)), receipt(event(1))]

        await box.start()
        await until(lambda: len(outcomes.seen) == 5)
        assert times(api.delivered) == [receipt(event(rank)) for rank in (2, 3, 4)]


async def test_the_outbox_holds_at_most_its_bytes_and_drops_the_oldest_beyond() -> None:
    box_settings = settings(max_bytes=1024 * 1024, flush_seconds=0.1)
    api = FakeApi(replies=[httpx.ConnectError("injoignable")] * 1_000)
    async with outbox(api, box_settings=box_settings, backoff=(5, 5)) as (box, _, outcomes):
        for rank in range(15):
            box.emit(event(rank, size=100_000))
        await until(lambda: box.stats.dropped_overflow >= 5)
        assert box.queued_bytes <= 1024 * 1024
        # Les plus anciens sont partis les premiers ; le lot en cours d'envoi n'est pas compté.
        assert outcomes.lost == [receipt(event(rank)) for rank in range(len(outcomes.lost))]


async def test_an_event_that_cannot_be_serialized_is_rejected_and_never_sent() -> None:
    async with outbox() as (box, api, outcomes):
        broken = event(0)
        broken["run"]["facets"]["mesure"] = {"latence": float("nan")}
        box.emit(broken)
        box.emit(event(1))
        await until(lambda: len(outcomes.seen) == 2)
    assert outcomes.lost == [receipt(broken)]
    assert box.stats.rejected == 1
    assert times(api.delivered) == [receipt(event(1))]


# Réponses de l'API.


def accepted(count: int, rejected: list[int]) -> httpx.Response:
    return httpx.Response(
        202,
        json={
            "accepted": count - len(rejected),
            "duplicates": 0,
            "rejected": [
                {"index": index, "errors": [{"path": "/run", "type": "x", "message": "m"}]}
                for index in rejected
            ],
        },
    )


async def test_an_event_rejected_by_the_api_is_dropped_counted_and_never_retried() -> None:
    api = FakeApi(replies=[accepted(4, [1, 3])])
    async with outbox(api) as (box, _, outcomes):
        for rank in range(4):
            box.emit(event(rank))
        await until(lambda: len(outcomes.seen) == 4)
    assert len(api.batches) == 1
    assert outcomes.lost == [receipt(event(1)), receipt(event(3))]
    assert outcomes.delivered == [receipt(event(0)), receipt(event(2))]
    assert (box.stats.rejected, box.stats.sent, box.stats.retries) == (2, 2, 0)


async def test_a_batch_without_any_valid_event_is_rejected_whole() -> None:
    api = FakeApi(replies=[httpx.Response(422, json={"accepted": 0, "rejected": []})])
    async with outbox(api) as (box, _, outcomes):
        for rank in range(3):
            box.emit(event(rank))
        await until(lambda: len(outcomes.seen) == 3)
    assert len(outcomes.lost) == 3
    assert box.stats.rejected == 3


@pytest.mark.parametrize("status", [400, 413])
async def test_a_refused_batch_is_cut_in_two_until_the_culprit_stands_alone(status: int) -> None:
    async def refuse_the_second(body: list[Any]) -> httpx.Response:
        if receipt(event(1)) in times(body):
            return httpx.Response(status, json={"error": "body_too_large"})
        return httpx.Response(202, json={"accepted": len(body), "duplicates": 0, "rejected": []})

    api = FakeApi(replies=[refuse_the_second] * 10)
    async with outbox(api) as (box, _, outcomes):
        for rank in range(4):
            box.emit(event(rank))
        await until(lambda: len(outcomes.seen) == 4)
    assert outcomes.lost == [receipt(event(1))]
    assert sorted(outcomes.delivered) == sorted(receipt(event(rank)) for rank in (0, 2, 3))
    assert [len(batch) for batch in api.batches][:3] == [4, 2, 1]
    assert box.stats.rejected == 1


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(503),
        httpx.Response(429),
        httpx.Response(500, text=CANARY),
        httpx.Response(307, headers={"location": "http://ailleurs.test/"}),
        httpx.ConnectError("refusée"),
        httpx.ReadTimeout("muette"),
    ],
    ids=["503", "429", "500", "307", "connexion", "lecture"],
)
async def test_an_unavailable_api_is_retried_until_it_answers(failure: Reply) -> None:
    api = FakeApi(replies=[failure, failure])
    async with outbox(api) as (box, _, outcomes):
        box.emit(event(0))
        await until(lambda: len(outcomes.seen) == 1)
    assert outcomes.delivered == [receipt(event(0))]
    assert len(api.batches) == 3
    assert box.stats.retries == 2
    # Une redirection n'est jamais suivie.
    assert {item.path for item in api.posted} == {"/api/v1/lineage/batch"}


async def test_retries_back_off_exponentially_up_to_their_cap() -> None:
    api = FakeApi(replies=[httpx.Response(503)] * 5)
    async with outbox(api, backoff=(0.02, 0.08), random=Random(1)) as (box, _, outcomes):  # noqa: S311
        box.emit(event(0))
        await until(lambda: len(outcomes.seen) == 1)
    gaps = [
        later.at - earlier.at for earlier, later in zip(api.posted, api.posted[1:], strict=False)
    ]
    # Chaque attente vaut de la moitié au tout de 0,02 s fois 2^n, plafonnée à 0,08 s.
    for rank, gap in enumerate(gaps):
        ceiling = min(0.08, 0.02 * 2**rank)
        assert ceiling / 2 - 0.005 <= gap
    assert max(gaps) < 0.08 + 0.05


async def test_retry_after_is_honoured() -> None:
    api = FakeApi(replies=[httpx.Response(429, headers={"retry-after": "1"})])
    async with outbox(api) as (box, _, outcomes):
        box.emit(event(0))
        await until(lambda: len(outcomes.seen) == 1)
    assert api.posted[1].at - api.posted[0].at >= 0.95


def test_retry_after_is_read_as_seconds_or_date_and_bounded() -> None:
    now = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
    assert retry_delay(httpx.Headers({"retry-after": "3"}), now) == 3
    assert retry_delay(httpx.Headers({"retry-after": "99999999"}), now) == RETRY_AFTER_CAP
    later = format_datetime(now + timedelta(seconds=20), usegmt=True)
    assert retry_delay(httpx.Headers({"retry-after": later}), now) == 20
    earlier = format_datetime(now - timedelta(seconds=20), usegmt=True)
    assert retry_delay(httpx.Headers({"retry-after": earlier}), now) == 0
    for value in ("-1", "1.5", "demain", "x" * 200, "Wed, 11 Nov 2026 09:00:00"):
        assert retry_delay(httpx.Headers({"retry-after": value}), now) is None
    assert retry_delay(httpx.Headers(), now) is None


async def test_a_black_hole_is_cut_by_the_request_timeout_and_retried() -> None:
    async def never(_: list[Any]) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError  # pragma: no cover

    api = FakeApi(replies=[never])
    async with outbox(api, request_timeout=0.1) as (box, _, outcomes):
        box.emit(event(0))
        await until(lambda: len(outcomes.seen) == 1)
    assert outcomes.delivered == [receipt(event(0))]
    assert box.stats.retries == 1


async def test_an_unreadable_or_oversized_answer_counts_the_batch_as_delivered() -> None:
    api = FakeApi(
        replies=[
            httpx.Response(202, content=b"{" + CANARY.encode()),
            httpx.Response(202, json={"rejected": [{"index": "1"}, {"index": 99}, {"x": 1}]}),
            httpx.Response(202, json={"rejected": "tous"}),
            httpx.Response(202, content=b" " * (2 * 1024 * 1024)),
        ]
    )
    async with outbox(api) as (box, _, outcomes):
        for rank in range(4):
            box.emit(event(rank))
            await until(outcomes.counted(rank + 1))
    assert outcomes.lost == []
    assert box.stats.sent == 4


# Relevés de couverture : une file prioritaire, jamais évincée par des événements.


async def test_reports_leave_before_events_and_are_never_evicted_by_them() -> None:
    box_settings = settings(max_events=2)
    async with outbox(start=False, box_settings=box_settings) as (box, api, outcomes):
        for rank in range(5):
            box.emit(event(rank))
        box.report_coverage(report("09:00"))
        box.report_coverage(report("09:01"))
        await box.start()
        await until(lambda: len(api.reports) == 2 and len(outcomes.delivered) == 2)
    assert [item.path for item in api.posted][:2] == ["/api/v1/coverage"] * 2
    assert api.reports == [report("09:00"), report("09:01")]
    assert box.stats.reports_sent == 2


async def test_a_corrected_report_replaces_the_one_still_waiting_for_its_window() -> None:
    async with outbox(start=False) as (box, api, _):
        box.report_coverage(report("09:00"))
        box.report_coverage(report("09:01"))
        box.report_coverage(report("09:00", lost=3))
        await box.start()
        await until(lambda: len(api.reports) == 2)
    assert api.reports == [report("09:00", lost=3), report("09:01")]


async def test_a_report_refused_by_the_api_is_dropped_and_another_retried() -> None:
    api = FakeApi(report_replies=[httpx.Response(422, json={"errors": []}), httpx.Response(503)])
    async with outbox(api, start=False) as (box, _, _):
        box.report_coverage(report("09:00"))
        box.report_coverage(report("09:01"))
        await box.start()
        await until(lambda: box.stats.reports_sent == 1)
    assert api.reports == [report("09:00"), report("09:01"), report("09:01")]
    assert box.stats.reports_dropped == 1


async def test_the_report_queue_is_bounded_too() -> None:
    async with outbox(start=False, max_reports=2) as (box, api, _):
        for minute in range(4):
            box.report_coverage(report(f"09:0{minute}"))
        await box.start()
        await until(lambda: len(api.reports) == 2)
    assert api.reports == [report("09:02"), report("09:03")]
    assert box.stats.reports_dropped == 2


# Arrêt : vidage dans le délai, le reste perdu et dit.


async def test_at_shutdown_the_outbox_sends_what_it_can_within_its_delay() -> None:
    async with outbox(start=False) as (box, api, outcomes):
        for rank in range(10):
            box.emit(event(rank))
        box.report_coverage(report("09:00"))
        await box.aclose()
        assert len(outcomes.delivered) == 10
        assert api.reports == [report("09:00")]
        box.emit(event(11))
        assert outcomes.lost == [receipt(event(11))]
        assert box.stats.dropped_shutdown == 1


async def test_at_shutdown_what_remains_is_lost_and_the_last_report_says_so() -> None:
    async def hold(_: list[Any]) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError  # pragma: no cover

    api = FakeApi(replies=[hold])
    box_settings = settings(flush_seconds=0.5)

    def correct(receipt: str, delivered: bool) -> None:
        # Le transport répond à chaque perte par le relevé corrigé de sa fenêtre.
        box.report_coverage(report("09:00", lost=sum(1 for _ in lost) + 1))
        lost.append(receipt)

    lost: list[str] = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(api))
    box = Outbox(box_settings, client=client, request_timeout=30)
    box.watch(correct)
    await box.start()
    for rank in range(3):
        box.emit(event(rank))
    await until(lambda: len(api.batches) == 1)
    started = time.monotonic()
    await box.aclose()
    await client.aclose()

    assert time.monotonic() - started < 0.5 + 0.3
    assert lost == [receipt(event(rank)) for rank in range(3)]
    assert box.stats.dropped_shutdown == 3
    assert api.reports[-1] == report("09:00", lost=3)


async def test_without_delay_nothing_is_sent_at_shutdown() -> None:
    async with outbox(start=False, box_settings=settings(flush_seconds=0)) as (box, api, outcomes):
        box.emit(event(0))
        box.report_coverage(report())
        await box.aclose()
    assert api.posted == []
    assert outcomes.lost == [receipt(event(0))]
    assert box.stats.reports_dropped == 1


async def test_a_failing_listener_never_stops_the_outbox(caplog: pytest.LogCaptureFixture) -> None:
    def broken(receipt: str, delivered: bool) -> None:
        raise RuntimeError(CANARY)

    async with outbox() as (box, api, _):
        box.watch(broken)
        box.emit(event(0))
        box.emit(event(1))
        await until(lambda: box.stats.sent == 2)
    assert len(api.delivered) == 2
    assert CANARY not in caplog.text
    assert "RuntimeError" in caplog.text


# Rien du contenu ni de l'API ne sort dans le journal.


async def test_the_journal_names_a_failure_by_its_class_never_by_a_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    api = FakeApi(
        replies=[
            httpx.ConnectError(CANARY),
            httpx.Response(500, text=CANARY, headers={"x-canari": CANARY}),
            httpx.Response(202, json={"rejected": [{"index": 0, "errors": [{"message": CANARY}]}]}),
        ]
    )
    async with outbox(api) as (box, _, outcomes):
        trapped = event(0)
        trapped["run"]["facets"]["piege"] = {"valeur": CANARY}
        box.emit(trapped)
        await until(lambda: len(outcomes.seen) == 1)
    assert CANARY not in caplog.text
    assert API.removeprefix("http://") not in caplog.text
    # Un épisode d'indisponibilité se dit une fois, par la classe de son premier échec.
    assert "ConnectError" in caplog.text
    assert "rejeté" in caplog.text


# Réglages.


def test_settings_have_their_defaults() -> None:
    created = OutboxSettings.create(API)
    assert created.api_url == httpx.URL(API)
    assert (created.max_events, created.max_bytes, created.flush_seconds) == (
        DEFAULT_MAX_EVENTS,
        DEFAULT_MAX_BYTES,
        DEFAULT_FLUSH_SECONDS,
    )
    assert (DEFAULT_MAX_EVENTS, DEFAULT_MAX_BYTES, DEFAULT_FLUSH_SECONDS) == (10_000, 32 << 20, 5)
    assert (BATCH_EVENTS, BATCH_BYTES) == (500, 1 << 20)
    prefixed = OutboxSettings.create("https://api.test/answerlineage/")
    assert prefixed.events_url == httpx.URL("https://api.test/answerlineage/api/v1/lineage/batch")
    assert prefixed.coverage_url == httpx.URL("https://api.test/answerlineage/api/v1/coverage")


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("ftp://api.test", "http ou https"),
        ("api.test", "http ou https"),
        ("http://", "http ou https"),
        (f"http://moi:{CANARY}@api.test", "aucun identifiant"),
        (f"http://{CANARY}@api.test", "aucun identifiant"),
        (f"http://api.test/?jeton={CANARY}", "ni paramètres ni fragment"),
        (f"http://api.test/#{CANARY}", "ni paramètres ni fragment"),
        ("http://[::1", "illisible"),
    ],
)
def test_an_invalid_address_is_refused_without_being_cited(url: str, message: str) -> None:
    with pytest.raises(ProxyConfigurationError, match=message) as refused:
        OutboxSettings.create(url)
    assert CANARY not in str(refused.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"max_events": 0},
        {"max_events": 1_000_001},
        {"max_bytes": (1 << 20) - 1},
        {"max_bytes": (1 << 30) + 1},
        {"flush_seconds": -1},
        {"flush_seconds": 301},
    ],
)
def test_bounds_are_bounded(changes: dict[str, Any]) -> None:
    with pytest.raises(ProxyConfigurationError):
        OutboxSettings.create(API, **changes)


UPSTREAM = ["--upstream", "http://127.0.0.1:9000"]


def test_the_outbox_is_set_by_option_or_environment() -> None:
    assert cli.outbox_settings_from_args(UPSTREAM, {}) is None
    from_environment = cli.outbox_settings_from_args(
        UPSTREAM,
        {
            cli.ENV_API_URL: "http://127.0.0.1:8000",
            cli.ENV_OUTBOX_MAX_EVENTS: "20",
            cli.ENV_OUTBOX_MAX_BYTES: str(2 << 20),
            cli.ENV_OUTBOX_FLUSH_SECONDS: "1",
        },
    )
    assert from_environment == OutboxSettings.create(
        "http://127.0.0.1:8000", max_events=20, max_bytes=2 << 20, flush_seconds=1
    )
    from_options = cli.outbox_settings_from_args(
        [
            *UPSTREAM,
            "--api-url",
            "http://127.0.0.1:8001",
            "--outbox-max-events",
            "30",
            "--outbox-max-bytes",
            str(3 << 20),
            "--outbox-flush-seconds",
            "2",
        ],
        {cli.ENV_API_URL: "http://127.0.0.1:8000", cli.ENV_OUTBOX_MAX_EVENTS: "20"},
    )
    assert from_options == OutboxSettings.create(
        "http://127.0.0.1:8001", max_events=30, max_bytes=3 << 20, flush_seconds=2
    )


def test_the_command_line_sends_through_the_outbox_when_an_api_is_set(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    served: dict[str, Any] = {}
    monkeypatch.setattr(
        "answerlineage_proxy.lifecycle.run", lambda app, settings: served.update(app=app) or 0
    )
    caplog.set_level(logging.INFO)
    assert cli.main([*UPSTREAM, "--api-url", "http://127.0.0.1:8000"]) == 0
    assert isinstance(served["app"]._sink, Outbox)
    assert "boîte d'envoi" in caplog.text

    assert cli.main(UPSTREAM) == 0
    assert isinstance(served["app"]._sink, NullSink)


@pytest.mark.parametrize(
    "argv",
    [
        ["--api-url", "ftp://127.0.0.1"],
        ["--api-url", "http://127.0.0.1", "--outbox-max-events", "0"],
    ],
)
def test_an_invalid_outbox_setting_stops_the_proxy(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("answerlineage_proxy.lifecycle.run", lambda app, settings: 0)
    assert cli.main([*UPSTREAM, *argv]) == 2
    assert "Configuration invalide" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ANSWERLINEAGE_PROXY_OUTBOX_MAX_EVENTS", "x"),
        ("ANSWERLINEAGE_PROXY_OUTBOX_FLUSH_SECONDS", "x"),
    ],
)
def test_an_environment_bound_must_be_a_number(name: str, value: str) -> None:
    with pytest.raises(ProxyConfigurationError):
        cli.outbox_settings_from_args(UPSTREAM, {cli.ENV_API_URL: API, name: value})


async def test_an_event_evicted_while_it_is_serialized_is_counted_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # La sérialisation des deux premiers est retenue dans son fil, le temps que deux
    # autres arrivent : sans cette retenue, le test dépendrait de l'ordonnanceur.
    started, release = threading.Event(), threading.Event()
    encode = outbox_module._encode

    def held(events: Any) -> Any:
        started.set()
        release.wait(timeout=10)
        return encode(events)

    monkeypatch.setattr(outbox_module, "_encode", held)
    async with outbox(start=False, box_settings=settings(max_events=2)) as (box, api, outcomes):
        box.emit(event(0))
        box.emit(event(1))
        await box.start()
        await until(started.is_set)
        box.emit(event(2))
        box.emit(event(3))
        monkeypatch.setattr(outbox_module, "_encode", encode)
        release.set()
        await until(lambda: len(outcomes.seen) == 4)
    assert outcomes.lost == [receipt(event(0)), receipt(event(1))]
    assert times(api.delivered) == [receipt(event(2)), receipt(event(3))]
    assert box.stats.dropped_overflow == 2


async def test_a_batch_put_back_after_a_failure_still_fits_the_bounds() -> None:
    gate = asyncio.Event()

    async def fail_later(_: list[Any]) -> httpx.Response:
        await gate.wait()
        return httpx.Response(503)

    api = FakeApi(replies=[fail_later])
    async with outbox(api, box_settings=settings(max_events=3)) as (box, _, outcomes):
        box.emit(event(0))
        box.emit(event(1))
        await until(lambda: len(api.batches) == 1)
        # Le lot de deux est en route ; trois autres remplissent la file.
        for rank in range(2, 5):
            box.emit(event(rank))
        await until(lambda: box.queued_bytes > 0 and len(box._raw) == 0)
        gate.set()
        await until(lambda: len(outcomes.seen) == 5)
    # Remis en tête, le lot fait déborder la file : ses événements, les plus anciens, partent.
    assert outcomes.lost == [receipt(event(0)), receipt(event(1))]
    assert outcomes.delivered == [receipt(event(rank)) for rank in (2, 3, 4)]


async def test_a_report_is_retried_after_a_network_failure_unless_corrected_meanwhile() -> None:
    gate = asyncio.Event()

    async def fail_later(_: Any) -> httpx.Response:
        await gate.wait()
        raise httpx.ConnectError("coupée")

    api = FakeApi(report_replies=[fail_later, httpx.ConnectError("coupée")])
    async with outbox(api, start=False) as (box, _, _):
        box.report_coverage(report("09:00"))
        await box.start()
        await until(lambda: len(api.reports) == 1)
        # Sa correction arrive pendant l'envoi : c'est elle qui repart, pas l'ancien relevé.
        box.report_coverage(report("09:00", lost=1))
        gate.set()
        await until(lambda: box.stats.reports_sent == 1)
    assert api.reports == [report("09:00"), report("09:00", lost=1), report("09:00", lost=1)]


async def test_the_age_of_the_oldest_event_waiting_is_measured() -> None:
    now = [100.0]
    async with outbox(start=False, clock=lambda: now[0]) as (box, _, _):
        assert box.oldest_age == 0
        box.emit(event(0))
        now[0] = 102.5
        box.emit(event(1))
        assert box.oldest_age == 2.5


async def test_a_report_after_shutdown_is_counted_lost_and_a_refused_one_too() -> None:
    api = FakeApi(report_replies=[httpx.Response(422)])
    async with outbox(api, start=False) as (box, _, _):
        box.report_coverage(report("09:00"))
        await box.aclose()
        box.report_coverage(report("09:01"))
    assert box.stats.reports_dropped == 2


async def test_the_outbox_reaches_a_refused_address_with_its_own_client() -> None:
    with socket.socket() as reserved:
        # Réservé sans écouter : la connexion y est refusée.
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        box = Outbox(settings_for(f"http://127.0.0.1:{port}", flush_seconds=0))
        outcomes = Outcomes()
        box.watch(outcomes)
        await box.start()
        box.emit(event(0))
        await until(lambda: box.stats.retries >= 1)
        await box.aclose()
    assert outcomes.lost == [receipt(event(0))]
    assert (box.stats.sent, box.stats.dropped_shutdown) == (0, 1)


async def hold(_: Any) -> httpx.Response:
    await asyncio.Event().wait()
    raise AssertionError  # pragma: no cover


@pytest.mark.parametrize("second", [hold, httpx.Response(422)], ids=["muette", "refusé"])
async def test_a_report_interrupted_at_shutdown_is_tried_once_more_then_counted_lost(
    second: Reply,
) -> None:
    api = FakeApi(report_replies=[hold, second])
    box_settings = settings(flush_seconds=0.3)
    async with outbox(api, box_settings=box_settings, request_timeout=30) as (box, _, _):
        box.report_coverage(report("09:00"))
        await until(lambda: len(api.reports) == 1)
        await box.aclose()
    assert len(api.reports) == 2
    assert (box.stats.reports_sent, box.stats.reports_dropped) == (0, 1)


# Le canari : rien du contenu d'un appel dans ce que la boîte envoie ni dans son journal.


async def test_no_content_of_a_call_leaves_through_the_outbox_nor_its_journal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    api = FakeApi(
        replies=[httpx.Response(500, text=CANARY), httpx.ConnectError(CANARY)],
        report_replies=[httpx.Response(503, text=CANARY)],
    )
    upstream = FakeMcpServer(tool_result=TOOL_RESULT.replace(b"Il fait beau", CANARY.encode()))
    client = httpx.AsyncClient(transport=httpx.MockTransport(api))
    box = Outbox(settings(), client=client, backoff=(0.01, 0.05))
    key = FingerprintKey(b"k" * 32)
    async with running(upstream) as upstream_url:
        proxy = McpProxy(ProxySettings.create(upstream_url, "meteo", fingerprint_key=key), box)
        async with running(proxy, transparent=True) as proxy_url:
            relayed = await exchange(
                "POST",
                proxy_url + "/mcp",
                headers=[("content-type", "application/json"), ("authorization", CANARY)],
                content=tool_call(arguments={"lieu": CANARY}),
            )
            assert CANARY.encode() in relayed.body
            await until(lambda: box.stats.sent == 1)
    await client.aclose()

    sent = json.dumps([item.body for item in api.posted])
    assert len(api.batches) == 3
    assert api.reports
    assert CANARY not in sent
    assert CANARY not in caplog.text
