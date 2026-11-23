"""La liste de rappel du premier incident de l'entreprise synthétique, faite par un worker.

L'entreprise arrive par le chemin réel ; l'incident est déclaré par l'API, la boucle
d'un worker l'analyse et en fait la liste, que l'API rend. La liste est comparée à la
vérité de l'entreprise : chaque destinataire une fois, avec exactement ses réponses
touchées, rangé par l'exposition que la documentation décrit, recalculée ici sans le
produit.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_corpus.cli import DEFAULT_SEED
from answerlineage_corpus.enterprise import Enterprise, generate
from answerlineage_corpus.truth import truth
from answerlineage_workers.queue import run_worker
from tests.test_corpus_to_graph import api, load

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

HALF_LIFE = timedelta(days=30)


@pytest.fixture(scope="module")
def enterprise() -> Enterprise:
    return generate(DEFAULT_SEED)


@pytest.fixture
async def worker(engine: AsyncEngine, enterprise: Enterprise) -> AsyncIterator[AsyncEngine]:
    """L'entreprise chargée, et la boucle d'un worker, comme en production, le temps du test."""
    await load(engine, enterprise)
    stop = asyncio.Event()
    task = asyncio.ensure_future(run_worker(engine, stop, idle_delay=0.01))
    try:
        yield engine
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=30)


def received_by(enterprise: Enterprise, incident: dict[str, Any]) -> dict[str, list[datetime]]:
    """Pour chaque destinataire de la vérité, l'heure de chaque réponse touchée qu'il a reçue."""
    touched = {item["fingerprint"] for item in incident["answers"]}
    received: dict[str, list[datetime]] = {}
    for request in enterprise.requests:
        answer = request.answer
        if answer is not None and answer.fingerprint in touched:
            for recipient in answer.recipients:
                received.setdefault(recipient, []).append(answer.time)
    return received


async def recall_list(client: httpx.AsyncClient, incident_id: str) -> dict[str, Any]:
    """La liste de l'incident, une fois son analyse faite par le worker."""
    for _ in range(3000):
        answered = await client.get(f"/api/v1/incidents/{incident_id}/recall-list?limit=200")
        if answered.status_code == 200:
            found: dict[str, Any] = answered.json()
            return found
        assert answered.json() == {"error": "no_recall_list"}
        await asyncio.sleep(0.01)
    raise AssertionError("la liste n'a pas été faite à temps")


async def test_the_recall_list_of_the_first_incident_is_its_truth_ranked_by_exposure(
    worker: AsyncEngine, enterprise: Enterprise
) -> None:
    first: dict[str, Any] = truth(enterprise)["incidents"][0]
    async with api(worker) as client:
        declared = await client.post(
            "/api/v1/incidents",
            json={
                "scope": {"datasets": [first["dataset"]]},
                "window": {"from": first["from"], "to": first["to"]},
                "severity": "high",
            },
        )
        incident_id = declared.json()["id"]
        listed = await recall_list(client, incident_id)
        analysis = (await client.get(f"/api/v1/incidents/{incident_id}")).json()["analysis"]

    received = received_by(enterprise, first)
    entries = listed["entries"]
    # Chaque destinataire de la vérité, une fois, avec ses réponses touchées.
    assert sorted(entry["recipient"] for entry in entries) == sorted(first["recipients"])
    assert listed["counts"] == {"recipients": len(received), "answers": len(first["answers"])}
    assert (listed["more"], listed["reference_at"]) == (False, analysis["known_at"])
    for entry in entries:
        times = received[entry["recipient"]]
        assert entry["answer_count"] == len(entry["answers"]) == len(times)
        assert sorted(answer["produced_at"] for answer in entry["answers"]) == sorted(
            moment.isoformat().replace("+00:00", "Z") for moment in times
        )
    # Rangés par exposition : 2^(-âge / 30 jours) par réponse reçue, à l'heure de référence.
    reference = datetime.fromisoformat(listed["reference_at"])
    for entry in entries:
        expected = sum(
            2 ** -((reference - moment) / HALF_LIFE) for moment in received[entry["recipient"]]
        )
        assert entry["exposure"] == pytest.approx(expected, abs=1e-6)
    assert [entry["rank"] for entry in entries] == list(range(1, len(entries) + 1))
    exposures = [entry["exposure"] for entry in entries]
    assert exposures == sorted(exposures, reverse=True)
