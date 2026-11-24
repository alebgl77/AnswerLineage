"""Le score d'exposition, mesuré sur le corpus de vérité.

Chaque scénario qui tient un ordre d'exposition passe par le chemin réel, dans
sa propre base : ingestion, file, workers, puis classement par l'API à l'heure
de référence du scénario. L'ordre des jeux de données et de leurs colonnes est
comparé à celui que le scénario écrit à la main ; il ne se calcule jamais avec
le produit. ``answerlineage exposure`` lit le même classement, sur un vrai port.
"""

from __future__ import annotations

import json
from typing import Any

import anyio
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_api.auth.roles import PERSON_ROLES
from answerlineage_cli.client import ENV_TOKEN
from answerlineage_cli.exits import Exit
from answerlineage_corpus.scenarios import Scenario, load_corpus
from api.tests.issuer import TEST_ISSUER, authentication
from cli.tests.fakes import Run, run
from proxy.tests.conftest import running
from tests.test_corpus_to_graph import api, ingest

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture(autouse=True)
def command_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """La commande présente le jeton d'une personne qui a tous les rôles de personne."""
    monkeypatch.setenv(ENV_TOKEN, TEST_ISSUER.token(*PERSON_ROLES))


EXPOSED = [scenario for scenario in load_corpus() if scenario.exposure is not None]


async def ranked(engine: AsyncEngine, scenario: Scenario) -> dict[str, Any]:
    """Le classement que l'API rend du scénario, à son heure de référence."""
    assert scenario.exposure is not None
    await ingest(engine, scenario.events())
    async with api(engine) as client:
        response = await client.get(
            "/api/v1/exposure", params={"at": scenario.exposure.at.isoformat(), "limit": 200}
        )
    assert response.status_code == 200
    result: dict[str, Any] = response.json()
    assert result["truncated"] is False
    assert result["more"] is False
    return result


def test_at_least_three_scenarios_hold_an_exposure_order() -> None:
    assert len(EXPOSED) >= 3


@pytest.mark.parametrize("scenario", EXPOSED, ids=lambda scenario: scenario.name)
async def test_the_datasets_and_their_columns_come_in_the_expected_order(
    engine: AsyncEngine, scenario: Scenario
) -> None:
    expected = scenario.exposure
    assert expected is not None
    result = await ranked(engine, scenario)

    assert [(item["namespace"], item["name"]) for item in result["datasets"]] == [
        (dataset.namespace, dataset.name) for dataset in expected.datasets
    ], scenario.name
    columns = {(item["namespace"], item["name"]): item["columns"] for item in result["datasets"]}
    for dataset, names in expected.columns:
        found = columns[(dataset.namespace, dataset.name)]
        assert [column["name"] for column in found] == list(names), (scenario.name, dataset.name)
    for dataset in expected.datasets:
        if dataset not in dict(expected.columns):
            assert columns[(dataset.namespace, dataset.name)] == [], (scenario.name, dataset.name)


async def test_the_answers_and_recipients_of_a_dataset_are_those_of_the_scenario(
    engine: AsyncEngine,
) -> None:
    scenario = next(item for item in EXPOSED if item.name == "exposition-destinataires")
    result = await ranked(engine, scenario)

    for item in result["datasets"]:
        readers = [
            run.answer
            for run in scenario.runs
            if run.answer is not None
            and any(read.dataset.name == item["name"] for call in run.calls for read in call.reads)
        ]
        assert item["answers"] == len(readers)
        assert item["recipients"] == len(
            {pseudonym for answer in readers for pseudonym in answer.recipients}
        )


async def command(monkeypatch: pytest.MonkeyPatch, url: str, *argv: str) -> Run:
    return await anyio.to_thread.run_sync(lambda: run([*argv, "--url", url], None, monkeypatch))


async def test_the_command_reads_the_ranking_of_a_scenario_in_text_and_in_json(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = next(item for item in EXPOSED if item.name == "exposition-anciennes-recentes")
    assert scenario.exposure is not None
    direct = await ranked(engine, scenario)
    at = scenario.exposure.at.isoformat()

    async with running(create_app(engine, authentication=authentication())) as url:
        text = await command(monkeypatch, url, "exposure", "--at", at)
        shown = await command(
            monkeypatch, url, "exposure", "--at", at, "--limit", "200", "--format", "json"
        )

    assert text.code == Exit.OK
    positions = [text.out.index(f"  {dataset.name} (") for dataset in scenario.exposure.datasets]
    assert positions == sorted(positions)
    assert "Classement complet" in text.out
    assert shown.code == Exit.OK
    assert json.loads(shown.out) == direct
