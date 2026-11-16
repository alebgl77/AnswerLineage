"""Parcourir les réponses page par page, sans perte ni doublon.

Des réponses produites au même instant ne se départagent pas par l'heure : le
curseur rendu par l'API les départage par l'identifiant stable de la réponse.
"""

from __future__ import annotations

import random
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
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

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

T0 = datetime(2026, 11, 4, 8, 0, tzinfo=UTC)
# Trois instants seulement : les réponses se partagent les heures.
INSTANTS = [T0 + timedelta(minutes=minutes) for minutes in (10, 20, 30)]
RATES = DatasetRef("bigquery", "finance.ref.taux_change")
WINDOW = {
    "namespace": RATES.namespace,
    "name": RATES.name,
    "from": "2026-11-04T08:00:00Z",
    "to": "2026-11-04T09:00:00Z",
}
ROUTES = [("/api/v1/graph", {}), ("/api/v1/impact", WINDOW)]


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(engine))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield client


async def tied_answers(engine: AsyncEngine, seed: int) -> set[str]:
    """Des runs qui répondent aux mêmes instants, certains plusieurs fois."""
    dice = random.Random(seed)  # noqa: S311 — un tirage reproductible, pas un secret.
    expected: set[str] = set()
    async with engine.begin() as conn:
        for _ in range(dice.randint(5, 8)):
            run_id = uuid4()
            fingerprints = [
                f"sha256:{dice.getrandbits(256):064x}" for _ in range(dice.randint(1, 3))
            ]
            at = dice.choice(INSTANTS)
            await record_run(
                conn,
                RunRecord(
                    run_id=run_id,
                    kind="agent",
                    state="COMPLETE",
                    event_time=at,
                    agent=AgentRef("agent://ventes", "assistant-commercial"),
                    answers=tuple(Answer(fingerprint) for fingerprint in fingerprints),
                ),
            )
            await record_run(
                conn,
                RunRecord(
                    run_id=uuid4(),
                    kind="tool",
                    state="COMPLETE",
                    event_time=at - timedelta(minutes=1),
                    tool=ToolRef("entrepot", "execute_sql"),
                    parent_run_id=run_id,
                    reads=(Read(RATES),),
                ),
            )
            expected.update(answer_key(run_id, fingerprint) for fingerprint in fingerprints)
    return expected


async def every_page(
    client: httpx.AsyncClient, path: str, params: dict[str, Any], limit: int
) -> list[dict[str, Any]]:
    answers: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(100):
        query = {**params, "limit": limit} | ({} if cursor is None else {"cursor": cursor})
        response = await client.get(path, params=query)
        assert response.status_code == 200
        body = response.json()
        view = body.get("graph", body)
        assert len(view["answers"]) <= limit
        answers.extend(view["answers"])
        cursor = view["next_cursor"]
        assert view["more"] is (cursor is not None)
        if cursor is None:
            return answers
    pytest.fail("La pagination ne s'arrête pas.")


@pytest.mark.parametrize("seed", range(4))
@pytest.mark.parametrize(("path", "params"), ROUTES)
async def test_every_answer_comes_once_whatever_the_page_size(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    seed: int,
    path: str,
    params: dict[str, Any],
) -> None:
    expected = await tied_answers(engine, seed)

    for limit in range(1, len(expected) + 2):
        answers = await every_page(client, path, params, limit)
        identifiers = [answer["id"] for answer in answers]
        assert sorted(identifiers) == sorted(expected), f"pages de {limit}"
        # Les plus récentes d'abord, d'une page à l'autre aussi.
        produced = [answer["produced_at"] for answer in answers]
        assert produced == sorted(produced, reverse=True)
