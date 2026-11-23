"""Plusieurs instances d'un émetteur : des proxys placés devant un même serveur.

Ils partagent l'identifiant d'émetteur, l'espace de noms de leurs jobs. Chacun
publie ses relevés sous son identifiant d'instance : deux relevés d'une même
fenêtre ne se confondent plus, l'API les additionne pour mesurer l'émetteur, et
le silence d'une seule instance n'est pas celui de l'émetteur.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_workers.silence import check_silent_emitters
from api.tests.issuer import authentication, signed_in
from api.tests.test_coverage_read import call_event, receive

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

EMITTER = "mcp://entrepot"
MINUTE = timedelta(minutes=1)
# Une heure passée, quelle que soit l'heure du test, alignée sur la minute.
T0 = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(hours=2)


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def report(
    instance: str | None, start: float, end: float, *, seen: int = 0, lost: int = 0
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "emitter": EMITTER,
        "window": {"from": iso(T0 + start * MINUTE), "to": iso(T0 + end * MINUTE)},
        "calls_seen": seen,
        "events_emitted": seen - lost,
        "events_lost": lost,
        "uninspected": 0,
    }
    if instance is not None:
        body["instance"] = instance
    return body


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(engine, authentication=authentication()))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://localhost", headers=signed_in()
    ) as client:
        yield client


async def publish(client: httpx.AsyncClient, *bodies: dict[str, Any]) -> list[dict[str, Any]]:
    answers = []
    for body in bodies:
        response = await client.post("/api/v1/coverage", json=body)
        assert response.status_code == 201, response.text
        answers.append(response.json())
    return answers


async def rows(engine: AsyncEngine) -> list[tuple[Any, ...]]:
    async with engine.connect() as conn:
        found = await conn.execute(
            text(
                "SELECT instance, window_start, window_end, calls_seen, events_emitted,"
                " events_lost FROM coverage_report ORDER BY instance, window_start"
            )
        )
        return [tuple(row) for row in found]


async def coverage(client: httpx.AsyncClient, start: float, end: float) -> dict[str, Any]:
    response = await client.get(
        "/api/v1/coverage", params={"from": iso(T0 + start * MINUTE), "to": iso(T0 + end * MINUTE)}
    )
    assert response.status_code == 200, response.text
    [emitter] = response.json()["emitters"]
    found: dict[str, Any] = emitter["coverage"]
    return found


def holes(found: dict[str, Any]) -> list[tuple[str, str, str, int | None]]:
    return [
        (hole["kind"], hole["window"]["from"], hole["window"]["to"], hole["count"])
        for hole in found["holes"]
    ]


async def test_two_instances_of_one_window_no_longer_merge_whatever_the_order_or_replay(
    engine: AsyncEngine,
) -> None:
    first, second, anonymous = (
        report("a", 0, 1, seen=10),
        report("b", 0, 1, seen=5),
        report(None, 0, 1, seen=2),
    )
    corrected = report("a", 0, 1, seen=10, lost=1)
    orders = [
        [first, second, anonymous, corrected],
        [corrected, anonymous, second, first],
        [second, first, corrected, second, anonymous, first, corrected],
    ]
    states = []
    for order in orders:
        transport = httpx.ASGITransport(app=create_app(engine, authentication=authentication()))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost", headers=signed_in()
        ) as client:
            answers = await publish(client, *order)
        # La réponse rend l'état de la fenêtre de cette instance ; un relevé sans instance
        # n'en reçoit pas.
        assert {answer.get("instance") for answer in answers} == {"a", "b", None}
        states.append(await rows(engine))
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE coverage_report, coverage_emitter"))
    assert states[0] == states[1] == states[2]
    assert [(row[0], row[3], row[4], row[5]) for row in states[0]] == [
        ("", 2, 2, 0),
        ("a", 10, 10, 1),
        ("b", 5, 5, 0),
    ]


async def test_the_coverage_of_the_emitter_adds_its_instances_up(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    # L'instance « b » a démarré au milieu de la première fenêtre.
    await publish(
        client,
        report("a", 0, 1, seen=10, lost=1),
        report("a", 1, 2, seen=4),
        report("b", 0.5, 1, seen=5),
        report("b", 1, 2, seen=6),
    )
    events = [call_event(EMITTER, T0 + 0.05 * rank * MINUTE, rank) for rank in range(14)]
    events += [
        call_event(EMITTER, T0 + (1 + 0.05 * rank) * MINUTE, 100 + rank) for rank in range(10)
    ]
    await receive(engine, events)

    found = await coverage(client, 0, 2)
    assert (found["status"], found["instances"]) == ("measured", 2)
    assert (found["calls_seen"], found["events_in_graph"], found["events_lost"]) == (25, 24, 1)
    # Les fenêtres des deux instances qui se recoupent font une seule fenêtre de mesure.
    window = (f"{T0:%Y-%m-%dT%H:%M:%SZ}", f"{T0 + MINUTE:%Y-%m-%dT%H:%M:%SZ}")
    assert holes(found) == [("events_lost", *window, 1), ("missing_from_graph", *window, 1)]


async def test_a_report_without_instance_counts_as_an_instance_of_its_own(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    await publish(client, report("a", 0, 1, seen=3), report(None, 0, 1, seen=2))
    await receive(engine, [call_event(EMITTER, T0 + 0.1 * MINUTE, rank) for rank in range(5)])
    found = await coverage(client, 0, 1)
    assert (found["instances"], found["calls_seen"], found["rate"]) == (2, 5, 1.0)


async def test_the_silence_of_a_single_instance_is_not_the_silence_of_its_emitter(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    # « b » s'est arrêté il y a vingt minutes, après sa première fenêtre : une mise à
    # l'échelle. « a » continue de publier.
    await publish(client, report("b", 0, 1))
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE coverage_emitter SET last_report_at = last_report_at - interval '20 min'")
        )
    await publish(client, *(report("a", minute, minute + 1) for minute in range(4)))

    found = await coverage(client, 0, 4)
    assert (found["status"], found["instances"], found["holes"]) == ("measured", 2, [])
    assert (await coverage(client, 2, 4))["instances"] == 1
    # Le contrôle des émetteurs muets suit l'émetteur : tant qu'une instance publie, rien.
    now = datetime.now(UTC)
    assert await check_silent_emitters(engine, now=now) == []
    # Quand aucune ne publie plus, l'émetteur est muet, une fois.
    signalled = await check_silent_emitters(engine, now=now + 11 * MINUTE)
    assert [item.emitter for item in signalled] == [EMITTER]
    assert await check_silent_emitters(engine, now=now + 12 * MINUTE) == []
