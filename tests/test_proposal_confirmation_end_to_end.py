"""Rien ne part d'un incident proposé avant qu'un humain le confirme ; tout part après.

Un webhook est en service, la boucle d'un worker tourne comme en production
devant un récepteur servi sur un vrai port, et un test dbt en échec propose un
incident sur une table qu'une réponse a lue. Tant que la proposition attend,
les lots, les analyses, les propositions et les webhooks passent sans rien
faire partir : ni incident, ni analyse, ni message, ni quarantaine. À la
confirmation, l'incident est déclaré, son analyse faite sur la table et la
fenêtre déduite, et chaque changement d'état annoncé, signé.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_api.store.graph import (
    AgentRef,
    Answer,
    DatasetRef,
    Delivery,
    Read,
    RunRecord,
    ToolRef,
    record_run,
)
from answerlineage_workers.destination import parse_destination
from answerlineage_workers.queue import run_worker
from answerlineage_workers.webhooks import Webhook
from tests.test_webhooks_end_to_end import Receiver, in_service, until
from workers.tests.catalog import serving
from workers.tests.dbt import CUSTOMERS, T0, WAREHOUSE, failing

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

# Le temps de plusieurs passages de la boucle, lots, analyses, propositions et webhooks.
QUIET_SECONDS = 1.5


@pytest.fixture
async def api(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(engine))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        yield client


async def an_answer_read_the_tested_table(engine: AsyncEngine) -> None:
    """Une réponse, diffusée à deux destinataires, dont un appel d'outil a lu la table testée,
    une heure après la dernière réussite connue des tests."""
    agent, tool = uuid4(), uuid4()
    at = T0 + timedelta(hours=1)
    async with engine.begin() as conn:
        await record_run(
            conn,
            RunRecord(
                run_id=agent,
                kind="agent",
                state="COMPLETE",
                event_time=at + timedelta(minutes=5),
                agent=AgentRef("agent://ventes", "assistant-commercial"),
                answers=(
                    Answer(
                        f"sha256:{1:064x}",
                        deliveries=(Delivery("slack", "message-1", recipients=("p-1", "p-2")),),
                    ),
                ),
            ),
        )
        await record_run(
            conn,
            RunRecord(
                run_id=tool,
                kind="tool",
                state="COMPLETE",
                event_time=at,
                tool=ToolRef("entrepot", "execute_sql"),
                parent_run_id=agent,
                reads=(Read(DatasetRef(WAREHOUSE, CUSTOMERS)),),
            ),
        )


async def count(engine: AsyncEngine, table: str) -> int:
    async with engine.connect() as conn:
        found: int = (
            await conn.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608 — constant.
        ).scalar_one()
        return found


async def test_nothing_leaves_before_the_confirmation_and_everything_after(
    engine: AsyncEngine, api: httpx.AsyncClient
) -> None:
    """Critère : aucune analyse, aucun message, aucune quarantaine avant la confirmation ; à
    la confirmation, l'incident est déclaré, analysé sur la fenêtre déduite et annoncé."""
    await an_answer_read_the_tested_table(engine)
    receiver = Receiver(secrets.token_urlsafe(32))
    async with serving(receiver) as base:
        webhook = Webhook(parse_destination(base + "/webhooks"), receiver.secret)
        stop = asyncio.Event()
        worker = asyncio.create_task(run_worker(engine, stop, idle_delay=0.01, webhook=webhook))
        try:
            await until(lambda: in_service(engine), timeout=10)
            sent = await api.post("/api/v1/lineage/batch", json=[failing(1, 3, "email")])
            assert sent.status_code == 202, sent.text

            async def proposed() -> list[dict[str, Any]]:
                listed: list[dict[str, Any]] = (await api.get("/api/v1/incident-proposals")).json()[
                    "proposals"
                ]
                return listed

            await until(proposed, timeout=10)
            await asyncio.sleep(QUIET_SECONDS)
            (proposal,) = await proposed()
            assert proposal["state"] == "signaled"
            for table in ("incident", "incident_analysis", "webhook_queue", "incident_quarantine"):
                assert await count(engine, table) == 0, table
            assert receiver.requests == []
            assert (await api.get("/api/v1/quarantine")).json()["quarantines"] == []

            confirmed_at = time.monotonic()
            response = await api.post(
                f"/api/v1/incident-proposals/{proposal['id']}/confirm", json={"severity": "high"}
            )
            assert response.status_code == 200, response.text
            incident_id = response.json()["proposal"]["incident_id"]

            async def both_announced() -> bool:
                return len(receiver.accepted) >= 2

            await until(both_announced, timeout=60)
        finally:
            stop.set()
            await asyncio.wait_for(worker, timeout=30)

    detail = (await api.get(f"/api/v1/incidents/{incident_id}")).json()
    (declared_received, declared), (_, analysed) = receiver.accepted
    assert receiver.refused == []
    assert declared_received - confirmed_at < 60
    assert (declared["type"], analysed["type"]) == ("incident.declared", "incident.analysed")
    assert declared["incident"]["id"] == incident_id
    assert detail["proposal_id"] == proposal["id"]
    assert detail["scope"] == {"datasets": [{"namespace": WAREHOUSE, "name": CUSTOMERS}]}
    assert detail["window"]["from"] == proposal["window"]["from"]
    # L'analyse a trouvé la réponse qui a lu la table testée pendant la fenêtre déduite.
    assert detail["analysis"]["counts"] == {"answers": 1, "recipients": 2, "lower_bound": False}
