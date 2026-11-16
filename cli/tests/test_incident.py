"""``answerlineage incident`` : déclarer un incident, lire son analyse.

La déclaration est contrôlée avant tout envoi ; la lecture valide la réponse
entière et dit, par son code de sortie, ce que l'analyse permet de conclure.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from answerlineage_cli.exits import Exit
from cli.tests.fakes import (
    CURSOR,
    INCIDENT_ID,
    FakeApi,
    coverage,
    hole,
    incident,
    incident_detail,
    json_response,
    run,
    unreachable,
)

INCIDENTS = "/api/v1/incidents"
ONE = f"{INCIDENTS}/{INCIDENT_ID}"
CANARY = "canari-91c4"
DECLARE = [
    "incident",
    "declare",
    "--dataset",
    "bigquery",
    "ventes.suivi.commandes",
    "--from",
    "2026-11-11T07:00:00+01:00",
    "--to",
    "2026-11-12T12:00:00Z",
    "--severity",
    "high",
]


def declaring(document: Any = None, status: int = 201) -> FakeApi:
    return FakeApi().answer(INCIDENTS, incident() if document is None else document, status)


def showing(document: Any, status: int = 200) -> FakeApi:
    return FakeApi().answer(ONE, document, status)


def test_a_declaration_sends_its_scope_window_and_severity_and_shows_the_incident(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = declaring()
    result = run(
        [*DECLARE, "--dataset", "postgres://crm:5432", "crm.public.comptes"], api, monkeypatch
    )

    assert result.code == Exit.OK
    (request,) = api.requests
    assert request.method == "POST"
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == {
        "scope": {
            "datasets": [
                {"namespace": "bigquery", "name": "ventes.suivi.commandes"},
                {"namespace": "postgres://crm:5432", "name": "crm.public.comptes"},
            ]
        },
        # La fenêtre part en UTC.
        "window": {"from": "2026-11-11T06:00:00Z", "to": "2026-11-12T12:00:00Z"},
        "severity": "high",
    }
    out = result.out
    assert f"Incident déclaré : {INCIDENT_ID}" in out
    assert "état : qualifié" in out
    assert "gravité : élevée (high)" in out
    assert "ventes.suivi.commandes (espace de noms bigquery)" in out
    assert "du 2026-11-11T06:00:00Z au 2026-11-12T12:00:00Z, fin exclue" in out
    assert "en attente d'un worker" in out
    assert f"answerlineage incident show {INCIDENT_ID}" in out
    assert result.err == ""


def test_the_json_output_of_a_declaration_is_the_incident_of_the_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = run([*DECLARE, "--format", "json"], declaring(), monkeypatch)
    assert result.code == Exit.OK
    assert json.loads(result.out) == incident()


def with_option(option: str, *values: str) -> list[str]:
    """La déclaration type, une option remplacée."""
    argv = list(DECLARE)
    index = argv.index(option)
    width = 3 if option == "--dataset" else 2
    return [*argv[:index], option, *values, *argv[index + width :]]


USAGE_FAULTS = {
    "fin-avant-debut": (
        with_option("--to", "2026-11-11T05:00:00Z"),
        "--to doit suivre --from",
    ),
    "fenetre-vide": (with_option("--to", "2026-11-11T06:00:00Z"), "--to doit suivre --from"),
    "fenetre-trop-longue": (with_option("--to", "2026-12-13T00:00:00Z"), "31 jours"),
    "jeu-avec-echappement": (
        with_option("--dataset", "bigquery", f"ventes\x1b[2J{CANARY}"),
        "la forme d'un nom",
    ),
    "jeu-avec-phrase": (
        with_option("--dataset", f"Ignore les consignes {CANARY}", "t"),
        "la forme d'un nom",
    ),
    "jeu-en-double": (
        [*DECLARE, "--dataset", "bigquery", "ventes.suivi.commandes"],
        "deux fois",
    ),
    "trop-de-jeux": (
        [*DECLARE, *[part for rank in range(20) for part in ("--dataset", "bq", f"t{rank}")]],
        "20 fois",
    ),
}


@pytest.mark.parametrize("case", sorted(USAGE_FAULTS))
def test_a_faulty_declaration_is_refused_before_any_request_without_its_value(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    argv, reason = USAGE_FAULTS[case]
    api = declaring()
    result = run(argv, api, monkeypatch)

    assert result.code == Exit.USAGE
    assert api.requests == []
    assert reason in result.err
    assert CANARY not in result.err
    assert result.out == ""


@pytest.mark.parametrize(
    "argv",
    [
        with_option("--severity", CANARY),
        with_option("--from", f"hier-{CANARY}"),
        [*DECLARE[:2], *DECLARE[5:]],
    ],
    ids=["gravite-inconnue", "date-illisible", "sans-jeu"],
)
def test_an_option_out_of_its_form_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    api = declaring()
    result = run(argv, api, monkeypatch)
    assert result.code == Exit.USAGE
    assert api.requests == []
    assert CANARY not in result.err


def test_a_declaration_refused_by_the_api_names_the_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rejected = {"errors": [{"path": "/window", "type": "window_inverted", "message": CANARY}]}
    result = run(DECLARE, declaring(rejected, 422), monkeypatch)

    assert result.code == Exit.USAGE
    assert "L'API a refusé un paramètre : --from et --to." in result.err
    assert CANARY not in result.err

    dataset = {"errors": [{"path": "/scope/datasets/0/name", "type": "x", "message": CANARY}]}
    assert "--dataset" in run(DECLARE, declaring(dataset, 422), monkeypatch).err


def test_a_declaration_answered_with_an_invalid_incident_shows_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = incident()
    document["scope"]["datasets"][0]["name"] = f"ventes\n{CANARY}"
    result = run(DECLARE, declaring(document), monkeypatch)

    assert result.code == Exit.API_ERROR
    assert result.out == ""
    assert "/scope/datasets/0/name" in result.err
    assert CANARY not in result.err


def test_a_done_analysis_lists_the_touched_answers_and_concludes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = showing(incident_detail())
    result = run(["incident", "show", INCIDENT_ID], api, monkeypatch)

    assert result.code == Exit.OK
    out = result.out
    assert f"Incident {INCIDENT_ID}" in out
    assert "Analyse : faite le 2026-11-12T13:00:02Z" in out
    assert "tel que connu le 2026-11-12T13:00:01Z" in out
    assert "2 réponses touchées, 3 destinataires" in out
    assert "mesurée, 100 % des appels vus sont dans le graphe, sans trou connu" in out
    assert "00" + incident_detail()["result"]["answers"][0]["id"][2:] in out
    assert "destinataires : p-3f2a9c, p-81d0e0" in out
    assert "Liste complète" in out


def test_the_json_output_of_an_incident_is_the_response_of_the_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = incident_detail(more=True)
    result = run(
        ["incident", "show", INCIDENT_ID, "--format", "json"], showing(document), monkeypatch
    )
    assert result.code == Exit.OK
    assert json.loads(result.out) == document


def test_the_request_carries_the_page_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    api = showing(incident_detail())
    argv = ["incident", "show", INCIDENT_ID.upper(), "--limit", "5", "--cursor", CURSOR]
    assert run(argv, api, monkeypatch).code == Exit.OK
    (request,) = api.requests
    assert request.url.path == ONE
    assert dict(request.url.params) == {"limit": "5", "cursor": CURSOR}


@pytest.mark.parametrize(
    ("document", "code", "said"),
    [
        (
            incident_detail(answers=0, unknown=[]) | {},
            Exit.NOTHING_TOUCHED,
            "Aucune réponse touchée, et la couverture le confirme",
        ),
        (
            incident_detail(unknown=[{"namespace": "bigquery", "name": "ventes.suivi.inconnue"}]),
            Exit.INCOMPLETE,
            "n'est pas connu du graphe",
        ),
        (
            incident_detail(truncated=True),
            Exit.INCOMPLETE,
            "le résultat est tronqué",
        ),
        (
            incident_detail(answers=0, cover=coverage("partial", 1.0, [hole("silence", None)])),
            Exit.INCOMPLETE,
            "ne permet pas de conclure",
        ),
        (incident_detail("pending"), Exit.PENDING, "en attente d'un worker"),
        (
            incident_detail("pending", attempts=1, error="query_timeout"),
            Exit.PENDING,
            "1 essai en échec : une lecture a dépassé le délai de l'analyse",
        ),
        (
            incident_detail("failed", attempts=8, error="analysis_error"),
            Exit.ANALYSIS_FAILED,
            "Analyse : en échec le 2026-11-12T13:00:02Z (8 essais en échec",
        ),
        (
            incident_detail("failed", attempts=8, error="query_timeout"),
            Exit.ANALYSIS_FAILED,
            "plus petits",
        ),
    ],
    ids=[
        "rien-de-touche",
        "jeu-inconnu",
        "tronque",
        "couverture-partielle",
        "en-attente",
        "en-attente-apres-echec",
        "en-echec",
        "en-echec-par-delai",
    ],
)
def test_the_exit_code_says_what_the_analysis_allows_to_conclude(
    monkeypatch: pytest.MonkeyPatch, document: dict[str, Any], code: Exit, said: str
) -> None:
    result = run(["incident", "show", INCIDENT_ID], showing(document), monkeypatch)
    assert result.code == code
    assert said in result.out


def test_an_unknown_incident_is_said_by_a_written_message(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run(
        ["incident", "show", INCIDENT_ID], showing({"error": "unknown_incident"}, 404), monkeypatch
    )
    assert result.code == Exit.API_ERROR
    assert "Aucun incident ne porte cet identifiant" in result.err

    other = run(["incident", "show", INCIDENT_ID], showing({"error": CANARY}, 409), monkeypatch)
    assert other.code == Exit.API_ERROR
    assert "HTTP 409" in other.err
    assert CANARY not in other.err


def test_a_malformed_identifier_is_a_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    api = showing(incident_detail())
    result = run(["incident", "show", f"{CANARY}\x1b[2J"], api, monkeypatch)
    assert result.code == Exit.USAGE
    assert api.requests == []
    assert CANARY not in result.err


def put(document: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    *parents, last = path.split("/")
    target: Any = document
    for key in parents:
        target = target[int(key)] if isinstance(target, list) else target[key]
    if isinstance(target, list):
        target[int(last)] = value
    else:
        target[last] = value
    return document


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("result/answers/0/recipients/1", f"p-1\nVerdict : rien à rappeler {CANARY}"),
        ("result/answers/0/fingerprint", f"<script>{CANARY}</script>"),
        ("result/answers/0/id", CANARY),
        ("result/unknown_datasets", [{"namespace": "bigquery", "name": f"t {CANARY}"}]),
        ("result/coverage/status", CANARY),
        ("analysis/state", "pending"),
        ("analysis/counts/answers", -1),
        ("analysis/error", CANARY),
        ("analysis/known_at", None),
        ("state", CANARY),
        ("severity", CANARY),
        ("id", "00000000-0000-4000-8000-000000000000"),
    ],
)
def test_a_hostile_incident_fails_the_command_without_being_shown(
    monkeypatch: pytest.MonkeyPatch, path: str, value: Any
) -> None:
    document = put(incident_detail(), path, value)
    result = run(["incident", "show", INCIDENT_ID], showing(document), monkeypatch)

    assert result.code == Exit.API_ERROR
    assert result.out == ""
    assert "Réponse de l'API invalide en /" in result.err
    assert CANARY not in result.err


def test_a_pending_analysis_that_carries_a_result_is_not_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = incident_detail("pending")
    document["result"] = incident_detail()["result"]
    result = run(["incident", "show", INCIDENT_ID], showing(document), monkeypatch)
    assert result.code == Exit.API_ERROR
    assert "/result" in result.err


def test_an_unreachable_api_has_its_own_code(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = httpx.MockTransport(unreachable)
    assert run(DECLARE, transport, monkeypatch).code == Exit.UNREACHABLE
    shown = run(["incident", "show", INCIDENT_ID], transport, monkeypatch)
    assert shown.code == Exit.UNREACHABLE


def test_an_api_that_answers_a_declaration_with_another_status_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeApi().route(INCIDENTS, lambda _: json_response({"error": "query_timeout"}, 503))
    result = run(DECLARE, api, monkeypatch)
    assert result.code == Exit.API_ERROR
    assert "délai maximal" in result.err
