"""La couverture du lignage de bout en bout : ingestion, workers, relevé, lecture.

Un émetteur a relayé 1 000 appels ; 990 de leurs événements arrivent dans le
graphe par le chemin réel. La couverture lue vaut 99 %, et la fenêtre de la
perte est indiquée, que l'émetteur ait vu la perte ou non.

Puis l'entreprise synthétique, dont un serveur se tait une heure : chaque
analyse qui porte sur cette heure, propagation d'un incident ou remontée d'une
réponse, dit sa couverture et la période du trou.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_api.lineage.trace import answer_key
from answerlineage_corpus.coverage import coverage_reports
from answerlineage_corpus.enterprise import Enterprise, Request, generate
from answerlineage_workers.queue import process_batch
from api.tests.events import run_event
from api.tests.issuer import authentication, signed_in
from tests.test_corpus_to_graph import load

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

HOUR = timedelta(hours=1)
SECOND = timedelta(seconds=1)
# Le serveur qui se tait, et l'émetteur qui le représente : le proxy placé devant lui.
SILENT = "crm"
SILENT_EMITTER = "mcp://crm"

WINDOW = {"from": "2026-11-02T09:00:00Z", "to": "2026-11-02T09:01:00Z"}
INCIDENT = {
    "namespace": "bigquery",
    "name": "finance.ref.taux_change",
    "from": "2026-11-02T08:00:00Z",
    "to": "2026-11-02T09:01:00Z",
}


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(engine, authentication=authentication()))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://localhost", headers=signed_in()
    ) as client:
        yield client


async def deliver_990_calls(client: httpx.AsyncClient, engine: AsyncEngine) -> None:
    """990 appels de ``mcp://entrepot`` dans la fenêtre, projetés par les workers."""
    events = [run_event(index) for index in range(990)]
    assert {event["job"]["namespace"] for event in events} == {"mcp://entrepot"}
    assert all(event["eventTime"].startswith("2026-11-02T09:00:") for event in events)
    response = await client.post("/api/v1/lineage/batch", json=events)
    assert response.json() == {"accepted": 990, "duplicates": 0, "rejected": []}
    while not (await process_batch(engine)).empty:
        pass


async def publish(client: httpx.AsyncClient, *, emitted: int, lost: int) -> None:
    report = {
        "emitter": "mcp://entrepot",
        "window": WINDOW,
        "calls_seen": 1000,
        "events_emitted": emitted,
        "events_lost": lost,
    }
    assert (await client.post("/api/v1/coverage", json=report)).status_code == 201


async def read(client: httpx.AsyncClient) -> tuple[dict[str, Any], dict[str, Any]]:
    """La couverture de l'émetteur, et celle du résultat d'impact sur la même période."""
    response = await client.get("/api/v1/coverage", params=WINDOW)
    [emitter] = response.json()["emitters"]
    impact = await client.get("/api/v1/impact", params=INCIDENT)
    assert impact.status_code == 200
    return emitter["coverage"], impact.json()["coverage"]


def holes(coverage: dict[str, Any]) -> list[tuple[str, dict[str, str], int | None]]:
    return [(hole["kind"], hole["window"], hole["count"]) for hole in coverage["holes"]]


async def test_1000_calls_with_10_events_lost_give_99_percent_and_the_window_of_the_loss(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    await deliver_990_calls(client, engine)
    await publish(client, emitted=990, lost=10)

    by_emitter, in_impact = await read(client)
    assert by_emitter["status"] == "measured"
    assert by_emitter["rate"] == 0.99
    assert (by_emitter["calls_seen"], by_emitter["events_in_graph"]) == (1000, 990)
    assert holes(by_emitter) == [("events_lost", WINDOW, 10), ("missing_from_graph", WINDOW, 10)]
    # Le résultat d'impact porte la même mesure : sa fenêtre commence avant le premier
    # relevé, il dit donc aussi que ce début n'est pas mesuré.
    assert in_impact["rate"] == 0.99
    assert in_impact["status"] == "partial"
    assert holes(in_impact) == [
        ("before_first_report", {"from": INCIDENT["from"], "to": WINDOW["from"]}, None),
        ("events_lost", WINDOW, 10),
        ("missing_from_graph", WINDOW, 10),
    ]


async def test_events_lost_on_the_way_show_even_when_the_emitter_believes_it_sent_them(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    await deliver_990_calls(client, engine)
    await publish(client, emitted=1000, lost=0)

    by_emitter, in_impact = await read(client)
    assert by_emitter["rate"] == in_impact["rate"] == 0.99
    assert holes(by_emitter) == [("missing_from_graph", WINDOW, 10)]
    assert ("missing_from_graph", WINDOW, 10) in holes(in_impact)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def hour_of(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


def calls_between(enterprise: Enterprise, server: str, start: datetime, end: datetime) -> int:
    return sum(
        1
        for request in enterprise.requests
        for call in request.calls
        if call.tool.server == server and start <= call.time < end
    )


def silenced(enterprise: Enterprise, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Les relevés de l'entreprise, sauf ceux du serveur muet de ``start`` à ``end``.

    Son relevé du jour est coupé en deux autour de l'heure muette, chaque moitié
    avec les appels qu'il a vus.
    """
    reports = []
    for report in coverage_reports(enterprise):
        day_start, day_end = (instant(report["window"][bound]) for bound in ("from", "to"))
        if report["emitter"] != SILENT_EMITTER or not day_start <= start < day_end:
            reports.append(report)
            continue
        assert end <= day_end
        for first, last in ((day_start, start), (end, day_end)):
            seen = calls_between(enterprise, SILENT, first, last)
            reports.append(
                {
                    **report,
                    "window": {"from": iso(first), "to": iso(last)},
                    "calls_seen": seen,
                    "events_emitted": seen,
                }
            )
    return reports


async def a_silent_hour(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> tuple[Request, datetime, datetime]:
    """L'entreprise chargée par le chemin réel, et ses relevés, sauf une heure muette.

    L'heure muette est celle de la première réponse qui a appelé le serveur muet.
    """
    enterprise = generate(7, requests=60, days=5, incidents=2)
    await load(engine, enterprise)
    target = min(
        (
            request
            for request in enterprise.requests
            if request.answer is not None
            and any(call.tool.server == SILENT for call in request.calls)
        ),
        key=lambda request: request.started_at,
    )
    start, end = hour_of(target.started_at), hour_of(target.ended_at) + HOUR
    for report in silenced(enterprise, start, end):
        assert (await client.post("/api/v1/coverage", json=report)).status_code == 201
    return target, start, end


async def test_a_hole_in_the_window_of_an_incident_shows_in_its_result_with_its_period(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Étant donné un trou de lignage dans la fenêtre, quand je propage, alors le résultat
    indique la couverture et la période du trou."""
    target, start, end = await a_silent_hour(client, engine)
    assert target.answer is not None
    table = next(
        read.table for call in target.calls if call.tool.server == SILENT for read in call.reads
    )

    response = await client.get(
        "/api/v1/impact",
        params={
            "namespace": table.namespace,
            "name": table.name,
            "from": iso(start - HOUR),
            "to": iso(end + HOUR),
        },
    )
    assert response.status_code == 200
    result = response.json()
    returned = {answer["fingerprint"] for answer in result["graph"]["answers"]}
    assert target.answer.fingerprint in returned
    coverage = result["coverage"]
    # Tout ce que les relevés ont vu est dans le graphe ; l'heure muette n'est pas couverte.
    assert (coverage["status"], coverage["rate"]) == ("partial", 1.0)
    assert coverage["holes"] == [
        {
            "emitter": SILENT_EMITTER,
            "kind": "silence",
            "window": {"from": iso(start), "to": iso(end)},
            "count": None,
        }
    ]


async def test_the_trace_of_an_answer_says_the_hole_over_the_period_of_its_run(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    target, _, _ = await a_silent_hour(client, engine)
    assert target.answer is not None
    moments = [target.started_at, target.ended_at, *(call.time for call in target.calls)]

    response = await client.get(
        f"/api/v1/answers/{answer_key(target.run_id, target.answer.fingerprint)}/sources"
    )
    assert response.status_code == 200
    found = response.json()
    period = {
        "from": iso(min(moments).replace(microsecond=0)),
        "to": iso(max(moments).replace(microsecond=0) + SECOND),
    }
    assert found["period"] == period
    coverage = found["coverage"]
    # Les autres serveurs qu'elle a appelés ont publié ; le serveur muet n'a rien couvert.
    others = {call.tool.server for call in target.calls} - {SILENT}
    assert coverage["status"] == ("partial" if others else "unknown")
    assert coverage["holes"] == [
        {"emitter": SILENT_EMITTER, "kind": "silence", "window": period, "count": None}
    ]
