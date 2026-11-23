"""La file de rebut : la consulter, la rejouer.

Les événements y entrent par le chemin des workers (``bury``), depuis la file.
La liste est bornée, paginée et filtrable ; le rejeu remet en file, en nombre
borné, ce que les filtres désignent. Aucune réponse ne recopie un contenu
d'événement ni une valeur reçue.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from answerlineage_api.app import create_app
from answerlineage_api.ingest import dead_letters
from answerlineage_api.ingest.dead_letters import bury
from answerlineage_api.ingest.models import validate_event
from answerlineage_api.ingest.store import AcceptedEvent, event_identifier, store_events
from api.tests.events import mutated, run_event
from api.tests.issuer import authentication, signed_in

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

LIST = "/api/v1/ingestion/dead-letters"
REPLAY = "/api/v1/ingestion/dead-letters/replay"
CANARY = "canari3b8e"
REASONS = [
    "invalid_identifier",
    "invalid_event",
    "constraint_violation",
    "invalid_value",
    "database_error",
    "projection_error",
]


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(engine, authentication=authentication()))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://localhost", headers=signed_in()
    ) as client:
        yield client


def event_of(index: int, namespace: str = "mcp://entrepot") -> dict[str, Any]:
    """Un appel d'outil dont la table lue porte le canari : un contenu, qui ne doit fuir nulle
    part."""
    event = mutated(["job", "namespace"], namespace, base=run_event(index))
    event["inputs"][0]["name"] = f"finance.{CANARY}"
    return event  # type: ignore[no-any-return]


async def ingest(engine: AsyncEngine, payloads: Sequence[dict[str, Any]]) -> list[str]:
    accepted = []
    for payload in payloads:
        event, errors = validate_event(payload)
        assert event is not None, errors
        accepted.append(AcceptedEvent(event_identifier(payload), event, payload))
    async with engine.begin() as conn:
        await store_events(conn, accepted)
    return [item.event_id for item in accepted]


async def to_dead_letters(
    engine: AsyncEngine, event_ids: Sequence[str], reason: str, *, together: bool = False
) -> None:
    """Met ces événements au rebut comme un worker : un par transaction, ou tous dans une."""

    async def one(conn: Any, event_id: str) -> None:
        found = await conn.execute(
            text(
                "SELECT q.id, e.emitter FROM ingest_queue q"
                " JOIN event_raw e USING (event_id, recorded_month) WHERE q.event_id = :id"
            ),
            {"id": event_id},
        )
        queue_id, emitter = found.one()
        await bury(conn, queue_id=queue_id, emitter=emitter, reason=reason)  # type: ignore[arg-type]

    if together:
        async with engine.begin() as conn:
            for event_id in event_ids:
                await one(conn, event_id)
        return
    for event_id in event_ids:
        async with engine.begin() as conn:
            await one(conn, event_id)


async def scalar(engine: AsyncEngine, sql: str) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql))).scalar_one()


def counts(document: dict[str, Any]) -> dict[str, int]:
    return {item["reason"]: item["count"] for item in document["counts"]}


async def test_the_dead_letters_say_reason_emitter_attempts_and_times_without_content(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    first, second, healthy = await ingest(
        engine, [event_of(0), event_of(1, "mcp://crm"), event_of(2)]
    )
    await to_dead_letters(engine, [first], "constraint_violation")
    await to_dead_letters(engine, [second], "invalid_identifier")

    response = await client.get(LIST)

    assert response.status_code == 200
    document = response.json()
    assert [
        (item["event_id"], item["emitter"], item["reason"]) for item in document["dead_letters"]
    ] == [
        (second, "mcp://crm", "invalid_identifier"),
        (first, "mcp://entrepot", "constraint_violation"),
    ]
    one = document["dead_letters"][1]
    assert one["run_id"] == run_event(0)["run"]["runId"]
    assert one["attempts"] == 1
    assert one["first_failed_at"] == one["last_failed_at"]
    assert one["received_at"] <= one["first_failed_at"]
    # Chaque motif de la liste fermée, dans son ordre, zéro compris.
    assert [item["reason"] for item in document["counts"]] == REASONS
    assert counts(document) == dict.fromkeys(REASONS, 0) | {
        "constraint_violation": 1,
        "invalid_identifier": 1,
    }
    assert (document["truncated"], document["more"], document["next_cursor"]) == (
        False,
        False,
        None,
    )
    # Le rebut désigne l'événement, il n'en recopie rien.
    assert CANARY not in response.text
    assert healthy not in response.text


async def test_the_list_filters_by_emitter_and_reason_and_counts_follow_the_emitter(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    ids = await ingest(
        engine,
        [event_of(0, "mcp://société"), event_of(1, "mcp://société"), event_of(2, "mcp://crm")],
    )
    await to_dead_letters(engine, ids[:1], "invalid_event")
    await to_dead_letters(engine, ids[1:], "database_error")

    # L'émetteur se donne sous l'une ou l'autre écriture Unicode : le rebut garde sa forme NFC.
    decomposed = "mcp://société"
    found = (await client.get(LIST, params={"emitter": decomposed})).json()
    assert {item["event_id"] for item in found["dead_letters"]} == set(ids[:2])
    assert counts(found) == dict.fromkeys(REASONS, 0) | {"invalid_event": 1, "database_error": 1}

    narrowed = (
        await client.get(LIST, params={"emitter": decomposed, "reason": "database_error"})
    ).json()
    assert [item["event_id"] for item in narrowed["dead_letters"]] == [ids[1]]
    # Les décomptes disent la répartition des motifs de l'émetteur, filtre de motif à part.
    assert counts(narrowed) == counts(found)

    by_reason = (await client.get(LIST, params={"reason": "database_error"})).json()
    assert {item["event_id"] for item in by_reason["dead_letters"]} == set(ids[1:])
    assert sum(counts(by_reason).values()) == 3


async def test_pages_follow_one_another_without_loss_or_repeat(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    ids = await ingest(engine, [event_of(index) for index in range(7)])
    # Cinq au rebut dans la même transaction, donc à la même heure : l'identifiant départage.
    await to_dead_letters(engine, ids[:5], "invalid_event", together=True)
    await to_dead_letters(engine, ids[5:], "invalid_event")

    seen: list[str] = []
    cursor = None
    for _ in range(10):
        params = {"limit": "2"} | ({} if cursor is None else {"cursor": cursor})
        page = (await client.get(LIST, params=params)).json()
        seen += [item["event_id"] for item in page["dead_letters"]]
        assert page["more"] == (page["next_cursor"] is not None)
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == [*reversed(ids[5:]), *sorted(ids[:5], reverse=True)]


async def test_counts_beyond_their_bound_are_minimums(
    client: httpx.AsyncClient, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dead_letters, "MAX_COUNTED", 2)
    ids = await ingest(engine, [event_of(index) for index in range(4)])
    await to_dead_letters(engine, ids, "projection_error", together=True)

    document = (await client.get(LIST, params={"limit": "1"})).json()

    assert document["truncated"] is True
    assert 2 < counts(document)["projection_error"] <= 4
    assert len(document["dead_letters"]) == 1


@pytest.mark.parametrize(
    ("params", "path", "code"),
    [
        ({"emitter": f"mcp://entrepot {CANARY}"}, "/query/emitter", "identifier_whitespace"),
        ({"emitter": f"<script>{CANARY}"}, "/query/emitter", "identifier_markup"),
        ({"emitter": f"{CANARY}​"}, "/query/emitter", "identifier_invisible"),
        ({"emitter": "x" * 513}, "/query/emitter", "string_too_long"),
        ({"reason": CANARY}, "/query/reason", "literal_error"),
        ({"limit": "0"}, "/query/limit", "greater_than_equal"),
        ({"limit": "501"}, "/query/limit", "less_than_equal"),
        ({"cursor": CANARY}, "/query/cursor", "string_pattern_mismatch"),
        ({"cursor": f"999999999999999999.{'0' * 64}"}, "/query/cursor", "cursor_invalid"),
    ],
    ids=[
        "emetteur-phrase",
        "emetteur-balise",
        "emetteur-invisible",
        "emetteur-trop-long",
        "motif-inconnu",
        "limite-nulle",
        "limite-trop-grande",
        "curseur-mal-forme",
        "curseur-hors-du-temps",
    ],
)
async def test_an_invalid_parameter_is_refused_without_its_value(
    client: httpx.AsyncClient, params: dict[str, str], path: str, code: str
) -> None:
    response = await client.get(LIST, params=params)
    assert response.status_code == 422
    assert [(error["path"], error["type"]) for error in response.json()["errors"]] == [(path, code)]
    assert CANARY not in response.text


async def test_a_replay_puts_what_the_filters_name_back_in_the_queue(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    ids = await ingest(
        engine, [event_of(0), event_of(1, "mcp://crm"), event_of(2, "mcp://crm"), event_of(3)]
    )
    await to_dead_letters(engine, ids[:3], "constraint_violation")
    await to_dead_letters(engine, ids[3:], "invalid_event")

    response = await client.post(REPLAY, json={"emitter": "mcp://crm"})

    assert (response.status_code, response.json()) == (202, {"replayed": 2, "more": False})
    async with engine.connect() as conn:
        queued = await conn.execute(
            text("SELECT event_id, attempts, first_failed_at FROM ingest_queue ORDER BY id")
        )
        # Remis en file sans essai : un nouvel échec ouvre un nouvel épisode.
        assert [tuple(row) for row in queued] == [(ids[1], 0, None), (ids[2], 0, None)]
        left = await conn.execute(text("SELECT event_id FROM dead_letter ORDER BY event_id"))
        assert left.scalars().all() == sorted([ids[0], ids[3]])

    by_reason = await client.post(REPLAY, json={"reason": "invalid_event"})
    assert by_reason.json() == {"replayed": 1, "more": False}
    by_id = await client.post(REPLAY, json={"event_ids": [ids[0], "f" * 64]})
    assert by_id.json() == {"replayed": 1, "more": False}
    assert await scalar(engine, "SELECT count(*) FROM dead_letter") == 0
    # Un rejeu de plus ne trouve rien, et ne change rien.
    assert (await client.post(REPLAY, json={})).json() == {"replayed": 0, "more": False}
    assert await scalar(engine, "SELECT count(*) FROM ingest_queue") == 4


async def test_a_replay_moves_at_most_its_limit_the_oldest_failures_first(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    ids = await ingest(engine, [event_of(index) for index in range(3)])
    await to_dead_letters(engine, ids, "database_error")

    assert (await client.post(REPLAY, json={"limit": 2})).json() == {"replayed": 2, "more": True}
    async with engine.connect() as conn:
        queued = await conn.execute(text("SELECT event_id FROM ingest_queue ORDER BY id"))
        assert queued.scalars().all() == ids[:2]
    assert (await client.post(REPLAY, json={"limit": 2})).json() == {"replayed": 1, "more": False}


async def test_concurrent_replays_put_each_event_back_once(
    database: URL, engine: AsyncEngine
) -> None:
    ids = await ingest(engine, [event_of(index) for index in range(40)])
    await to_dead_letters(engine, ids, "invalid_value", together=True)
    # Une connexion par requête : les deux rejeux tiennent chacun leur transaction.
    pooled = create_async_engine(database, poolclass=NullPool, hide_parameters=True)
    try:
        transport = httpx.ASGITransport(app=create_app(pooled, authentication=authentication()))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost", headers=signed_in()
        ) as client:
            first, second = await asyncio.gather(
                client.post(REPLAY, json={"limit": 40}), client.post(REPLAY, json={"limit": 40})
            )
    finally:
        await pooled.dispose()
    assert first.status_code == second.status_code == 202
    assert first.json()["replayed"] + second.json()["replayed"] == 40
    assert await scalar(engine, "SELECT count(*) FROM ingest_queue") == 40
    assert await scalar(engine, "SELECT count(DISTINCT event_id) FROM ingest_queue") == 40
    assert await scalar(engine, "SELECT count(*) FROM dead_letter") == 0


@pytest.mark.parametrize(
    ("body", "path", "code"),
    [
        ({f"emiter{CANARY}": "mcp://crm"}, "", "unknown_field"),
        ([CANARY], "", "not_an_object"),
        ({"emitter": f"mcp://crm {CANARY}"}, "/emitter", "identifier_whitespace"),
        ({"emitter": 3}, "/emitter", "string_type"),
        ({"reason": CANARY}, "/reason", "literal_error"),
        ({"event_ids": [CANARY]}, "/event_ids/0", "string_pattern_mismatch"),
        ({"event_ids": ["f" * 64] * 1001}, "/event_ids", "too_many_items"),
        ({"event_ids": []}, "/event_ids", "too_few_items"),
        ({"event_ids": "f" * 64}, "/event_ids", "list_type"),
        ({"limit": 0}, "/limit", "greater_than_equal"),
        ({"limit": 10_001}, "/limit", "less_than_equal"),
        ({"limit": True}, "/limit", "int_type"),
        ({"limit": "5"}, "/limit", "int_type"),
    ],
    ids=[
        "champ-inconnu",
        "pas-un-objet",
        "emetteur-phrase",
        "emetteur-nombre",
        "motif-inconnu",
        "identifiant-mal-forme",
        "trop-d-identifiants",
        "aucun-identifiant",
        "identifiants-hors-liste",
        "limite-nulle",
        "limite-trop-grande",
        "limite-booleenne",
        "limite-en-texte",
    ],
)
async def test_an_invalid_replay_is_refused_without_its_value_and_replays_nothing(
    client: httpx.AsyncClient, engine: AsyncEngine, body: Any, path: str, code: str
) -> None:
    ids = await ingest(engine, [event_of(0)])
    await to_dead_letters(engine, ids, "invalid_event")

    response = await client.post(REPLAY, json=body)

    assert response.status_code == 422
    assert (path, code) in [(error["path"], error["type"]) for error in response.json()["errors"]]
    assert CANARY not in response.text
    assert "1001" not in response.text
    assert await scalar(engine, "SELECT count(*) FROM dead_letter") == 1


async def test_a_replay_body_is_read_bounded(client: httpx.AsyncClient) -> None:
    invalid = await client.post(REPLAY, content=b"{", headers={"content-type": "application/json"})
    assert (invalid.status_code, invalid.json()) == (400, {"error": "invalid_json"})
    large = await client.post(
        REPLAY,
        content=b'{"emitter": "' + b"x" * dead_letters.MAX_REPLAY_BYTES + b'"}',
        headers={"content-type": "application/json"},
    )
    assert (large.status_code, large.json()) == (413, {"error": "body_too_large"})


async def test_a_replay_beyond_the_time_limit_puts_nothing_back(
    database: URL, engine: AsyncEngine
) -> None:
    ids = await ingest(engine, [event_of(0)])
    await to_dead_letters(engine, ids, "invalid_event")
    holder = create_async_engine(database, poolclass=NullPool, hide_parameters=True)
    try:
        transport = httpx.ASGITransport(
            app=create_app(engine, read_timeout_ms=50, authentication=authentication())
        )
        async with (
            httpx.AsyncClient(
                transport=transport, base_url="http://localhost", headers=signed_in()
            ) as client,
            holder.connect() as conn,
        ):
            await conn.execute(text("LOCK TABLE dead_letter IN ACCESS EXCLUSIVE MODE"))
            refused = await asyncio.wait_for(client.post(REPLAY, json={}), 10)
            await conn.rollback()
            assert (refused.status_code, refused.json()) == (503, {"error": "query_timeout"})
            # Rien n'a bougé ; le rejeu suivant, sans verrou, passe.
            assert await scalar(engine, "SELECT count(*) FROM dead_letter") == 1
            assert await scalar(engine, "SELECT count(*) FROM ingest_queue") == 0
            replayed = await client.post(REPLAY, json={})
            assert replayed.json() == {"replayed": 1, "more": False}
    finally:
        await holder.dispose()
