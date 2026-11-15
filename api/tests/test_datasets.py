"""La liste des jeux de données connus du graphe."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_api.store.graph import DatasetRef, Read, RunRecord, Write, record_run

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(engine))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield client


async def known(
    engine: AsyncEngine, *datasets: DatasetRef, written: tuple[DatasetRef, ...] = ()
) -> None:
    async with engine.begin() as conn:
        await record_run(
            conn,
            RunRecord(
                run_id=uuid4(),
                kind="job",
                state="COMPLETE",
                event_time=datetime(2026, 11, 3, 9, 0, tzinfo=UTC),
                reads=tuple(Read(dataset) for dataset in datasets),
                writes=tuple(Write(dataset) for dataset in written),
            ),
        )


async def listed(client: httpx.AsyncClient, **params: Any) -> dict[str, Any]:
    response = await client.get("/api/v1/datasets", params=params)
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


async def test_known_datasets_are_listed_by_namespace_and_name(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    assert await listed(client) == {"datasets": [], "more": False}
    await known(
        engine,
        DatasetRef("postgres://crm:5432", "crm.public.comptes"),
        DatasetRef("bigquery", "ventes.suivi.commandes"),
        DatasetRef("bigquery", "finance.ref.taux_change"),
        written=(DatasetRef("memory://finance", "notes", kind="memory"),),
    )

    assert await listed(client) == {
        "datasets": [
            {"namespace": "bigquery", "name": "finance.ref.taux_change", "kind": "table"},
            {"namespace": "bigquery", "name": "ventes.suivi.commandes", "kind": "table"},
            {"namespace": "memory://finance", "name": "notes", "kind": "memory"},
            {"namespace": "postgres://crm:5432", "name": "crm.public.comptes", "kind": "table"},
        ],
        "more": False,
    }


async def test_the_list_is_bounded(client: httpx.AsyncClient, engine: AsyncEngine) -> None:
    await known(engine, *(DatasetRef("bigquery", f"table_{rank:02d}") for rank in range(5)))

    first = await listed(client, limit=2)
    assert [dataset["name"] for dataset in first["datasets"]] == ["table_00", "table_01"]
    assert first["more"] is True
    assert (await listed(client, limit=5))["more"] is False

    for limit in ("0", "501", "tous"):
        response = await client.get("/api/v1/datasets", params={"limit": limit})
        assert response.status_code == 422
        assert [error["path"] for error in response.json()["errors"]] == ["/query/limit"]
