"""La couverture du lignage : les compteurs des émetteurs comparés au graphe, et ses trous."""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_api.ingest.models import validate_event
from answerlineage_api.ingest.store import AcceptedEvent, event_identifier, store_events
from answerlineage_api.lineage import coverage
from answerlineage_api.lineage.coverage import Report
from answerlineage_api.lineage.trace import answer_key
from answerlineage_api.store.graph import (
    AgentRef,
    Answer,
    DatasetRef,
    Read,
    RunRecord,
    ToolRef,
    record_run,
)
from api.tests.events import run_event

T0 = datetime(2026, 11, 3, 6, 0, tzinfo=UTC)
WAREHOUSE = "mcp://entrepot"
CRM = "mcp://crm"
RATES = DatasetRef("bigquery", "finance.ref.taux_change")


def minutes(count: float) -> datetime:
    return T0 + timedelta(minutes=count)


def instant(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def period(start: float, end: float) -> dict[str, str]:
    return {"from": instant(minutes(start)), "to": instant(minutes(end))}


# Le calcul d'un émetteur, sans base.


def report(
    start: float, end: float, *, seen: int = 0, found: int | None = None, **counts: int
) -> Report:
    return Report(
        emitter=WAREHOUSE,
        start=minutes(start),
        end=minutes(end),
        calls_seen=seen,
        events_emitted=counts.get("emitted", seen),
        events_lost=counts.get("lost", 0),
        uninspected=counts.get("uninspected", 0),
        in_graph=seen if found is None else found,
    )


def measured(
    reports: list[Report],
    start: float,
    end: float,
    *,
    known_before: bool = False,
    now: float = 10_000,
) -> tuple[str, list[tuple[str, float, float, int | None]]]:
    status, tally = coverage._measure(
        WAREHOUSE,
        reports,
        known_before=known_before,
        start=minutes(start),
        end=minutes(end),
        now=minutes(now),
    )
    holes: list[tuple[str, float, float, int | None]] = [
        (
            hole.kind,
            (hole.window.start - T0) / timedelta(minutes=1),
            (hole.window.end - T0) / timedelta(minutes=1),
            hole.count,
        )
        for hole in tally.holes
    ]
    return status, holes


def test_a_period_covered_by_reports_without_loss_is_measured_without_hole() -> None:
    assert measured([report(0, 1, seen=4), report(1, 2), report(2, 3, seen=1)], 0, 3) == (
        "measured",
        [],
    )


def test_a_period_without_report_is_unknown_and_a_known_emitter_is_silent() -> None:
    assert measured([], 0, 3) == ("unknown", [])
    assert measured([], 0, 3, known_before=True) == ("unknown", [("silence", 0, 3, None)])


def test_intervals_without_report_are_holes_named_by_what_preceded_them() -> None:
    reports = [report(2, 3), report(5, 6)]
    assert measured(reports, 0, 8) == (
        "partial",
        [
            ("before_first_report", 0, 2, None),
            ("silence", 3, 5, None),
            ("silence", 6, 8, None),
        ],
    )
    assert measured(reports, 0, 8, known_before=True)[1][0] == ("silence", 0, 2, None)


def test_what_is_not_yet_past_is_neither_covered_nor_a_hole() -> None:
    assert measured([report(0, 2)], 0, 10, now=2) == ("measured", [])
    assert measured([report(0, 2)], 0, 10, now=3) == ("partial", [("silence", 2, 3, None)])


def test_holes_of_one_kind_that_follow_each_other_are_merged() -> None:
    reports = [
        report(0, 1, seen=10, lost=2, emitted=8, found=8),
        report(1, 2, seen=5, lost=1, emitted=4, found=4),
        report(3, 4, seen=5, lost=1, emitted=4, found=4),
    ]
    _, holes = measured(reports, 0, 4)
    assert holes == [
        ("events_lost", 0, 2, 3),
        ("missing_from_graph", 0, 2, 3),
        ("silence", 2, 3, None),
        ("events_lost", 3, 4, 1),
        ("missing_from_graph", 3, 4, 1),
    ]


def test_events_beyond_the_calls_seen_never_raise_the_rate_above_one() -> None:
    status, tally = coverage._measure(
        WAREHOUSE,
        [report(0, 1, seen=3, found=3), report(1, 2, seen=4, found=2)],
        known_before=False,
        start=minutes(0),
        end=minutes(2),
        now=minutes(10),
    )
    # Une fenêtre en excès ne comble pas le manque d'une autre.
    assert (tally.calls_seen, tally.events_in_graph) == (7, 5)
    assert status == "measured"


# Par l'API, sur un PostgreSQL réel.


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(engine))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield client


async def publish(
    client: httpx.AsyncClient, emitter: str, start: float, end: float, **counts: int
) -> None:
    seen = counts.get("seen", 0)
    body = {
        "emitter": emitter,
        "window": period(start, end),
        "calls_seen": seen,
        "events_emitted": counts.get("emitted", seen - counts.get("lost", 0)),
        "events_lost": counts.get("lost", 0),
        "uninspected": counts.get("uninspected", 0),
    }
    response = await client.post("/api/v1/coverage", json=body)
    assert response.status_code == 201, response.text


def call_event(emitter: str, at: datetime, rank: int, *, tool_call: bool = True) -> dict[str, Any]:
    event = copy.deepcopy(run_event(rank))
    event["eventTime"] = at.isoformat(timespec="microseconds").replace("+00:00", "Z")
    event["job"]["namespace"] = emitter
    if not tool_call:
        del event["run"]["facets"]["answerlineage_toolCall"]
    return event


async def receive(
    engine: AsyncEngine, events: list[dict[str, Any]], *, projected: bool = True
) -> None:
    """Reçoit des événements ; projetés, ils sortent de la file comme après un worker."""
    accepted = []
    for raw in events:
        event, errors = validate_event(raw)
        assert event is not None, errors
        accepted.append(AcceptedEvent(event_identifier(raw), event, raw))
    async with engine.begin() as conn:
        await store_events(conn, accepted)
        if projected:
            await conn.execute(
                text("DELETE FROM ingest_queue WHERE event_id = ANY(:ids)"),
                {"ids": [item.event_id for item in accepted]},
            )


async def by_emitter(
    client: httpx.AsyncClient, start: float, end: float, **query: Any
) -> dict[str, Any]:
    response = await client.get("/api/v1/coverage", params={**period(start, end), **query})
    assert response.status_code == 200, response.text
    return {item["emitter"]: item for item in response.json()["emitters"]}


def holes(coverage_: dict[str, Any]) -> list[tuple[Any, ...]]:
    return [
        (hole["emitter"], hole["kind"], hole["window"]["from"], hole["window"]["to"], hole["count"])
        for hole in coverage_["holes"]
    ]


@pytest.mark.integration
@pytest.mark.anyio
async def test_the_coverage_counts_the_call_events_received_and_projected_in_each_window(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    await publish(client, WAREHOUSE, 0, 1, seen=10)
    in_window = [call_event(WAREHOUSE, minutes(0.05 * rank), rank) for rank in range(8)]
    await receive(engine, in_window)
    # Reçu mais pas encore projeté : il n'est pas dans le graphe.
    await receive(engine, [call_event(WAREHOUSE, minutes(0.9), 20)], projected=False)
    # Hors de la fenêtre, d'un autre émetteur, ou qui n'est pas un appel d'outil.
    await receive(
        engine,
        [
            call_event(WAREHOUSE, minutes(1), 21),
            call_event(CRM, minutes(0.5), 22),
            call_event(WAREHOUSE, minutes(0.5), 23, tool_call=False),
        ],
    )

    found = (await by_emitter(client, 0, 1))[WAREHOUSE]
    measure = found["coverage"]
    assert (measure["status"], measure["rate"]) == ("measured", 0.8)
    assert (measure["calls_seen"], measure["events_in_graph"]) == (10, 8)
    assert holes(measure) == [
        (WAREHOUSE, "missing_from_graph", instant(minutes(0)), instant(minutes(1)), 2)
    ]
    assert found["muted_since"] is None
    assert found["last_report_at"] is not None


@pytest.mark.integration
@pytest.mark.anyio
async def test_uninspected_requests_keep_the_rate_and_show_as_a_hole_of_their_own(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    await publish(client, WAREHOUSE, 0, 1, seen=2, uninspected=3)
    await receive(engine, [call_event(WAREHOUSE, minutes(0.5), rank) for rank in range(2)])

    measure = (await by_emitter(client, 0, 1))[WAREHOUSE]["coverage"]
    assert (measure["status"], measure["rate"], measure["uninspected"]) == ("measured", 1.0, 3)
    assert holes(measure) == [
        (WAREHOUSE, "uninspected", instant(minutes(0)), instant(minutes(1)), 3)
    ]


@pytest.mark.integration
@pytest.mark.anyio
async def test_every_known_emitter_is_listed_with_its_own_coverage(
    client: httpx.AsyncClient,
) -> None:
    await publish(client, WAREHOUSE, 0, 1)
    await publish(client, WAREHOUSE, 2, 3)
    await publish(client, CRM, 0, 3)

    found = await by_emitter(client, 0, 3)
    assert list(found) == [CRM, WAREHOUSE]
    assert found[CRM]["coverage"]["status"] == "measured"
    assert found[CRM]["coverage"]["rate"] is None
    assert found[WAREHOUSE]["coverage"]["status"] == "partial"
    assert holes(found[WAREHOUSE]["coverage"]) == [
        (WAREHOUSE, "silence", instant(minutes(1)), instant(minutes(2)), None)
    ]
    # Une période où aucun relevé ne tombe : non mesurée, et le silence se dit.
    later = await by_emitter(client, 10, 20)
    assert later[CRM]["coverage"]["status"] == "unknown"
    assert holes(later[CRM]["coverage"]) == [
        (CRM, "silence", instant(minutes(10)), instant(minutes(20)), None)
    ]
    # Une période qui précède tout relevé n'est pas mesurée, sans trou à signaler.
    earlier = await by_emitter(client, -10, -5)
    assert earlier[CRM]["coverage"]["status"] == "unknown"
    assert earlier[CRM]["coverage"]["holes"] == []

    response = await client.get("/api/v1/coverage", params={**period(0, 3), "limit": 1})
    assert [item["emitter"] for item in response.json()["emitters"]] == [CRM]
    assert response.json()["more"] is True


@pytest.mark.integration
@pytest.mark.anyio
async def test_an_emitter_signalled_silent_shows_since_when_until_it_publishes_again(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    await publish(client, WAREHOUSE, 0, 1)
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE coverage_emitter SET silent_since = last_report_at"))
    found = (await by_emitter(client, 0, 1))[WAREHOUSE]
    assert found["muted_since"] == found["last_report_at"]

    await publish(client, WAREHOUSE, 1, 2)
    assert (await by_emitter(client, 0, 1))[WAREHOUSE]["muted_since"] is None


@pytest.mark.integration
@pytest.mark.anyio
async def test_a_reading_that_exceeds_its_bound_is_never_given_as_measured(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await publish(client, WAREHOUSE, 0, 1)
    await publish(client, WAREHOUSE, 1, 2)
    monkeypatch.setattr(coverage, "MAX_REPORTS", 1)
    measure = (await by_emitter(client, 0, 1))[WAREHOUSE]["coverage"]
    assert (measure["status"], measure["truncated"]) == ("measured", False)
    measure = (await by_emitter(client, 0, 2))[WAREHOUSE]["coverage"]
    assert (measure["status"], measure["truncated"]) == ("partial", True)


@pytest.mark.integration
@pytest.mark.anyio
@pytest.mark.parametrize(
    ("query", "path", "kind"),
    [
        (period(1, 1), "/query/to", "window_empty"),
        ({"from": instant(minutes(0))}, "/query/to", "missing"),
        ({**period(0, 1), "limit": 0}, "/query/limit", "greater_than_equal"),
        ({"from": "hier", "to": instant(minutes(0))}, "/query/from", "datetime_from_date_parsing"),
    ],
)
async def test_an_invalid_period_is_rejected_without_its_value(
    client: httpx.AsyncClient, query: dict[str, Any], path: str, kind: str
) -> None:
    response = await client.get("/api/v1/coverage", params=query)
    assert response.status_code == 422
    assert [(error["path"], error["type"]) for error in response.json()["errors"]] == [(path, kind)]
    assert "hier" not in response.text


# La couverture dans le résultat d'impact.


async def incident(client: httpx.AsyncClient, start: float, end: float) -> dict[str, Any]:
    response = await client.get(
        "/api/v1/impact",
        params={"namespace": RATES.namespace, "name": RATES.name, **period(start, end)},
    )
    assert response.status_code == 200, response.text
    measure: dict[str, Any] = response.json()["coverage"]
    return measure


@pytest.fixture
async def dataset(engine: AsyncEngine) -> None:
    """Un jeu de données connu du graphe, pour que l'impact réponde."""
    async with engine.begin() as conn:
        await record_run(
            conn,
            RunRecord(
                run_id=uuid4(),
                kind="tool",
                state="COMPLETE",
                event_time=minutes(0),
                tool=ToolRef("entrepot", "execute_sql"),
                reads=(Read(RATES),),
            ),
        )


@pytest.mark.integration
@pytest.mark.anyio
@pytest.mark.usefixtures("dataset")
async def test_an_impact_without_report_says_its_coverage_is_not_measured(
    client: httpx.AsyncClient,
) -> None:
    measure = await incident(client, 0, 60)
    assert measure == {
        "status": "unknown",
        "rate": None,
        "calls_seen": 0,
        "events_in_graph": 0,
        "events_lost": 0,
        "uninspected": 0,
        "holes": [],
        "truncated": False,
    }


@pytest.mark.integration
@pytest.mark.anyio
@pytest.mark.usefixtures("dataset")
async def test_an_impact_gives_the_coverage_and_the_holes_of_its_window_only(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    for start in range(0, 6):
        await publish(client, WAREHOUSE, start, start + 1, seen=2)
        await publish(client, CRM, start, start + 1)
    await receive(
        engine,
        [
            call_event(WAREHOUSE, minutes(start + 0.5), 10 * start + rank)
            for start in range(6)
            for rank in range(2)
        ],
    )
    # Une perte et des requêtes non inspectées, hors de la fenêtre de l'incident.
    await publish(client, WAREHOUSE, 5, 6, seen=2, lost=1, emitted=1)
    await publish(client, CRM, 5, 6, uninspected=4)

    measure = await incident(client, 1, 4)
    assert (measure["status"], measure["rate"], measure["calls_seen"]) == ("measured", 1.0, 6)
    assert measure["holes"] == []

    # Dans la fenêtre, les trous apparaissent avec leur fenêtre et leur nature.
    measure = await incident(client, 1, 6)
    assert (measure["status"], measure["events_lost"], measure["uninspected"]) == (
        "measured",
        1,
        4,
    )
    assert holes(measure) == [
        (CRM, "uninspected", instant(minutes(5)), instant(minutes(6)), 4),
        (WAREHOUSE, "events_lost", instant(minutes(5)), instant(minutes(6)), 1),
    ]


@pytest.mark.integration
@pytest.mark.anyio
@pytest.mark.usefixtures("dataset")
async def test_an_impact_partly_covered_says_partial_and_what_is_missing(
    client: httpx.AsyncClient,
) -> None:
    await publish(client, WAREHOUSE, 0, 2)
    await publish(client, CRM, 0, 1)
    await publish(client, CRM, 3, 4)
    # Un émetteur apparu après la fenêtre n'y compte pas.
    await publish(client, "mcp://nouveau", 30, 31)

    measure = await incident(client, 0, 2)
    assert measure["status"] == "partial"
    assert holes(measure) == [(CRM, "silence", instant(minutes(1)), instant(minutes(2)), None)]
    # Un émetteur connu, muet pendant toute la fenêtre : elle n'est pas couverte.
    measure = await incident(client, 10, 20)
    assert measure["status"] == "unknown"
    assert [hole[:2] for hole in holes(measure)] == [(CRM, "silence"), (WAREHOUSE, "silence")]


# La couverture dans la remontée d'une réponse.

FINGERPRINT = "sha256:" + "ab" * 32


async def answered(engine: AsyncEngine, *calls: tuple[str, float]) -> str:
    """Une réponse rendue à la minute 1, et ses appels d'outils : l'émetteur et la minute de chacun.

    Le graphe et l'archive des événements s'accordent : chaque appel a son run,
    enfant du run de la réponse, et son événement d'appel d'outil.
    """
    run_id = uuid4()
    records = [
        RunRecord(
            run_id=run_id,
            kind="agent",
            state="COMPLETE",
            event_time=minutes(1),
            agent=AgentRef("agent://ventes", "assistant-commercial"),
            answers=(Answer(FINGERPRINT),),
        )
    ]
    events = []
    for rank, (emitter, at) in enumerate(calls):
        call = uuid4()
        records.append(
            RunRecord(
                run_id=call,
                kind="tool",
                state="COMPLETE",
                event_time=minutes(at),
                tool=ToolRef(emitter.removeprefix("mcp://"), "outil"),
                parent_run_id=run_id,
                reads=(Read(RATES),),
            )
        )
        event = call_event(emitter, minutes(at), rank)
        event["run"]["runId"] = str(call)
        events.append(event)
    async with engine.begin() as conn:
        for record in records:
            await record_run(conn, record)
    await receive(engine, events)
    return answer_key(run_id, FINGERPRINT)


async def traced(client: httpx.AsyncClient, answer_id: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/answers/{answer_id}/sources")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


@pytest.mark.integration
@pytest.mark.anyio
async def test_the_trace_of_an_answer_gives_the_period_of_its_run_and_its_coverage(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    answer_id = await answered(engine, (WAREHOUSE, 0.5))
    await publish(client, WAREHOUSE, 0, 2, seen=1)

    found = await traced(client, answer_id)
    # Du premier instant de son arbre de runs à la seconde de la réponse, comprise.
    assert found["period"] == {"from": "2026-11-03T06:00:30Z", "to": "2026-11-03T06:01:01Z"}
    assert found["coverage"] == {
        "status": "measured",
        "rate": 1.0,
        "calls_seen": 1,
        "events_in_graph": 1,
        "events_lost": 0,
        "uninspected": 0,
        "holes": [],
        "truncated": False,
    }


@pytest.mark.integration
@pytest.mark.anyio
async def test_the_trace_counts_only_the_emitters_that_saw_its_runs(
    client: httpx.AsyncClient, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    alone = await answered(engine, (WAREHOUSE, 0.5))
    both = await answered(engine, (CRM, 0.2), (WAREHOUSE, 0.5))
    await publish(client, WAREHOUSE, 0, 2, seen=2)
    # Le CRM se tait de 06:00:36 à 06:05 : un trou pour qui l'a appelé, et pour lui seul.
    await publish(client, CRM, 0, 0.6, seen=1)
    await publish(client, CRM, 5, 6)

    measure = (await traced(client, alone))["coverage"]
    assert (measure["status"], measure["rate"], measure["holes"]) == ("measured", 1.0, [])

    found = await traced(client, both)
    assert found["period"] == {"from": "2026-11-03T06:00:12Z", "to": "2026-11-03T06:01:01Z"}
    measure = found["coverage"]
    assert (measure["status"], measure["rate"], measure["calls_seen"]) == ("partial", 1.0, 3)
    assert holes(measure) == [
        (CRM, "silence", "2026-11-03T06:00:36Z", "2026-11-03T06:01:01Z", None)
    ]

    # Plus d'émetteurs que la borne : la mesure n'est jamais donnée pour complète.
    monkeypatch.setattr(coverage, "MAX_EMITTERS", 1)
    measure = (await traced(client, alone))["coverage"]
    assert (measure["status"], measure["truncated"]) == ("measured", False)
    measure = (await traced(client, both))["coverage"]
    assert (measure["status"], measure["truncated"]) == ("partial", True)


@pytest.mark.integration
@pytest.mark.anyio
async def test_a_trace_without_report_from_the_emitters_of_its_runs_is_not_measured(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    answer_id = await answered(engine, (WAREHOUSE, 0.5))
    assert (await traced(client, answer_id))["coverage"]["status"] == "unknown"

    # Les relevés d'un émetteur qui n'a rien vu de ce run n'y comptent pas.
    await publish(client, CRM, 0, 2)
    measure = (await traced(client, answer_id))["coverage"]
    assert (measure["status"], measure["rate"], measure["holes"]) == ("unknown", None, [])
