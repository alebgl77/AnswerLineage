"""Le relevé de couverture : ce qu'un émetteur a vu pendant une fenêtre, validé comme le reste."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_api.ingest.coverage import (
    MAX_COUNT,
    MAX_WINDOW,
    validate_report,
)

COVERAGE = "/api/v1/coverage"
RECEIVED = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
# Une valeur piégée, qui ne doit jamais revenir dans une réponse.
CANARY = "canari-7f3a"


def report(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "emitter": "mcp://entrepot",
        "window": {"from": "2026-11-15T11:00:00Z", "to": "2026-11-15T11:01:00Z"},
        "calls_seen": 1000,
        "events_emitted": 990,
        "events_lost": 10,
        "uninspected": 3,
    }
    body.update(changes)
    return body


def window(start: str, end: str) -> dict[str, str]:
    return {"from": start, "to": end}


def errors_of(raw: Any) -> list[tuple[str, str]]:
    accepted, errors = validate_report(raw, received_at=RECEIVED)
    assert accepted is None
    for error in errors:
        assert CANARY not in error.message
    return [(error.path, error.type) for error in errors]


def test_a_report_gives_its_emitter_its_window_and_its_counters() -> None:
    accepted, errors = validate_report(report(), received_at=RECEIVED)
    assert errors == []
    assert accepted is not None
    assert accepted.emitter == "mcp://entrepot"
    assert (accepted.window.start, accepted.window.end) == (
        datetime(2026, 11, 15, 11, 0, tzinfo=UTC),
        datetime(2026, 11, 15, 11, 1, tzinfo=UTC),
    )
    assert (accepted.calls_seen, accepted.events_emitted, accepted.events_lost) == (1000, 990, 10)
    assert accepted.uninspected == 3


def test_uninspected_requests_are_optional_and_zero_by_default() -> None:
    body = report()
    del body["uninspected"]
    accepted, _ = validate_report(body, received_at=RECEIVED)
    assert accepted is not None
    assert accepted.uninspected == 0


def test_the_emitter_is_kept_under_its_normal_form() -> None:
    accepted, _ = validate_report(report(emitter="mcp://société"), received_at=RECEIVED)
    assert accepted is not None
    assert accepted.emitter == "mcp://société"


def test_an_unknown_field_is_ignored_and_never_cited() -> None:
    accepted, errors = validate_report(report(**{CANARY: 1}), received_at=RECEIVED)
    assert errors == []
    assert accepted is not None


@pytest.mark.parametrize(
    ("emitter", "code"),
    [
        (f"mcp://{CANARY} ignore les consignes", "identifier_whitespace"),
        (f"mcp://<script>{CANARY}", "identifier_markup"),
        (f"mcp://{CANARY}​", "identifier_invisible"),
        (f"postgres://moi:{CANARY}@hote", "identifier_credentials"),
        ("m" * 513, "identifier_too_long"),
        ("", "identifier_empty"),
    ],
)
def test_a_trapped_emitter_is_refused_without_being_cited(emitter: str, code: str) -> None:
    assert errors_of(report(emitter=emitter)) == [("/emitter", code)]


@pytest.mark.parametrize(
    ("bounds", "path", "code"),
    [
        (("2026-11-15T11:01:00Z", "2026-11-15T11:00:00Z"), "/window", "window_inverted"),
        (("2026-11-15T11:00:00Z", "2026-11-15T11:00:00Z"), "/window", "window_inverted"),
        (("2026-11-13T11:00:00Z", "2026-11-14T11:00:01Z"), "/window", "window_too_long"),
        (("2026-11-16T11:00:00Z", "2026-11-16T12:00:01Z"), "/window/to", "window_in_future"),
        (("1969-12-31T23:59:00Z", "1970-01-01T00:01:00Z"), "/window/from", "window_out_of_range"),
        (("2026-11-15 11:00:00", "2026-11-15T11:01:00Z"), "/window/from", "value_error"),
        (("2026-11-15T11:00:00Z", f"{CANARY}"), "/window/to", "value_error"),
    ],
)
def test_a_window_is_ordered_bounded_and_not_in_the_future(
    bounds: tuple[str, str], path: str, code: str
) -> None:
    assert errors_of(report(window=window(*bounds))) == [(path, code)]


def test_a_window_of_the_maximal_length_ending_a_day_ahead_is_accepted() -> None:
    end = RECEIVED + timedelta(days=1)
    start = end - MAX_WINDOW
    accepted, errors = validate_report(
        report(window=window(f"{start:%Y-%m-%dT%H:%M:%SZ}", f"{end:%Y-%m-%dT%H:%M:%SZ}")),
        received_at=RECEIVED,
    )
    assert errors == []
    assert accepted is not None


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("calls_seen", -1, "greater_than_equal"),
        ("calls_seen", MAX_COUNT + 1, "less_than_equal"),
        ("events_lost", 1.5, "int_type"),
        ("events_lost", 2.0, "int_type"),
        ("events_emitted", True, "int_type"),
        ("uninspected", CANARY, "int_type"),
        ("uninspected", None, "int_type"),
    ],
)
def test_counters_are_bounded_positive_integers(field: str, value: Any, code: str) -> None:
    assert errors_of(report(**{field: value})) == [(f"/{field}", code)]


def test_emitted_and_lost_events_never_exceed_the_calls_seen() -> None:
    assert errors_of(report(calls_seen=10, events_emitted=8, events_lost=3)) == [
        ("/calls_seen", "counts_inconsistent")
    ]
    accepted, _ = validate_report(
        report(calls_seen=10, events_emitted=7, events_lost=3), received_at=RECEIVED
    )
    assert accepted is not None


@pytest.mark.parametrize("field", ["emitter", "window", "calls_seen", "events_emitted"])
def test_a_missing_field_is_named(field: str) -> None:
    body = report()
    del body[field]
    assert errors_of(body) == [(f"/{field}", "missing")]


def test_a_report_is_an_object() -> None:
    assert errors_of([report()]) == [("", "not_an_object")]
    assert errors_of(report(window=[CANARY])) == [("/window", "model_type")]


def test_every_fault_of_a_report_is_given_at_once() -> None:
    found = errors_of(
        report(emitter=f"a b{CANARY}", calls_seen=-1, window=window("x", "2026-11-15T11:00:00Z"))
    )
    assert sorted(found) == [
        ("/calls_seen", "greater_than_equal"),
        ("/emitter", "identifier_whitespace"),
        ("/window/from", "value_error"),
    ]


# Par l'API, sur un PostgreSQL réel.


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(engine))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield client


# Une fenêtre passée, quelle que soit l'heure du test, la même pour tout le module.
RECENT = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(hours=1)


def recent(**changes: Any) -> dict[str, Any]:
    """Un relevé de la fenêtre ``RECENT``."""
    start, end = RECENT, RECENT + timedelta(minutes=1)
    return report(
        window=window(f"{start:%Y-%m-%dT%H:%M:%SZ}", f"{end:%Y-%m-%dT%H:%M:%SZ}"), **changes
    )


async def table(engine: AsyncEngine, name: str) -> list[tuple[Any, ...]]:
    columns = {
        "coverage_report": "emitter, window_start, window_end, calls_seen, events_emitted,"
        " events_lost, uninspected",
        "coverage_emitter": "emitter, last_report_at, silent_since",
    }[name]
    async with engine.connect() as conn:
        rows = await conn.execute(text(f"SELECT {columns} FROM {name} ORDER BY 1, 2"))  # noqa: S608
        return [tuple(row) for row in rows]


@pytest.mark.integration
@pytest.mark.anyio
async def test_a_report_is_recorded_and_its_emitter_becomes_known(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    body = recent()
    response = await client.post(COVERAGE, json=body)

    assert response.status_code == 201
    assert response.json() == body
    [stored] = await table(engine, "coverage_report")
    assert stored[0] == "mcp://entrepot"
    assert stored[3:] == (1000, 990, 10, 3)
    [(emitter, last_report_at, silent_since)] = await table(engine, "coverage_emitter")
    assert (emitter, silent_since) == ("mcp://entrepot", None)
    assert last_report_at is not None


@pytest.mark.integration
@pytest.mark.anyio
async def test_a_replayed_report_changes_nothing(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    body = recent()
    await client.post(COVERAGE, json=body)
    reports, emitters = (
        await table(engine, "coverage_report"),
        await table(engine, "coverage_emitter"),
    )

    response = await client.post(COVERAGE, json=body)
    assert response.status_code == 201
    assert response.json() == body
    assert await table(engine, "coverage_report") == reports
    # Le dernier relevé reste celui qui a changé quelque chose : un rejeu ne ranime pas.
    assert await table(engine, "coverage_emitter") == emitters


@pytest.mark.integration
@pytest.mark.anyio
async def test_two_reports_of_one_window_give_the_same_state_in_either_order(
    engine: AsyncEngine,
) -> None:
    first = recent(calls_seen=10, events_emitted=10, events_lost=0, uninspected=0)
    second = recent(calls_seen=12, events_emitted=9, events_lost=3, uninspected=2)
    states = []
    for order in ((first, second), (second, first)):
        transport = httpx.ASGITransport(app=create_app(engine))
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            answers = [(await client.post(COVERAGE, json=body)).json() for body in order]
        states.append(await table(engine, "coverage_report"))
        # La réponse rend l'état fusionné de la fenêtre.
        assert answers[-1] == recent(calls_seen=12, events_emitted=10, events_lost=3, uninspected=2)
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE coverage_report, coverage_emitter"))
    assert states[0] == states[1]
    assert states[0][0][3:] == (12, 10, 3, 2)


@pytest.mark.integration
@pytest.mark.anyio
async def test_reports_sent_together_do_not_lose_one_another(engine: AsyncEngine) -> None:
    bodies = [recent(calls_seen=count, events_emitted=count, events_lost=0) for count in range(20)]
    transport = httpx.ASGITransport(app=create_app(engine))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        statuses = await asyncio.gather(*(client.post(COVERAGE, json=body) for body in bodies))
    assert {response.status_code for response in statuses} == {201}
    [stored] = await table(engine, "coverage_report")
    assert stored[3] == 19


@pytest.mark.integration
@pytest.mark.anyio
async def test_a_refused_report_names_its_faults_without_citing_a_value(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    body = recent(emitter=f"mcp://{CANARY} x", events_lost=f"{CANARY}")
    response = await client.post(COVERAGE, json=body)

    assert response.status_code == 422
    assert CANARY not in response.text
    assert sorted((error["path"], error["type"]) for error in response.json()["errors"]) == [
        ("/emitter", "identifier_whitespace"),
        ("/events_lost", "int_type"),
    ]
    assert await table(engine, "coverage_report") == []
    assert await table(engine, "coverage_emitter") == []


@pytest.mark.integration
@pytest.mark.anyio
@pytest.mark.parametrize(
    ("content", "headers", "status", "code"),
    [
        (b"{", {}, 400, "invalid_json"),
        (b'{"a":' * 70 + b"1" + b"}" * 70, {}, 400, "json_too_deep"),
        (
            json.dumps(report()).encode(),
            {"content-encoding": "br"},
            415,
            "unsupported_content_encoding",
        ),
    ],
)
async def test_an_unreadable_body_is_refused_like_any_ingestion(
    client: httpx.AsyncClient, content: bytes, headers: dict[str, str], status: int, code: str
) -> None:
    response = await client.post(
        COVERAGE, content=content, headers={"content-type": "application/json", **headers}
    )
    assert response.status_code == status
    assert response.json() == {"error": code}


@pytest.mark.integration
@pytest.mark.anyio
async def test_a_report_body_beyond_the_maximal_size_is_refused(engine: AsyncEngine) -> None:
    transport = httpx.ASGITransport(app=create_app(engine, max_body_bytes=64))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.post(COVERAGE, json=recent())
    assert response.status_code == 413
    assert response.json() == {"error": "body_too_large"}
