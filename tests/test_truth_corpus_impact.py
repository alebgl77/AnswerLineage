"""Le moteur d'impact, mesuré sur le corpus de vérité.

Chaque scénario passe par le chemin réel : ingestion, file, workers, puis
propagation de son incident au niveau table. Toute réponse attendue à ce niveau
doit être renvoyée.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_corpus.scenarios import Scenario, load_corpus
from tests.test_corpus_to_graph import api, ingest

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

CORPUS = load_corpus()


async def propagate(engine: AsyncEngine, scenario: Scenario) -> dict[str, Any]:
    await ingest(engine, scenario.events())
    incident = scenario.incident
    async with api(engine) as client:
        response = await client.get(
            "/api/v1/impact",
            params={
                "namespace": incident.dataset.namespace,
                "name": incident.dataset.name,
                "from": incident.start.isoformat(),
                "to": incident.end.isoformat(),
            },
        )
    assert response.status_code == 200
    result: dict[str, Any] = response.json()
    return result


@pytest.mark.parametrize("scenario", CORPUS.scenarios, ids=lambda scenario: scenario.name)
async def test_every_answer_expected_at_table_level_is_returned(
    engine: AsyncEngine, scenario: Scenario
) -> None:
    result = await propagate(engine, scenario)
    returned = {answer["fingerprint"] for answer in result["graph"]["answers"]}
    by_fingerprint = {answer.fingerprint: answer for answer in scenario.answers}

    # Rappel : aucune réponse attendue au niveau table ne manque.
    expected = {answer.fingerprint for answer in scenario.expected("direct")}
    assert expected <= returned, sorted(by_fingerprint[f].key for f in expected - returned)

    # Au niveau table, tout lecteur de la fenêtre est renvoyé : les réponses
    # écartées par leurs colonnes, leur sous-ensemble, leurs clés ou leur
    # version le sont aussi. Elles mesureront la précision des niveaux plus fins.
    discarded = {answer.fingerprint for answer, _ in scenario.discarded}
    assert returned == expected | discarded, sorted(
        by_fingerprint[f].key for f in returned ^ (expected | discarded)
    )

    # Ni une réponse sans rapport, ni une lecture faite hors de la fenêtre.
    assert not returned & {answer.fingerprint for answer in scenario.untouched}

    recipients = {pseudonym for f in returned for pseudonym in by_fingerprint[f].recipients}
    assert {r["pseudonym"] for r in result["graph"]["recipients"]} == recipients
    assert result["counts"] == {"answers": len(returned), "recipients": len(recipients)}
    assert result["truncated"] is False


async def test_an_answer_built_on_a_read_made_after_the_window_is_not_returned(
    engine: AsyncEngine,
) -> None:
    scenario = next(item for item in CORPUS if item.name == "table-fenetre")
    result = await propagate(engine, scenario)
    returned = {answer["fingerprint"] for answer in result["graph"]["answers"]}
    answers = {answer.key: answer.fingerprint for answer in scenario.answers}

    assert answers["heure-de-la-correction"] not in returned
    # Lue avant la correction, rendue après : c'est la lecture qui compte.
    assert answers["reponse-rendue-apres-la-correction"] in returned
