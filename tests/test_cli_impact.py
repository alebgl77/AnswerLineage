"""``answerlineage impact`` contre la vraie API, sur un vrai port, devant l'entreprise synthétique.

L'entreprise arrive par le chemin réel (ingestion, file, workers), avec les
relevés de couverture de ses serveurs. La commande tourne dans un fil à part,
comme dans un terminal ; ce qu'elle affiche est comparé à la vérité de
l'entreprise, établie sans passer par AnswerLineage.
"""

from __future__ import annotations

import json
import socket
from collections.abc import AsyncIterator
from typing import Any

import anyio
import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_api.auth.roles import PERSON_ROLES
from answerlineage_api.lineage.trace import answer_key
from answerlineage_cli.client import ENV_TOKEN
from answerlineage_cli.exits import Exit
from answerlineage_corpus.cli import DEFAULT_SEED
from answerlineage_corpus.enterprise import Enterprise, generate
from answerlineage_corpus.truth import truth
from api.tests.issuer import TEST_ISSUER, authentication, signed_in
from cli.tests.fakes import Run, run
from proxy.tests.conftest import running
from tests.test_corpus_to_graph import load, report_coverage

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture(autouse=True)
def command_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """La commande présente le jeton d'une personne qui a tous les rôles de personne."""
    monkeypatch.setenv(ENV_TOKEN, TEST_ISSUER.token(*PERSON_ROLES))


@pytest.fixture(scope="module")
def enterprise() -> Enterprise:
    return generate(DEFAULT_SEED)


@pytest.fixture(scope="module")
def first(enterprise: Enterprise) -> dict[str, Any]:
    incident: dict[str, Any] = truth(enterprise)["incidents"][0]
    return incident


@pytest.fixture
async def api(engine: AsyncEngine, enterprise: Enterprise) -> AsyncIterator[str]:
    """Adresse de l'API, servie devant l'entreprise et ses relevés de couverture."""
    await load(engine, enterprise)
    await report_coverage(engine, enterprise)
    async with running(create_app(engine, authentication=authentication())) as url:
        yield url


async def command(monkeypatch: pytest.MonkeyPatch, url: str, *argv: str) -> Run:
    return await anyio.to_thread.run_sync(lambda: run([*argv, "--url", url], None, monkeypatch))


def options(dataset: dict[str, str], start: str, end: str) -> list[str]:
    namespace, name = dataset["namespace"], dataset["name"]
    return ["impact", "--namespace", namespace, "--name", name, "--from", start, "--to", end]


async def test_the_first_incident_reads_like_its_truth_in_text_and_in_json(
    monkeypatch: pytest.MonkeyPatch, api: str, enterprise: Enterprise, first: dict[str, Any]
) -> None:
    argv = options(first["dataset"], first["from"], first["to"])
    answers, recipients = len(first["answers"]), len(first["recipients"])

    text = await command(monkeypatch, api, *argv)
    assert text.code == Exit.OK
    assert f"{answers} réponses touchées, {recipients} destinataires" in text.out
    assert "mesurée, 100 % des appels vus sont dans le graphe, sans trou connu" in text.out
    assert "Liste complète" in text.out
    touched = {(item["fingerprint"], item["messageRef"]) for item in first["answers"]}
    for request in enterprise.requests:
        if request.answer is not None and (
            (request.answer.fingerprint, request.answer.message_ref) in touched
        ):
            assert answer_key(request.run_id, request.answer.fingerprint) in text.out
    for pseudonym in first["recipients"]:
        assert pseudonym in text.out

    shown = await command(monkeypatch, api, *argv, "--format", "json")
    assert shown.code == Exit.OK
    async with httpx.AsyncClient(base_url=api, headers=signed_in()) as client:
        direct = await client.get(
            "/api/v1/impact",
            params={**first["dataset"], "from": first["from"], "to": first["to"]},
        )
    assert json.loads(shown.out) == direct.json()


async def test_a_window_without_read_under_a_measured_coverage_touches_nothing(
    monkeypatch: pytest.MonkeyPatch, api: str, enterprise: Enterprise, first: dict[str, Any]
) -> None:
    # Le premier jour couvert par les relevés commence à minuit ; aucune demande à cette heure.
    day = min(request.started_at for request in enterprise.requests).date().isoformat()
    argv = options(first["dataset"], f"{day}T00:00:00Z", f"{day}T00:00:01Z")

    result = await command(monkeypatch, api, *argv)
    assert result.code == Exit.NOTHING_TOUCHED
    assert "0 réponse touchée, 0 destinataire" in result.out
    assert "la couverture le confirme" in result.out


async def test_without_any_report_on_the_window_nothing_can_be_concluded(
    monkeypatch: pytest.MonkeyPatch, api: str, first: dict[str, Any]
) -> None:
    argv = options(first["dataset"], "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z")

    result = await command(monkeypatch, api, *argv)
    assert result.code == Exit.INCOMPLETE
    assert "non mesurée" in result.out
    assert "ne permet pas de conclure qu'aucune réponse n'est touchée" in result.out


async def test_touched_answers_on_a_partly_covered_window_are_a_minimum(
    monkeypatch: pytest.MonkeyPatch, api: str, first: dict[str, Any]
) -> None:
    argv = options(first["dataset"], "2026-10-01T00:00:00Z", first["to"])

    result = await command(monkeypatch, api, *argv, "--format", "json")
    assert result.code == Exit.INCOMPLETE
    document = json.loads(result.out)
    assert document["coverage"]["status"] == "partial"
    assert document["counts"]["answers"] >= len(first["answers"])


async def test_an_unknown_dataset_is_refused_by_the_api(
    monkeypatch: pytest.MonkeyPatch, engine: AsyncEngine, first: dict[str, Any]
) -> None:
    unknown = {"namespace": "bigquery", "name": "ventes.suivi.inconnue"}
    async with running(create_app(engine, authentication=authentication())) as empty:
        result = await command(monkeypatch, empty, *options(unknown, first["from"], first["to"]))
    assert result.code == Exit.API_ERROR
    assert "n'est pas connu du graphe" in result.err


async def test_a_usage_error_and_an_unreachable_api_have_their_own_codes(
    monkeypatch: pytest.MonkeyPatch, first: dict[str, Any]
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = f"http://127.0.0.1:{probe.getsockname()[1]}"
    # L'usage fautif se dit avant toute requête.
    backwards = options(first["dataset"], first["to"], first["from"])
    assert (await command(monkeypatch, closed, *backwards)).code == Exit.USAGE

    argv = options(first["dataset"], first["from"], first["to"])
    result = await command(monkeypatch, closed, *argv)
    assert result.code == Exit.UNREACHABLE
    assert closed in result.err
