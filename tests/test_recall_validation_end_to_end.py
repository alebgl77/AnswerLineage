"""La validation humaine d'une liste de rappel, de bout en bout, sur l'entreprise synthétique.

L'entreprise arrive par le chemin réel ; l'incident est déclaré par l'API, la boucle d'un
worker l'analyse et en fait la liste. Restée sans décision au-delà du délai, la liste
remonte au responsable de données, qui la voit par l'API ; un responsable d'agent la
valide ensuite. Tant qu'elle n'est pas validée, aucun message n'est parti, et la base
refuse qu'il en parte un.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.auth.roles import Role
from answerlineage_corpus.cli import DEFAULT_SEED
from answerlineage_corpus.enterprise import generate
from answerlineage_corpus.truth import truth
from answerlineage_workers.queue import run_worker
from api.tests.issuer import ISSUER, TEST_ISSUER, signed_in
from tests.test_corpus_to_graph import api, load

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

OWNER = "responsable-agent-ventes"


@asynccontextmanager
async def worker(engine: AsyncEngine) -> AsyncIterator[None]:
    """La boucle d'un worker, comme en production, le temps du bloc ; elle fait remonter les
    listes en retard à son démarrage, puis chaque minute."""
    stop = asyncio.Event()
    task = asyncio.ensure_future(run_worker(engine, stop, idle_delay=0.01))
    try:
        yield
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=30)


async def eventually(client: httpx.AsyncClient, path: str, **params: Any) -> dict[str, Any]:
    """La réponse de la route, une fois que le worker a fait ce qu'elle attend."""
    for _ in range(3000):
        answered = await client.get(path, params=params, headers=signed_in(Role.DATA_STEWARD))
        found: dict[str, Any] = answered.json()
        if answered.status_code == 200 and found.get("lists", True):
            return found
        await asyncio.sleep(0.01)
    raise AssertionError("le worker n'a pas fait ce qui était attendu à temps")


async def test_a_list_left_without_decision_reaches_the_data_steward_then_is_validated(
    engine: AsyncEngine,
) -> None:
    """Critères : une liste sans décision au-delà du délai remonte au responsable de données ;
    tant qu'elle n'est pas validée, rien n'est parti ; validée, la décision, son auteur et
    l'heure sont journalisés, et l'envoi devient possible."""
    enterprise = generate(DEFAULT_SEED)
    first: dict[str, Any] = truth(enterprise)["incidents"][0]
    await load(engine, enterprise)
    async with api(engine) as client:
        declared = await client.post(
            "/api/v1/incidents",
            json={
                "scope": {"datasets": [first["dataset"]]},
                "window": {"from": first["from"], "to": first["to"]},
                "severity": "critical",
            },
        )
        incident_id = declared.json()["id"]
        address = f"/api/v1/incidents/{incident_id}/recall-list"
        async with worker(engine):
            listed = await eventually(client, address)
        assert (listed["state"], listed["escalated_at"], listed["dispatched"]) == (
            "pending",
            None,
            0,
        )
        assert listed["counts"]["recipients"] == len(first["recipients"])

        # Le délai est passé : la liste a été faite il y a deux jours et une heure.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE recall_list SET built_at = built_at - interval '49 hours'"
                    " WHERE incident_id = :id"
                ),
                {"id": incident_id},
            )
        async with worker(engine):
            escalated = await eventually(
                client, "/api/v1/recall-lists", state="pending", escalated="true"
            )
        assert [item["incident_id"] for item in escalated["lists"]] == [incident_id]
        assert escalated["lists"][0]["severity"] == "critical"

        # Sans validation, rien n'est parti, et la base refuse qu'un message parte.
        with pytest.raises(IntegrityError) as refused:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO recall_dispatch (incident_id, pseudonym)"
                        " SELECT incident_id, pseudonym FROM recall_entry"
                        " WHERE incident_id = :id"
                    ),
                    {"id": incident_id},
                )
        diagnostic = getattr(refused.value.orig, "diag", None)
        assert getattr(diagnostic, "constraint_name", None) == "ck_recall_dispatch_validated"

        owner = {"authorization": f"Bearer {TEST_ISSUER.token(Role.AGENT_OWNER, sub=OWNER)}"}
        validated = await client.post(
            f"{address}/decision", json={"decision": "validated"}, headers=owner
        )
        assert validated.status_code == 200, validated.text
        assert validated.json()["decision"]["author"] == {"issuer": ISSUER, "subject": OWNER}

        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO recall_dispatch (incident_id, pseudonym)"
                    " SELECT incident_id, pseudonym FROM recall_entry WHERE incident_id = :id"
                ),
                {"id": incident_id},
            )
        shown = (await client.get(address, headers=signed_in(Role.READER))).json()

    assert shown["state"] == "validated"
    assert shown["escalated_at"] is not None
    assert shown["dispatched"] == len(first["recipients"])
