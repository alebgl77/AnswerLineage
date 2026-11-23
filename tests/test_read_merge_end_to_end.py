"""Un run lit une table en entier, puis en nomme une colonne : une autre colonne fausse le touche.

Les deux lectures arrivent dans deux événements du même appel d'outil, par le
chemin réel : ingestion, file, workers. Leur fusion garde la table entière, et
chaque lecture du graphe en tient compte : la remontée, la propagation, la vue
en cinq colonnes, l'exposition d'une colonne que le run n'a pas nommée.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.lineage.trace import answer_key
from answerlineage_corpus.events import agent_event, answer_delivered, dataset_read, tool_event
from tests.test_corpus_to_graph import api, ingest

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
ORDERS = ("bigquery", "ventes.suivi.commandes")
JOB = {"namespace": "agent://ventes", "name": "assistant-commercial"}
WHOLE = UUID("0193a9c5-7a10-7c3e-9d5e-2f1b6c8a0001")
WHOLE_CALL = UUID("0193a9c5-7a10-7c3e-9d5e-2f1b6c8a0011")
NAMED = UUID("0193a9c5-7a10-7c3e-9d5e-2f1b6c8a0002")
NAMED_CALL = UUID("0193a9c5-7a10-7c3e-9d5e-2f1b6c8a0012")


def _at(second: int) -> datetime:
    return T0 + timedelta(seconds=second)


def _fingerprint(run_id: UUID) -> str:
    return f"sha256:{run_id.int:064x}"


def _answer(run_id: UUID, second: int, recipient: str) -> list[dict[str, Any]]:
    delivered = answer_delivered(
        JOB,
        fingerprint=_fingerprint(run_id),
        channel="slack",
        message_ref=f"C042/{second}",
        recipients=[recipient],
    )
    return [
        agent_event("START", _at(second - 10), run_id, JOB),
        agent_event("COMPLETE", _at(second), run_id, JOB, outputs=[delivered]),
    ]


def _call(run_id: UUID, call_id: UUID, second: int, *columns: str) -> dict[str, Any]:
    return tool_event(
        _at(second),
        call_id,
        server="entrepot",
        tool="execute_sql",
        parent=(run_id, JOB),
        inputs=[dataset_read(*ORDERS, columns=columns)],
    )


def events() -> list[dict[str, Any]]:
    # Le même appel d'outil : la table entière d'abord, puis la colonne « montant ».
    whole_table = _call(WHOLE, WHOLE_CALL, 2)
    whole_table["eventType"] = "RUNNING"
    return [
        *_answer(WHOLE, 20, "hmac-sha256:" + "a" * 64),
        whole_table,
        _call(WHOLE, WHOLE_CALL, 4, "montant"),
        # Une autre réponse nomme la colonne « statut » : l'exposition la connaît.
        *_answer(NAMED, 40, "hmac-sha256:" + "b" * 64),
        _call(NAMED, NAMED_CALL, 32, "statut"),
    ]


async def _get(engine: AsyncEngine, path: str, **params: str) -> dict[str, Any]:
    async with api(engine) as client:
        response = await client.get(path, params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_a_column_the_run_did_not_name_still_reaches_its_answer(engine: AsyncEngine) -> None:
    await ingest(engine, events())
    whole = answer_key(WHOLE, _fingerprint(WHOLE))
    named = answer_key(NAMED, _fingerprint(NAMED))

    # La remontée : la table entière est une source de la réponse.
    sources = await _get(engine, f"/api/v1/answers/{whole}/sources")
    (dataset,) = sources["datasets"]
    assert (dataset["precision"], dataset["columns"]) == ("table", [])

    # La propagation : la réponse est touchée.
    impact = await _get(
        engine,
        "/api/v1/impact",
        namespace=ORDERS[0],
        name=ORDERS[1],
        **{"from": _at(0).isoformat(), "to": _at(60).isoformat()},
    )
    assert {answer["id"] for answer in impact["graph"]["answers"]} == {whole, named}
    assert impact["counts"]["recipients"] == 2

    # La vue en cinq colonnes : la réponse cite la table.
    view = await _get(engine, "/api/v1/graph", answer=whole)
    ((read,),) = [answer["reads"] for answer in view["answers"]]
    (node,) = view["datasets"]
    assert read["dataset"] == node["id"]
    assert (node["namespace"], node["name"]) == ORDERS

    # L'exposition : la colonne « statut » compte aussi la réponse qui a tout lu.
    exposure = await _get(engine, "/api/v1/exposure", at=_at(60).isoformat())
    (exposed,) = exposure["datasets"]
    assert exposed["answers"] == 2
    assert [(column["name"], column["answers"]) for column in exposed["columns"]] == [("statut", 2)]
