"""``answerlineage impact`` : la propagation d'un incident, lisible ou en JSON.

Le code de sortie distingue « rien n'est touché » de « je ne sais pas » :
seul un résultat complet, sous une couverture mesurée et sans trou, conclut.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from answerlineage_cli.exits import Exit
from cli.tests.fakes import (
    ANSWER_ID,
    CURSOR,
    INCIDENT,
    FakeApi,
    coverage,
    hole,
    impact_result,
    run,
    unreachable,
)

IMPACT = "/api/v1/impact"


def api_with(document: Any, status: int = 200) -> FakeApi:
    return FakeApi().answer(IMPACT, document, status)


def test_touched_answers_with_a_complete_result_are_listed_and_succeed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = run(["impact", *INCIDENT], api_with(impact_result(2)), monkeypatch)

    assert result.code == Exit.OK
    out = result.out
    assert "ventes.suivi.commandes" in out
    assert "bigquery" in out
    assert "2 réponses touchées, 2 destinataires" in out
    assert "mesurée, 100 % des appels vus sont dans le graphe, sans trou connu" in out
    assert ANSWER_ID in out
    assert "assistant-commercial (agent://ventes)" in out
    assert "execute_sql (entrepot)" in out
    assert "lister_opportunites (crm)" in out
    assert "p-3f2a9c" in out
    assert "p-81d0e4" in out
    assert "crm.public.comptes" in out
    assert "Liste complète" in out
    assert result.err == ""


def test_the_json_output_is_the_response_of_the_api(monkeypatch: pytest.MonkeyPatch) -> None:
    document = impact_result(3, known_at="2026-11-12T08:00:00Z", more=True)
    result = run(["impact", *INCIDENT, "--format", "json"], api_with(document), monkeypatch)

    assert result.code == Exit.OK
    assert json.loads(result.out) == document


def test_the_request_carries_every_parameter_of_the_route(monkeypatch: pytest.MonkeyPatch) -> None:
    api = api_with(impact_result(1))
    options = [
        "--namespace",
        "bigquery",
        "--name",
        "ventes.suivi.commandes",
        "--from",
        "2026-11-11T07:00:00+01:00",
        "--to",
        "2026-11-12t12:00:00z",
        "--limit",
        "50",
        "--cursor",
        CURSOR,
        "--known-at",
        "2026-11-12T08:00:00Z",
    ]
    assert run(["impact", *options], api, monkeypatch).code == Exit.OK

    (request,) = api.requests
    assert request.url.path == IMPACT
    assert dict(request.url.params) == {
        "namespace": "bigquery",
        "name": "ventes.suivi.commandes",
        "from": "2026-11-11T06:00:00Z",
        "to": "2026-11-12T12:00:00Z",
        "limit": "50",
        "cursor": CURSOR,
        "known_at": "2026-11-12T08:00:00Z",
    }
    # Le client ne compresse pas : une réponse se borne en octets reçus comme lus.
    assert request.headers["accept-encoding"] == "identity"


def test_nothing_touched_under_a_measured_coverage_without_hole_is_a_conclusion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = run(["impact", *INCIDENT], api_with(impact_result(0)), monkeypatch)

    assert result.code == Exit.NOTHING_TOUCHED
    assert "Aucune réponse touchée" in result.out
    assert "la couverture le confirme" in result.out


@pytest.mark.parametrize(
    ("cover", "said"),
    [
        (coverage("unknown", None), "non mesurée"),
        (coverage("partial", 1.0, [hole("silence", None)]), "partielle"),
        (coverage("measured", 0.75, [hole("missing_from_graph", 3)]), "75 %"),
        (coverage("measured", 1.0, truncated=True), "mesure partielle"),
    ],
    ids=["unknown", "partial", "hole", "truncated-coverage"],
)
def test_nothing_found_without_a_complete_coverage_is_not_a_conclusion(
    monkeypatch: pytest.MonkeyPatch, cover: dict[str, Any], said: str
) -> None:
    result = run(["impact", *INCIDENT], api_with(impact_result(0, cover=cover)), monkeypatch)

    assert result.code == Exit.INCOMPLETE
    assert said in result.out
    assert "ne permet pas de conclure qu'aucune réponse n'est touchée" in result.out


def test_touched_answers_under_a_partial_coverage_may_not_be_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cover = coverage("partial", 1.0, [hole("before_first_report", None)])
    result = run(["impact", *INCIDENT], api_with(impact_result(2, cover=cover)), monkeypatch)

    assert result.code == Exit.INCOMPLETE
    assert "mcp://crm : avant son premier relevé" in result.out
    assert "D'autres réponses peuvent être touchées" in result.out


def test_a_truncated_result_says_its_counts_are_minimums(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run(["impact", *INCIDENT], api_with(impact_result(2, truncated=True)), monkeypatch)

    assert result.code == Exit.INCOMPLETE
    assert "au moins 2 réponses touchées, au moins 2 destinataires" in result.out
    assert "tronqué" in result.out
    assert "Réduire la fenêtre" in result.out


@pytest.mark.parametrize(
    ("kind", "count", "said"),
    [
        ("events_lost", 1, "1 événement perdu"),
        ("missing_from_graph", 4, "4 événements absents du graphe"),
        ("uninspected", 2, "2 requêtes non inspectées"),
        ("silence", None, "aucun relevé (silence)"),
        ("before_first_report", None, "avant son premier relevé"),
    ],
)
def test_every_known_hole_is_named_with_its_emitter_and_window(
    monkeypatch: pytest.MonkeyPatch, kind: str, count: int | None, said: str
) -> None:
    cover = coverage("partial", 0.5, [hole(kind, count, emitter="mcp://entrepot")])
    result = run(["impact", *INCIDENT], api_with(impact_result(1, cover=cover)), monkeypatch)

    assert result.code == Exit.INCOMPLETE
    assert f"mcp://entrepot : {said}, du 2026-11-11T00:00:00Z au 2026-11-12T00:00:00Z" in result.out


def test_a_rate_is_never_rounded_up(monkeypatch: pytest.MonkeyPatch) -> None:
    cover = coverage("measured", 0.9996, [hole("missing_from_graph", 1)])
    result = run(["impact", *INCIDENT], api_with(impact_result(1, cover=cover)), monkeypatch)
    assert "99,9 %" in result.out


def test_the_next_page_and_the_known_date_are_said(monkeypatch: pytest.MonkeyPatch) -> None:
    document = impact_result(1, more=True, known_at="2026-11-12T08:00:00Z")
    result = run(["impact", *INCIDENT], api_with(document), monkeypatch)

    assert f"--cursor {CURSOR}" in result.out
    assert "tel que connu le 2026-11-12T08:00:00Z" in result.out


def test_no_coverage_seen_says_no_call_was_seen(monkeypatch: pytest.MonkeyPatch) -> None:
    cover = coverage("measured", None)
    result = run(["impact", *INCIDENT], api_with(impact_result(0, cover=cover)), monkeypatch)
    assert result.code == Exit.NOTHING_TOUCHED
    assert "aucun appel vu" in result.out


@pytest.mark.parametrize(
    "options",
    [
        ["--name", "ventes.suivi.commandes", "--from", "2026-11-11T06:00:00Z"],
        [*INCIDENT[:5], "2026-11-11T06:00:00", *INCIDENT[6:]],
        [*INCIDENT[:5], "le 11 novembre", *INCIDENT[6:]],
        [*INCIDENT[:7], "2026-11-11T06:00:00Z"],
        [*INCIDENT[:7], "2026-11-10T06:00:00Z"],
        ["--namespace", "", *INCIDENT[2:]],
        ["--namespace", "a" * 501, *INCIDENT[2:]],
        ["--namespace", "big\x00query", *INCIDENT[2:]],
        [*INCIDENT, "--limit", "0"],
        [*INCIDENT, "--limit", "201"],
        [*INCIDENT, "--limit", "dix"],
        [*INCIDENT, "--cursor", "pas-un-curseur"],
        [*INCIDENT, "--known-at", "1969-12-31T23:59:59Z"],
        [*INCIDENT, "--known-at", "2026-11-12"],
        [*INCIDENT, "--format", "yaml"],
        [*INCIDENT, "--timeout", "0"],
        [*INCIDENT, "--timeout", "601"],
    ],
)
def test_a_usage_error_sends_nothing(monkeypatch: pytest.MonkeyPatch, options: list[str]) -> None:
    api = api_with(impact_result(1))
    result = run(["impact", *options], api, monkeypatch)

    assert result.code == Exit.USAGE
    assert api.requests == []
    assert result.out == ""


def test_an_unknown_dataset_is_said_as_such(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run(["impact", *INCIDENT], api_with({"error": "unknown_dataset"}, 404), monkeypatch)

    assert result.code == Exit.API_ERROR
    assert "Ce jeu de données n'est pas connu du graphe" in result.err
    assert result.out == ""


def test_a_parameter_refused_by_the_api_is_a_usage_error_named_by_its_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refusal = {"errors": [{"path": "/query/known_at", "type": "x", "message": "canari-motif"}]}
    result = run(["impact", *INCIDENT], api_with(refusal, 422), monkeypatch)

    assert result.code == Exit.USAGE
    assert "--known-at" in result.err
    assert "canari" not in result.err


@pytest.mark.parametrize(
    ("status", "body", "said"),
    [
        (503, {"error": "query_timeout"}, "délai maximal"),
        (403, {"error": "host_not_allowed"}, "ANSWERLINEAGE_ALLOWED_HOSTS"),
        (403, {"error": "origin_not_allowed"}, "ANSWERLINEAGE_ALLOWED_ORIGINS"),
        (403, {"error": "cross_site_not_allowed"}, "refuse"),
        (500, {"detail": "canari-trace"}, "HTTP 500"),
        (418, {"error": "canari-code"}, "HTTP 418"),
        (422, {"errors": [{"path": "/canari", "type": "x", "message": "y"}]}, "un paramètre"),
        (422, {"errors": "canari"}, "un paramètre"),
    ],
)
def test_a_refusal_of_the_api_is_said_without_copying_what_it_sent(
    monkeypatch: pytest.MonkeyPatch, status: int, body: Any, said: str
) -> None:
    result = run(["impact", *INCIDENT], api_with(body, status), monkeypatch)

    assert result.code in (Exit.API_ERROR, Exit.USAGE)
    assert said in result.err
    assert "canari" not in result.err + result.out


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout])
def test_an_unreachable_api_has_its_own_exit_code(
    monkeypatch: pytest.MonkeyPatch, failure: type[httpx.TransportError]
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise failure("canari-cause", request=request)

    result = run(["impact", *INCIDENT], httpx.MockTransport(fail), monkeypatch)

    assert result.code == Exit.UNREACHABLE
    assert "L'API ne répond pas" in result.err
    assert "http://127.0.0.1:8000" in result.err
    assert "canari" not in result.err


def test_the_address_comes_from_the_option_then_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Derrière un relais, l'API peut être servie sous un chemin.
    api = api_with(impact_result(1)).answer("/al" + IMPACT, impact_result(1))
    monkeypatch.setenv("ANSWERLINEAGE_API_URL", "https://lignage.exemple.fr/al/")
    assert run(["impact", *INCIDENT], api, monkeypatch).code == Exit.OK
    assert run(["impact", *INCIDENT, "--url", "http://[::1]:9000"], api, monkeypatch).code == 0
    first, second = (str(request.url).split("?")[0] for request in api.requests)
    assert first == "https://lignage.exemple.fr/al/api/v1/impact"
    assert second == "http://[::1]:9000/api/v1/impact"


def test_an_unreachable_address_is_named_in_its_message(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run(
        ["impact", *INCIDENT, "--url", "http://lignage.exemple.fr:8000"],
        httpx.MockTransport(unreachable),
        monkeypatch,
    )
    assert result.err == (
        "L'API ne répond pas à http://lignage.exemple.fr:8000, ou pas dans le délai : "
        "vérifier qu'elle tourne, et l'adresse (--url ou ANSWERLINEAGE_API_URL).\n"
    )
