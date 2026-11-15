"""Le moteur d'impact, mesuré sur le corpus de vérité : sa porte de qualité.

Chaque scénario passe par le chemin réel, dans sa propre base : ingestion,
file, workers, puis propagation de son incident au niveau table. Ce que le
moteur renvoie est comparé à la vérité du corpus (``answerlineage_corpus.measure``),
qui ne passe jamais par le produit. La porte échoue quand le rappel au niveau
table passe sous son seuil, ou qu'une réponse hors d'atteinte est renvoyée, et
son message nomme les réponses en cause.

``make measure`` rejoue la mesure seule et imprime son tableau ; avec
``--truth-report CHEMIN``, son détail est aussi écrit en JSON.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.lineage import impact
from answerlineage_corpus.measure import CorpusMeasure, as_json, gate, measure, render
from answerlineage_corpus.scenarios import Scenario, load_corpus
from tests.test_corpus_to_graph import api, ingest

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

CORPUS = load_corpus()

EngineFactory = Callable[[], AbstractAsyncContextManager[AsyncEngine]]

# Un moteur affaibli : il ne remonte qu'au run parent immédiat d'une lecture.
# La lecture d'un outil appelé par un sous-agent n'atteint plus la réponse de
# l'agent qui a délégué.
_PARENT_ONLY = text(
    """
    SELECT r.parent_run_id AS run_id FROM run r
    WHERE r.run_id = ANY(:readers) AND r.parent_run_id IS NOT NULL
    LIMIT :limit
    """
)


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


async def returned_by_the_engine(engine: AsyncEngine, scenario: Scenario) -> set[str]:
    """Les empreintes des réponses renvoyées ; les décomptes et les destinataires s'y accordent."""
    result = await propagate(engine, scenario)
    assert result["truncated"] is False
    assert result["graph"]["more"] is False
    returned = {answer["fingerprint"] for answer in result["graph"]["answers"]}

    by_fingerprint = {answer.fingerprint: answer for answer in scenario.answers}
    recipients = {
        pseudonym
        for fingerprint in returned
        if fingerprint in by_fingerprint
        for pseudonym in by_fingerprint[fingerprint].recipients
    }
    assert {r["pseudonym"] for r in result["graph"]["recipients"]} == recipients
    assert result["counts"] == {
        "answers": len(returned),
        "recipients": len(recipients),
        "lower_bound": False,
    }
    return returned


async def measured(fresh_engine: EngineFactory) -> CorpusMeasure:
    """Chaque scénario, dans sa propre base, mesuré contre la vérité du corpus."""
    returned: dict[str, set[str]] = {}
    for scenario in CORPUS:
        async with fresh_engine() as engine:
            returned[scenario.name] = await returned_by_the_engine(engine, scenario)
    return measure(CORPUS, returned)


def assert_the_gate_passes(result: CorpusMeasure) -> None:
    """Échoue avec le message de la porte, tel quel : les réponses manquées, puis le rappel."""
    problems = gate(result.table)
    if problems:
        pytest.fail("\n".join(problems), pytrace=False)


async def test_the_impact_engine_passes_its_quality_gate_on_the_truth_corpus(
    fresh_engine: EngineFactory, request: pytest.FixtureRequest
) -> None:
    result = await measured(fresh_engine)
    print(render(result))
    report = request.config.getoption("truth_report")
    if report is not None:
        Path(report).write_text(
            json.dumps(as_json(result), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    assert_the_gate_passes(result)


async def test_a_weakened_engine_fails_the_gate_which_names_the_missed_answers(
    fresh_engine: EngineFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(impact, "_ANCESTORS", _PARENT_ONLY)
    result = await measured(fresh_engine)

    with pytest.raises(pytest.fail.Exception) as failure:
        assert_the_gate_passes(result)
    expected = len(result.table.expected)
    recall = f"{(expected - 1) / expected:.3f}".replace(".", ",")
    lines = str(failure.value.msg).splitlines()
    # La réponse manquée, nommée par son scénario, puis le rappel obtenu.
    assert len(lines) == 3
    assert lines[:2] == ["Réponses manquées au niveau table :", "  table-sous-agent/synthese"]
    assert lines[2].startswith(f"Rappel au niveau table : {recall} ({expected - 1}/{expected}), ")


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
