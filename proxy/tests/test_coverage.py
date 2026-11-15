"""Les relevés de couverture du proxy : ses compteurs, fenêtre par fenêtre."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from answerlineage_proxy import cli
from answerlineage_proxy.coverage import (
    CoverageWindows,
    Report,
    report_periodically,
)
from answerlineage_proxy.sink import MemorySink, NullSink
from proxy.tests.conftest import (
    MODERN_HEADERS,
    MODERN_META,
    BenchFactory,
    exchange,
    tool_call,
)

T0 = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def windows(started: float = 0) -> CoverageWindows:
    return CoverageWindows("mcp://meteo", length=MINUTE, started_at=at(started))


def summary(report: Report) -> tuple[str, str, int, int, int, int]:
    return (
        report["window"]["from"][11:19],
        report["window"]["to"][11:19],
        report["calls_seen"],
        report["events_emitted"],
        report["events_lost"],
        report["uninspected"],
    )


def call(counter: CoverageWindows, seconds: float, *, emitted: bool = True) -> None:
    counter.begin(at(seconds))
    counter.settle(at(seconds), emitted=emitted)


def test_a_call_counts_in_the_window_of_its_event_and_a_closed_window_gives_its_report() -> None:
    counter = windows()
    call(counter, 5)
    call(counter, 59.999)
    call(counter, 60, emitted=False)
    counter.uninspected(at(30))

    assert counter.close_due(at(59)) == []
    [first] = counter.close_due(at(60))
    assert first == {
        "emitter": "mcp://meteo",
        "window": {"from": "2026-11-11T09:00:00.000000Z", "to": "2026-11-11T09:01:00.000000Z"},
        "calls_seen": 2,
        "events_emitted": 2,
        "events_lost": 0,
        "uninspected": 1,
    }
    assert [summary(report) for report in counter.close_due(at(120))] == [
        ("09:01:00", "09:02:00", 1, 0, 1, 0)
    ]


def test_a_window_without_call_is_reported_all_the_same() -> None:
    counter = windows()
    assert [summary(report) for report in counter.close_due(at(180))] == [
        ("09:00:00", "09:01:00", 0, 0, 0, 0),
        ("09:01:00", "09:02:00", 0, 0, 0, 0),
        ("09:02:00", "09:03:00", 0, 0, 0, 0),
    ]


def test_windows_are_aligned_and_the_first_starts_when_the_proxy_starts() -> None:
    counter = windows(started=42.5)
    call(counter, 50)
    call(counter, 61)
    reports = counter.close_due(at(120))
    assert [summary(report) for report in reports] == [
        ("09:00:42", "09:01:00", 1, 1, 0, 0),
        ("09:01:00", "09:02:00", 1, 1, 0, 0),
    ]
    assert reports[0]["window"]["from"] == "2026-11-11T09:00:42.500000Z"


def test_a_window_stays_open_until_its_calls_are_settled_and_reports_leave_in_order() -> None:
    counter = windows()
    # L'appel est décrit dans la première fenêtre ; son SQL s'analyse encore.
    counter.begin(at(59))
    call(counter, 70)
    assert counter.close_due(at(150)) == []

    counter.settle(at(59), emitted=True)
    assert [summary(report) for report in counter.close_due(at(150))] == [
        ("09:00:00", "09:01:00", 1, 1, 0, 0),
        ("09:01:00", "09:02:00", 1, 1, 0, 0),
    ]


def test_a_clock_set_back_counts_the_call_in_the_oldest_open_window() -> None:
    counter = windows()
    counter.close_due(at(60))
    call(counter, 30)
    assert [summary(report) for report in counter.close_due(at(120))] == [
        ("09:01:00", "09:02:00", 1, 1, 0, 0)
    ]


def test_at_shutdown_the_current_window_ends_when_the_proxy_stops() -> None:
    counter = windows()
    call(counter, 10)
    call(counter, 65)
    assert [summary(report) for report in counter.close_all(at(90))] == [
        ("09:00:00", "09:01:00", 1, 1, 0, 0),
        ("09:01:00", "09:01:30", 1, 1, 0, 0),
    ]
    # Rien n'est remis deux fois.
    assert counter.close_all(at(200)) == []
    assert counter.close_due(at(200)) == []


def test_at_shutdown_a_call_still_waiting_counts_as_lost_with_what_follows() -> None:
    counter = windows()
    counter.begin(at(10))
    call(counter, 62)
    assert [summary(report) for report in counter.close_all(at(65))] == [
        ("09:00:00", "09:01:05", 2, 1, 1, 0)
    ]


def test_at_shutdown_calls_counted_beyond_the_clock_stay_inside_the_last_window() -> None:
    counter = windows()
    call(counter, 70)
    assert [summary(report) for report in counter.close_all(at(30))] == [
        ("09:00:00", "09:02:00", 1, 1, 0, 0)
    ]


def test_a_proxy_stopped_on_a_window_boundary_reports_no_empty_window() -> None:
    counter = windows()
    assert len(counter.close_all(at(60))) == 1


@pytest.mark.parametrize("length", [timedelta(0), timedelta(milliseconds=999), timedelta(days=2)])
def test_a_window_lasts_from_one_second_to_one_day(length: timedelta) -> None:
    with pytest.raises(ValueError, match="une seconde à un jour"):
        CoverageWindows("mcp://meteo", length=length, started_at=T0)


def test_the_next_close_is_awaited_until_the_end_of_the_open_window() -> None:
    counter = windows()
    assert counter.seconds_until_due(at(15)) == 45
    counter.begin(at(10))
    # Une fenêtre échue qui attend un appel est revue peu après.
    assert 0 < counter.seconds_until_due(at(61)) < 1


@pytest.mark.anyio
async def test_reports_are_delivered_as_each_window_closes_without_sleeping() -> None:
    counter = windows()
    call(counter, 3)
    moments = iter([at(30), at(60), at(125)])
    delivered: list[Report] = []
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) == 3:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await report_periodically(counter, lambda: next(moments), delivered.extend, sleep=sleep)
    assert [summary(report)[:3] for report in delivered] == [
        ("09:00:00", "09:01:00", 1),
        ("09:01:00", "09:02:00", 0),
    ]
    assert waits == [30, 60, 55]


def test_sinks_take_reports_apart_from_events() -> None:
    sink = MemorySink(capacity=2)
    for rank in range(3):
        sink.report_coverage({"rank": rank})
    assert sink.reports == [{"rank": 1}, {"rank": 2}]
    assert sink.events == []
    null = NullSink()
    null.report_coverage({})
    assert null.discarded_reports == 1


# Le proxy en marche, devant un faux serveur MCP.


@pytest.mark.anyio
async def test_the_proxy_reports_its_calls_and_uninspected_requests_when_it_stops(
    bench: BenchFactory,
) -> None:
    async with bench() as b:
        for _ in range(2):
            await exchange(
                "POST",
                b.proxy_url + "/mcp",
                headers=MODERN_HEADERS,
                content=tool_call(**MODERN_META),
            )
        await exchange("POST", b.proxy_url + "/mcp", content=b"[1, 2]")
        events = b.sink.events
        proxy, sink = b.proxy, b.sink
        assert sink.reports == []

    reports = sink.reports
    assert sum(report["calls_seen"] for report in reports) == 2
    assert sum(report["events_emitted"] for report in reports) == 2
    assert sum(report["uninspected"] for report in reports) == 1
    assert sum(report["events_lost"] for report in reports) == 0
    # L'émetteur du relevé est celui des événements : l'espace de noms de leurs jobs.
    assert {report["emitter"] for report in reports} == {
        event["job"]["namespace"] for event in events
    }
    # Les fenêtres se suivent, et chaque événement tombe dans l'une d'elles.
    bounds = [(report["window"]["from"], report["window"]["to"]) for report in reports]
    assert all(end == start for (_, end), (start, _) in itertools.pairwise(bounds))
    assert all(bounds[0][0] <= event["eventTime"] < bounds[-1][1] for event in events)
    assert proxy.coverage.close_all(datetime.now(UTC)) == []


@pytest.mark.anyio
async def test_a_lost_event_is_counted_lost_in_its_window(bench: BenchFactory) -> None:
    class FailingEvents(MemorySink):
        def emit(self, event: Mapping[str, Any]) -> None:
            raise RuntimeError("destination en panne")

    sink = FailingEvents()
    async with bench() as b:
        b.proxy._sink = sink
        proxied = await exchange("POST", b.proxy_url + "/mcp", content=tool_call())
        assert proxied.status == 200
    [report] = [report for report in sink.reports if report["calls_seen"]]
    assert (report["calls_seen"], report["events_emitted"], report["events_lost"]) == (1, 0, 1)


# La durée des fenêtres, réglée à la ligne de commande.


def test_the_window_is_set_by_option_or_environment() -> None:
    upstream = ["--upstream", "http://127.0.0.1:9000"]
    assert cli.coverage_window_from_args(upstream, {}) == timedelta(minutes=1)
    assert cli.coverage_window_from_args(upstream, {cli.ENV_COVERAGE_WINDOW: "300"}) == timedelta(
        minutes=5
    )
    assert cli.coverage_window_from_args(
        [*upstream, "--coverage-window-seconds", "10"], {cli.ENV_COVERAGE_WINDOW: "300"}
    ) == timedelta(seconds=10)


@pytest.mark.parametrize(("option", "environment"), [("0", ""), ("86401", ""), (None, "x")])
def test_an_invalid_window_stops_the_proxy_with_a_configuration_error(
    option: str | None,
    environment: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("uvicorn.run", lambda app, **options: None)
    argv = ["--upstream", "http://127.0.0.1:9000"]
    if option is not None:
        argv += ["--coverage-window-seconds", option]
    if environment:
        monkeypatch.setenv(cli.ENV_COVERAGE_WINDOW, environment)
    assert cli.main(argv) == 2
    assert "Configuration invalide" in capsys.readouterr().err


def test_the_proxy_started_from_the_command_line_uses_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served: dict[str, Any] = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **options: served.update(app=app))
    assert cli.main(["--upstream", "http://127.0.0.1:9000", "--coverage-window-seconds", "5"]) == 0
    assert served["app"].coverage.length == timedelta(seconds=5)
