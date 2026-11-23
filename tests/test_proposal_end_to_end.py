"""Un test dbt en échec, par le chemin réel : ingestion, file, boucle d'un worker, proposition.

L'événement de test part à l'API d'ingestion comme dbt-ol l'enverrait ; la
boucle d'un worker, comme en production, le projette puis en fait une
proposition, que l'API rend en quelques secondes. Rien d'autre ne part : ni
incident, ni analyse, ni annonce, même avec un webhook en service.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_workers.queue import run_worker
from workers.tests.dbt import CUSTOMERS, WAREHOUSE, failing

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

# Ce qu'un humain attend au plus pour voir la proposition.
PATIENCE = 5.0


async def test_a_failing_dbt_test_is_proposed_within_seconds_and_nothing_else_leaves(
    engine: AsyncEngine,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO webhook_stats (id, in_service_until)"
                " VALUES (true, now() + interval '1 hour')"
            )
        )
    stop = asyncio.Event()
    worker = asyncio.ensure_future(run_worker(engine, stop, idle_delay=0.05))
    transport = httpx.ASGITransport(app=create_app(engine))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            sent = await client.post("/api/v1/lineage/batch", json=[failing(1, 3, "email")])
            assert sent.status_code == 202, sent.text
            started = time.monotonic()
            proposals: list[dict[str, Any]] = []
            while not proposals and time.monotonic() - started < PATIENCE:
                await asyncio.sleep(0.05)
                listed = await client.get("/api/v1/incident-proposals")
                proposals = listed.json()["proposals"]
            (proposal,) = proposals
            assert proposal["state"] == "signaled"
            assert proposal["dataset"] == {"namespace": WAREHOUSE, "name": CUSTOMERS}
            assert proposal["columns"] == ["email"]
            detail = await client.get(f"/api/v1/incident-proposals/{proposal['id']}")
            assert detail.json()["counts"] == {"failed_runs": 1, "lower_bound": False}
            assert (await client.get("/api/v1/incidents")).json()["incidents"] == []
    finally:
        stop.set()
        await asyncio.wait_for(worker, timeout=30)
    async with engine.connect() as conn:
        for table in ("incident", "incident_analysis", "webhook_queue"):
            count = (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()  # noqa: S608
            assert count == 0, table
