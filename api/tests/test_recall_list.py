"""Lire la liste de rappel d'un incident : une entrée par destinataire, par rang, page par page.

Les workers font la liste ; ces tests l'écrivent dans la base comme eux, puis la lisent
par la route.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_api.auth.roles import Role
from answerlineage_api.incidents.models import validate_declaration
from answerlineage_api.incidents.store import declare
from answerlineage_api.lineage.trace import answer_key
from answerlineage_api.recall.models import MAX_ENTRY_ANSWERS
from api.tests.issuer import authentication, signed_in

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

T0 = datetime(2026, 11, 3, 6, 0, tzinfo=UTC)
BUILT = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)


def pseudonym(rank: int) -> str:
    return f"hmac-sha256:{rank:064x}"


def fingerprint(rank: int) -> str:
    return f"sha256:{rank:064x}"


async def declared(engine: AsyncEngine) -> UUID:
    document = {
        "scope": {"datasets": [{"namespace": "bigquery", "name": "finance.ref.taux_change"}]},
        "window": {"from": T0.isoformat(), "to": (T0 + timedelta(days=1)).isoformat()},
        "severity": "critical",
    }
    declaration, errors = validate_declaration(document)
    assert declaration is not None, errors
    async with engine.begin() as conn:
        return (await declare(conn, declaration)).id


async def listed(engine: AsyncEngine, incident_id: UUID, entries: list[tuple[int, int]]) -> UUID:
    """Écrit une liste comme les workers : chaque entrée, son exposition en millionièmes et
    son nombre de réponses, toutes produites par un même run, une heure d'écart chacune."""
    run_id = uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO recall_list (incident_id, built_at, reference_at, recipients, answers)"
                " VALUES (:id, :built, :built, :recipients, :answers)"
            ),
            {
                "id": incident_id,
                "built": BUILT,
                "recipients": len(entries),
                "answers": max((count for _, count in entries), default=0),
            },
        )
        for rank, (exposure, count) in enumerate(entries, start=1):
            await conn.execute(
                text(
                    "INSERT INTO recall_entry (incident_id, pseudonym, rank, exposure, answers,"
                    " latest_answer_at) VALUES (:id, :who, :rank, :exposure, :count, :latest)"
                ),
                {
                    "id": incident_id,
                    "who": pseudonym(rank),
                    "rank": rank,
                    "exposure": exposure,
                    "count": count,
                    "latest": T0 + timedelta(hours=count),
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO recall_entry_answer (incident_id, pseudonym, run_id, fingerprint,"
                    " produced_at)"
                    " SELECT :id, :who, :run, 'sha256:' || lpad(to_hex(n), 64, '0'),"
                    " :t0 + make_interval(hours => n)"
                    " FROM generate_series(1, :count) AS n"
                ),
                {
                    "id": incident_id,
                    "who": pseudonym(rank),
                    "run": run_id,
                    "t0": T0,
                    "count": count,
                },
            )
    return run_id


@pytest.fixture
async def client(engine: AsyncEngine) -> Any:
    app = create_app(engine, authentication=authentication())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http:
        http.headers.update(signed_in(Role.READER))
        yield http


async def test_the_list_gives_each_recipient_once_by_rank_with_its_answers(
    engine: AsyncEngine, client: httpx.AsyncClient
) -> None:
    incident_id = await declared(engine)
    run_id = await listed(engine, incident_id, [(2_500_000, 5), (707_107, 1)])

    answered = await client.get(f"/api/v1/incidents/{incident_id}/recall-list")

    assert answered.status_code == 200
    body = answered.json()
    assert body["incident_id"] == str(incident_id)
    assert body["severity"] == "critical"
    assert body["built_at"] == body["reference_at"] == "2026-11-04T09:00:00Z"
    assert body["counts"] == {"recipients": 2, "answers": 5}
    assert (body["more"], body["next_cursor"]) == (False, None)
    first, second = body["entries"]
    assert (first["rank"], first["recipient"], first["exposure"]) == (1, pseudonym(1), 2.5)
    assert (first["answer_count"], first["more_answers"]) == (5, False)
    assert first["latest_answer_at"] == "2026-11-03T11:00:00Z"
    # Ses cinq réponses, les plus récentes d'abord.
    assert [answer["fingerprint"] for answer in first["answers"]] == [
        fingerprint(n) for n in (5, 4, 3, 2, 1)
    ]
    assert first["answers"][0] == {
        "id": answer_key(run_id, fingerprint(5)),
        "fingerprint": fingerprint(5),
        "produced_at": "2026-11-03T11:00:00Z",
    }
    assert (second["rank"], second["recipient"], second["exposure"]) == (2, pseudonym(2), 0.707107)
    assert len(second["answers"]) == second["answer_count"] == 1


async def test_the_list_is_read_page_by_page_without_loss_or_duplicate(
    engine: AsyncEngine, client: httpx.AsyncClient
) -> None:
    incident_id = await declared(engine)
    await listed(engine, incident_id, [(1_000_000 - rank, 1) for rank in range(5)])

    seen: list[int] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 2} | ({} if cursor is None else {"cursor": cursor})
        page = await client.get(f"/api/v1/incidents/{incident_id}/recall-list", params=params)
        body = page.json()
        seen.extend(entry["rank"] for entry in body["entries"])
        cursor = body["next_cursor"]
        assert body["more"] is (cursor is not None)
        if cursor is None:
            break
    assert seen == [1, 2, 3, 4, 5]


async def test_an_entry_shows_its_most_recent_answers_and_says_there_are_more(
    engine: AsyncEngine, client: httpx.AsyncClient
) -> None:
    incident_id = await declared(engine)
    await listed(engine, incident_id, [(9_000_000, MAX_ENTRY_ANSWERS + 3)])

    body = (await client.get(f"/api/v1/incidents/{incident_id}/recall-list")).json()

    (entry,) = body["entries"]
    assert entry["answer_count"] == MAX_ENTRY_ANSWERS + 3
    assert entry["more_answers"] is True
    assert len(entry["answers"]) == MAX_ENTRY_ANSWERS
    assert entry["answers"][0]["fingerprint"] == fingerprint(MAX_ENTRY_ANSWERS + 3)


async def test_an_empty_list_says_there_is_no_one_to_recall(
    engine: AsyncEngine, client: httpx.AsyncClient
) -> None:
    incident_id = await declared(engine)
    await listed(engine, incident_id, [])

    body = (await client.get(f"/api/v1/incidents/{incident_id}/recall-list")).json()

    assert body["counts"] == {"recipients": 0, "answers": 0}
    assert (body["entries"], body["more"]) == ([], False)


async def test_an_unknown_incident_or_one_without_a_list_is_refused_by_its_code(
    engine: AsyncEngine, client: httpx.AsyncClient
) -> None:
    incident_id = await declared(engine)

    unknown = await client.get(f"/api/v1/incidents/{uuid4()}/recall-list")
    without = await client.get(f"/api/v1/incidents/{incident_id}/recall-list")

    assert (unknown.status_code, unknown.json()) == (404, {"error": "unknown_incident"})
    assert (without.status_code, without.json()) == (404, {"error": "no_recall_list"})


@pytest.mark.parametrize(
    "params", [{"cursor": "<canari>"}, {"cursor": "1" * 10}, {"limit": 0}, {"limit": 201}]
)
async def test_a_malformed_parameter_is_refused_without_being_copied(
    engine: AsyncEngine, client: httpx.AsyncClient, params: dict[str, Any]
) -> None:
    incident_id = await declared(engine)
    await listed(engine, incident_id, [(1, 1)])

    answered = await client.get(f"/api/v1/incidents/{incident_id}/recall-list", params=params)

    assert answered.status_code == 422
    assert "canari" not in answered.text and "1" * 10 not in answered.text
