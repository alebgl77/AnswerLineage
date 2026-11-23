"""L'état de l'installation : base, migrations, file d'ingestion, rebut, émetteurs, partitions,
réémission.

La route sert la vérification d'une installation (``answerlineage doctor``) :
elle dit des états et des décomptes bornés, jamais un détail interne.
"""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator
from datetime import date
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api import health
from answerlineage_api.app import API_VERSION, create_app
from answerlineage_api.ingest.dead_letters import bury
from answerlineage_api.store.engine import create_engine
from answerlineage_api.store.migrate import downgrade, upgrade
from api.tests.events import run_event

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

REPORT = {
    "emitter": "mcp://entrepot",
    "window": {"from": "2026-11-03T06:00:00Z", "to": "2026-11-03T06:01:00Z"},
    "calls_seen": 0,
    "events_emitted": 0,
    "events_lost": 0,
    "uninspected": 0,
}


def client(engine: AsyncEngine) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app(engine))
    return httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1")


async def state(engine: AsyncEngine) -> dict[str, Any]:
    async with client(engine) as api:
        response = await api.get("/api/v1/health")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


async def bury_the_queue(engine: AsyncEngine) -> None:
    """Met au rebut tout ce qui attend dans la file, comme un worker au premier échec."""
    async with engine.begin() as conn:
        waiting = (await conn.execute(text("SELECT id FROM ingest_queue ORDER BY id"))).scalars()
        for queue_id in waiting.all():
            await bury(
                conn, queue_id=queue_id, emitter="mcp://entrepot", reason="constraint_violation"
            )


@pytest.fixture
async def bare(empty_database: URL) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(empty_database)
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_a_sound_installation_says_so_with_empty_tallies(engine: AsyncEngine) -> None:
    assert await state(engine) == {
        "api_version": API_VERSION,
        "database": "available",
        "migrations": "current",
        "queue": {"pending": 0, "lower_bound": False, "oldest_age_seconds": None},
        "dead_letters": {"events": 0, "lower_bound": False, "oldest_age_seconds": None},
        "emitters": {"known": 0, "muted": 0, "lower_bound": False},
        "partitions": {"missing": 0, "lower_bound": False},
        "reemission": {
            "queued": 0,
            "retrying": 0,
            "lower_bound": False,
            "oldest_age_seconds": None,
            "sent": 0,
            "lost": 0,
            "rejected": 0,
        },
    }


async def test_a_database_without_schema_is_not_migrated(bare: AsyncEngine) -> None:
    assert await state(bare) == {
        "api_version": API_VERSION,
        "database": "available",
        "migrations": "not_migrated",
        "queue": None,
        "dead_letters": None,
        "emitters": None,
        "partitions": None,
        "reemission": None,
    }


async def test_a_database_brought_back_to_base_is_not_migrated(
    bare: AsyncEngine, empty_database: URL
) -> None:
    # Alembic garde sa table, vide.
    upgrade(empty_database)
    downgrade(empty_database, "base")
    assert (await state(bare))["migrations"] == "not_migrated"


async def test_a_schema_with_migrations_left_to_apply_is_behind(
    bare: AsyncEngine, empty_database: URL
) -> None:
    upgrade(empty_database, "0007")
    body = await state(bare)
    assert (body["migrations"], body["queue"], body["emitters"]) == ("behind", None, None)
    assert body["dead_letters"] is None
    assert body["reemission"] is None


async def test_a_revision_the_api_does_not_know_is_reported_without_being_cited(
    engine: AsyncEngine,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE alembic_version SET version_num = 'canari_9f3e'"))
    async with client(engine) as api:
        response = await api.get("/api/v1/health")
    assert response.json()["migrations"] == "unknown_revision"
    assert "canari_9f3e" not in response.text


async def test_an_unreachable_database_is_unavailable_and_nothing_else_is_said() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    # Un mot de passe canari : il ne doit apparaître nulle part dans la réponse.
    engine = create_engine(f"postgresql://al:canari-secret@127.0.0.1:{port}/al")
    try:
        async with client(engine) as api:
            response = await api.get("/api/v1/health")
    finally:
        await engine.dispose()
    assert response.status_code == 200
    assert response.json() == {
        "api_version": API_VERSION,
        "database": "unavailable",
        "migrations": None,
        "queue": None,
        "dead_letters": None,
        "emitters": None,
        "partitions": None,
        "reemission": None,
    }
    assert "canari" not in response.text
    assert str(port) not in response.text


async def test_the_queue_says_how_many_events_wait_and_since_when(engine: AsyncEngine) -> None:
    async with client(engine) as api:
        for minute in range(3):
            response = await api.post("/api/v1/lineage", json=run_event(minute))
            assert response.status_code == 201
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE ingest_queue SET enqueued_at = now() - interval '2 hours'"
                " WHERE id = (SELECT min(id) FROM ingest_queue)"
            )
        )
    queue = (await state(engine))["queue"]
    assert queue["pending"] == 3
    assert queue["lower_bound"] is False
    assert 7200 <= queue["oldest_age_seconds"] < 7260


async def test_the_dead_letters_say_how_many_events_wait_and_since_when(
    engine: AsyncEngine,
) -> None:
    async with client(engine) as api:
        for minute in range(3):
            assert (await api.post("/api/v1/lineage", json=run_event(minute))).status_code == 201
    await bury_the_queue(engine)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE dead_letter SET first_failed_at = now() - interval '2 hours',"
                " last_failed_at = now() - interval '2 hours'"
                " WHERE event_id = (SELECT min(event_id) FROM dead_letter)"
            )
        )
    body = await state(engine)
    buried = body["dead_letters"]
    assert (buried["events"], buried["lower_bound"]) == (3, False)
    assert 7200 <= buried["oldest_age_seconds"] < 7260
    # Au rebut, ils ont quitté la file.
    assert body["queue"]["pending"] == 0

    # Rejoués, ils quittent le rebut : il redevient vide, sans âge.
    async with client(engine) as api:
        replayed = await api.post("/api/v1/ingestion/dead-letters/replay", json={})
    assert replayed.json() == {"replayed": 3, "more": False}
    assert (await state(engine))["dead_letters"] == {
        "events": 0,
        "lower_bound": False,
        "oldest_age_seconds": None,
    }


async def test_the_dead_letter_count_stops_at_its_bound_and_says_so(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health, "COUNT_LIMIT", 2)
    async with client(engine) as api:
        for minute in range(3):
            assert (await api.post("/api/v1/lineage", json=run_event(minute))).is_success
    await bury_the_queue(engine)
    buried = (await state(engine))["dead_letters"]
    assert (buried["events"], buried["lower_bound"]) == (2, True)
    # Le plus ancien reste celui du rebut entier : la borne compte, elle ne choisit pas.
    assert buried["oldest_age_seconds"] is not None


async def test_counts_stop_at_their_bound_and_say_so(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health, "COUNT_LIMIT", 2)
    async with client(engine) as api:
        for minute in range(3):
            assert (await api.post("/api/v1/lineage", json=run_event(minute))).is_success
            emitter = {**REPORT, "emitter": f"mcp://serveur-{minute}"}
            assert (await api.post("/api/v1/coverage", json=emitter)).status_code == 201
    async with engine.begin() as conn:
        for month in range(3):
            await conn.execute(
                text("INSERT INTO partition_alert VALUES ('event_raw', :month, now())"),
                {"month": date(2030, month + 1, 1)},
            )
    body = await state(engine)
    assert body["queue"]["pending"] == 2
    assert body["queue"]["lower_bound"] is True
    assert body["emitters"] == {"known": 2, "muted": 0, "lower_bound": True}
    assert body["partitions"] == {"missing": 2, "lower_bound": True}


async def test_muted_emitters_are_those_the_workers_signalled(engine: AsyncEngine) -> None:
    async with client(engine) as api:
        for name in ("mcp://entrepot", "mcp://crm"):
            assert (await api.post("/api/v1/coverage", json={**REPORT, "emitter": name})).is_success
    assert (await state(engine))["emitters"] == {"known": 2, "muted": 0, "lower_bound": False}

    # Ce que pose le contrôle des émetteurs muets des workers.
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE coverage_emitter SET silent_since = last_report_at"))
    assert (await state(engine))["emitters"]["muted"] == 2

    # Un émetteur qui republie n'est plus muet.
    async with client(engine) as api:
        republished = {**REPORT, "calls_seen": 1, "events_emitted": 1}
        assert (await api.post("/api/v1/coverage", json=republished)).is_success
    assert (await state(engine))["emitters"]["muted"] == 1


async def test_missing_partitions_signalled_by_the_workers_are_counted(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO partition_alert VALUES ('coverage_report', '2030-01-01', now())")
        )
    assert (await state(engine))["partitions"] == {"missing": 1, "lower_bound": False}


async def test_the_reemission_says_what_waits_since_when_and_what_left_or_was_lost(
    engine: AsyncEngine,
) -> None:
    async with engine.begin() as conn:
        for rank in range(3):
            await conn.execute(
                text(
                    "INSERT INTO reemission_queue (event_id, recorded_month, enqueued_at,"
                    " attempts, last_error)"
                    " VALUES (:event_id, '2026-11-01', now() - make_interval(hours => :rank),"
                    " :attempts, :error)"
                ),
                {
                    "event_id": f"{rank:064x}",
                    "rank": rank,
                    "attempts": rank,
                    "error": "HTTP 503" if rank else None,
                },
            )
        await conn.execute(
            text("INSERT INTO reemission_stats (sent, rejected, dropped) VALUES (12, 2, 5)")
        )
    reemission = (await state(engine))["reemission"]
    oldest = reemission.pop("oldest_age_seconds")
    assert 7200 <= oldest < 7260
    assert reemission == {
        "queued": 3,
        "retrying": 2,
        "lower_bound": False,
        "sent": 12,
        "lost": 7,
        "rejected": 2,
    }


async def test_the_reemission_count_stops_at_its_bound_and_says_so(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health, "COUNT_LIMIT", 2)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO reemission_queue (event_id, recorded_month)"
                " SELECT md5(n::text) || md5(n::text), '2026-11-01' FROM generate_series(1, 3) n"
            )
        )
    reemission = (await state(engine))["reemission"]
    assert (reemission["queued"], reemission["retrying"], reemission["lower_bound"]) == (
        2,
        0,
        True,
    )


async def test_the_route_answers_only_an_expected_host(engine: AsyncEngine) -> None:
    async with client(engine) as api:
        response = await api.get("/api/v1/health", headers={"host": "attaquant.example"})
    assert response.status_code == 403
    assert response.json() == {"error": "host_not_allowed"}
