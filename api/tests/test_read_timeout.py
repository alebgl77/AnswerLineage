"""Toute lecture du graphe s'exécute sous un délai maximal, imposé par PostgreSQL.

Le dépassement est provoqué pour de bon : une autre connexion tient un verrou
exclusif sur les tables lues, et la lecture l'attend jusqu'à ce que PostgreSQL
l'annule.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from answerlineage_api.app import create_app
from answerlineage_api.lineage.reading import ENV_READ_TIMEOUT, Reader, ReadTimedOut

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

READ_TABLES = (
    "agent, tool, dataset, dataset_version, run, read_edge, write_edge,"
    " answer, delivery, recipient, delivery_recipient, ingest_duplicate"
)
IMPACT = {
    "namespace": "bigquery",
    "name": "finance.ref.taux_change",
    "from": "2026-11-03T06:00:00Z",
    "to": "2026-11-03T16:00:00Z",
}
READS = [
    ("/api/v1/answers", {"channel": "slack", "message_ref": "m-1"}),
    (f"/api/v1/answers/{'0' * 48}/sources", {}),
    ("/api/v1/graph", {}),
    ("/api/v1/graph", {"answer": "0" * 48}),
    ("/api/v1/datasets", {}),
    ("/api/v1/impact", IMPACT),
    ("/api/v1/ingestion/duplicates", {}),
]
# Au-delà, la lecture n'a pas été interrompue : le test échoue au lieu d'attendre.
PATIENCE = 10.0


@pytest.fixture
async def single(database: URL) -> AsyncIterator[AsyncEngine]:
    """Un moteur d'une seule connexion : la lecture suivante reprend celle qui a été annulée."""
    engine = create_async_engine(database, pool_size=1, max_overflow=0, hide_parameters=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@asynccontextmanager
async def locked(database: URL) -> AsyncIterator[AsyncEngine]:
    """Une autre connexion tient un verrou exclusif sur les tables lues."""
    holder = create_async_engine(database, poolclass=NullPool, hide_parameters=True)
    try:
        async with holder.connect() as conn:
            await conn.execute(text(f"LOCK TABLE {READ_TABLES} IN ACCESS EXCLUSIVE MODE"))
            yield holder
            # La lecture annulée n'attend plus le verrou : PostgreSQL l'a interrompue.
            waiting = await conn.execute(text("SELECT count(*) FROM pg_locks WHERE NOT granted"))
            assert waiting.scalar_one() == 0
            await conn.rollback()
    finally:
        await holder.dispose()


async def get(
    app_engine: AsyncEngine, path: str, params: dict[str, str], read_timeout_ms: int | None = None
) -> httpx.Response:
    transport = httpx.ASGITransport(app=create_app(app_engine, read_timeout_ms=read_timeout_ms))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        return await asyncio.wait_for(client.get(path, params=params), PATIENCE)


@pytest.mark.parametrize(("path", "params"), READS)
async def test_a_read_beyond_the_time_limit_is_cancelled_and_says_why(
    database: URL, single: AsyncEngine, path: str, params: dict[str, str]
) -> None:
    app = create_app(single, read_timeout_ms=50)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        async with locked(database):
            refused = await asyncio.wait_for(client.get(path, params=params), PATIENCE)
        assert refused.status_code == 503
        # Un code stable, sans rien de la requête ni des valeurs reçues.
        assert refused.json() == {"error": "query_timeout"}
        for value in params.values():
            assert value not in refused.text

        # La connexion rendue au pool sert la lecture suivante, sans garder le délai.
        answered = await asyncio.wait_for(client.get(path, params=params), PATIENCE)
        assert answered.status_code in {200, 404}
    async with single.connect() as conn:
        assert (await conn.execute(text("SHOW statement_timeout"))).scalar_one() == "0"


async def test_the_time_limit_comes_from_the_environment(
    database: URL, single: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_READ_TIMEOUT, "50")
    async with locked(database):
        refused = await get(single, "/api/v1/datasets", {})
    assert refused.status_code == 503


async def test_the_time_limit_given_to_the_application_wins_over_the_environment(
    database: URL, single: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_READ_TIMEOUT, "600000")
    async with locked(database):
        refused = await get(single, "/api/v1/datasets", {}, read_timeout_ms=50)
    assert refused.status_code == 503


@pytest.mark.parametrize("value", ["0", "-5", "dix", "1.5", "1e3", "600001"])
def test_a_time_limit_out_of_bounds_stops_the_start(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(ENV_READ_TIMEOUT, value)
    with pytest.raises(ValueError, match=ENV_READ_TIMEOUT):
        create_app()


@pytest.mark.parametrize("value", [0, -1, 600_001])
def test_a_time_limit_given_out_of_bounds_is_refused(value: int) -> None:
    with pytest.raises(ValueError, match="délai"):
        create_app(read_timeout_ms=value)


async def test_each_statement_gets_only_what_remains_of_the_time_limit(
    engine: AsyncEngine,
) -> None:
    async with engine.connect() as conn:
        reader = Reader(conn, deadline=time.monotonic() + 1.0)
        setting = text("SELECT setting::int FROM pg_settings WHERE name = 'statement_timeout'")
        remaining = (await reader.execute(setting, {})).scalar_one()
        assert 0 < remaining <= 1000

        # Délai écoulé : la lecture suivante n'est même pas envoyée à la base.
        with pytest.raises(ReadTimedOut):
            await Reader(conn, deadline=time.monotonic()).execute(text("SELECT 1"), {})


async def test_an_error_that_is_not_a_timeout_is_not_disguised_as_one(
    engine: AsyncEngine,
) -> None:
    async with engine.connect() as conn:
        reader = Reader(conn, deadline=time.monotonic() + 5.0)
        with pytest.raises(DBAPIError):
            await reader.execute(text("SELECT 1 / 0"), {})
