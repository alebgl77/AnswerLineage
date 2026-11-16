"""Un incident déclaré sur l'entreprise synthétique : analysé par un worker, égal à sa vérité.

L'entreprise arrive par le chemin réel (ingestion, file, workers), avec les
relevés de couverture de ses serveurs. L'incident est déclaré par l'API, puis
par la ligne de commande sur un vrai port ; la boucle du worker en fait
l'analyse. Les réponses touchées sont comparées à la vérité de l'entreprise,
établie sans passer par AnswerLineage.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import anyio
import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_api.lineage.trace import answer_key
from answerlineage_cli.exits import Exit
from answerlineage_corpus.cli import DEFAULT_SEED
from answerlineage_corpus.enterprise import Enterprise, generate
from answerlineage_corpus.truth import truth
from answerlineage_workers.queue import run_worker
from cli.tests.fakes import Run, run
from proxy.tests.conftest import running
from tests.test_corpus_to_graph import api, load, report_coverage

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture(scope="module")
def enterprise() -> Enterprise:
    return generate(DEFAULT_SEED)


@pytest.fixture(scope="module")
def first(enterprise: Enterprise) -> dict[str, Any]:
    incident: dict[str, Any] = truth(enterprise)["incidents"][0]
    return incident


@pytest.fixture
async def loaded(engine: AsyncEngine, enterprise: Enterprise) -> AsyncEngine:
    await load(engine, enterprise)
    await report_coverage(engine, enterprise)
    return engine


@pytest.fixture
async def worker(loaded: AsyncEngine) -> AsyncIterator[None]:
    """La boucle d'un worker, comme en production, le temps du test."""
    stop = asyncio.Event()
    task = asyncio.ensure_future(run_worker(loaded, stop, idle_delay=0.01))
    try:
        yield
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=30)


def declaration(first: dict[str, Any]) -> dict[str, Any]:
    return {
        "scope": {"datasets": [first["dataset"]]},
        "window": {"from": first["from"], "to": first["to"]},
        "severity": "critical",
    }


async def analysed(client: httpx.AsyncClient, incident_id: str) -> dict[str, Any]:
    """L'incident, une fois son analyse faite par le worker."""
    for _ in range(3000):
        detail: dict[str, Any] = (await client.get(f"/api/v1/incidents/{incident_id}")).json()
        if detail["analysis"]["state"] != "pending":
            return detail
        await asyncio.sleep(0.01)
    raise AssertionError("l'analyse n'a pas été faite à temps")


def expected_answers(enterprise: Enterprise, first: dict[str, Any]) -> dict[str, list[str]]:
    """Identifiant stable de chaque réponse de la vérité, et ses destinataires."""
    touched = {item["fingerprint"]: item for item in first["answers"]}
    return {
        answer_key(request.run_id, request.answer.fingerprint): sorted(
            touched[request.answer.fingerprint]["recipients"]
        )
        for request in enterprise.requests
        if request.answer is not None and request.answer.fingerprint in touched
    }


@pytest.mark.usefixtures("worker")
async def test_an_incident_declared_by_the_api_is_analyzed_into_its_truth(
    loaded: AsyncEngine, enterprise: Enterprise, first: dict[str, Any]
) -> None:
    async with api(loaded) as client:
        declared = await client.post("/api/v1/incidents", json=declaration(first))
        assert declared.status_code == 201
        incident = declared.json()
        assert (incident["state"], incident["analysis"]["state"]) == ("qualified", "pending")

        detail = await analysed(client, incident["id"])
        listed = (await client.get("/api/v1/incidents")).json()

    analysis, result = detail["analysis"], detail["result"]
    assert (analysis["state"], analysis["error"], analysis["truncated"]) == ("done", None, False)
    assert analysis["counts"] == {
        "answers": len(first["answers"]),
        "recipients": len(first["recipients"]),
        "lower_bound": False,
    }
    assert result["unknown_datasets"] == []
    assert (result["coverage"]["status"], result["coverage"]["holes"]) == ("measured", [])
    assert (result["more"], result["next_cursor"]) == (False, None)
    assert {
        answer["id"]: sorted(answer["recipients"]) for answer in result["answers"]
    } == expected_answers(enterprise, first)
    assert {answer["fingerprint"] for answer in result["answers"]} == {
        answer["fingerprint"] for answer in first["answers"]
    }
    assert [item["id"] for item in listed["incidents"]] == [incident["id"]]
    assert listed["incidents"][0]["analysis"]["counts"] == analysis["counts"]


async def command(monkeypatch: pytest.MonkeyPatch, url: str, *argv: str) -> Run:
    return await anyio.to_thread.run_sync(lambda: run([*argv, "--url", url], None, monkeypatch))


@pytest.mark.usefixtures("worker")
async def test_an_incident_declared_from_the_terminal_is_shown_with_its_truth(
    monkeypatch: pytest.MonkeyPatch,
    loaded: AsyncEngine,
    enterprise: Enterprise,
    first: dict[str, Any],
) -> None:
    dataset = first["dataset"]
    async with running(create_app(loaded)) as url:
        declared = await command(
            monkeypatch,
            url,
            *("incident", "declare", "--dataset", dataset["namespace"], dataset["name"]),
            *("--from", first["from"], "--to", first["to"], "--severity", "high"),
            *("--format", "json"),
        )
        assert declared.code == Exit.OK, declared.err
        incident_id = json.loads(declared.out)["id"]

        async with httpx.AsyncClient(base_url=url) as client:
            direct = await analysed(client, incident_id)

        shown = await command(monkeypatch, url, "incident", "show", incident_id)
        document = await command(
            monkeypatch, url, "incident", "show", incident_id, "--format", "json"
        )

        # Le critère du refus : une fenêtre dont la fin précède le début ne part pas.
        backwards = await command(
            monkeypatch,
            url,
            *("incident", "declare", "--dataset", dataset["namespace"], dataset["name"]),
            *("--from", first["to"], "--to", first["from"], "--severity", "high"),
        )

    assert shown.code == Exit.OK, shown.err
    answers, recipients = len(first["answers"]), len(first["recipients"])
    assert f"{answers} réponses touchées, {recipients} destinataires" in shown.out
    assert "mesurée, 100 % des appels vus sont dans le graphe, sans trou connu" in shown.out
    assert "Liste complète" in shown.out
    for answer_id in expected_answers(enterprise, first):
        assert answer_id in shown.out
    for pseudonym in first["recipients"]:
        assert pseudonym in shown.out

    assert document.code == Exit.OK
    assert json.loads(document.out) == direct

    assert backwards.code == Exit.USAGE
    assert "--to doit suivre --from" in backwards.err
