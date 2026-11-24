"""``answerlineage exposure`` : les jeux de données les plus exposés, lisibles ou en JSON.

Le code de sortie dit si le classement est complet : une borne dépassée rend
les scores des minimums, et la commande le dit.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from answerlineage_cli import exposure
from answerlineage_cli.exits import Exit
from cli.tests.fakes import EXPOSURE_CURSOR, FakeApi, exposure_result, run, unreachable

EXPOSURE = "/api/v1/exposure"
CANARY = "canari-5d2e"


def api_with(document: Any, status: int = 200) -> FakeApi:
    return FakeApi().answer(EXPOSURE, document, status)


def test_a_complete_ranking_is_shown_most_exposed_first_and_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = run(["exposure"], api_with(exposure_result()), monkeypatch)

    assert result.code == Exit.OK
    assert result.err == ""
    lines = result.out.splitlines()
    assert lines[0] == "Exposition des jeux de données au 2026-11-01T12:00:00Z"
    assert "pèse 1 plus un par destinataire, moitié moins tous les 30 jours" in lines[1]
    assert lines[3:10] == [
        "Jeux de données, les plus exposés d'abord (2 sur cette page) :",
        "  10,192078  finance.ref.taux_change (espace de noms bigquery, table)",
        "    5 réponses, 6 destinataires",
        "    colonnes : date_valeur 6,423125 (2 réponses), devise 5,315637 (4 réponses)",
        "    d'autres colonnes, moins exposées, suivent",
        "  4  crm.public.comptes (espace de noms postgres://crm:5432, table)",
        "    1 réponse, 3 destinataires",
    ]
    assert lines[-1].startswith("Classement complet")


def test_the_json_output_is_the_response_of_the_api(monkeypatch: pytest.MonkeyPatch) -> None:
    document = exposure_result(more=True, known_at="2026-11-01T08:00:00Z")
    result = run(["exposure", "--format", "json"], api_with(document), monkeypatch)

    assert result.code == Exit.OK
    assert json.loads(result.out) == document


def test_the_request_carries_every_parameter_of_the_route(monkeypatch: pytest.MonkeyPatch) -> None:
    api = api_with(exposure_result())
    options = [
        "--limit",
        "20",
        "--cursor",
        EXPOSURE_CURSOR,
        "--at",
        "2026-11-01T13:00:00+01:00",
        "--known-at",
        "2026-11-01t08:00:00z",
    ]
    assert run(["exposure", *options], api, monkeypatch).code == Exit.OK

    (request,) = api.requests
    assert request.url.path == EXPOSURE
    assert dict(request.url.params) == {
        "limit": "20",
        "cursor": EXPOSURE_CURSOR,
        "at": "2026-11-01T12:00:00Z",
        "known_at": "2026-11-01T08:00:00Z",
    }
    assert request.headers["accept-encoding"] == "identity"


def test_without_option_the_request_leaves_the_defaults_to_the_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = api_with(exposure_result())
    assert run(["exposure"], api, monkeypatch).code == Exit.OK
    (request,) = api.requests
    assert dict(request.url.params) == {}


def test_a_truncated_ranking_says_its_scores_are_minimums(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run(["exposure"], api_with(exposure_result(truncated=True)), monkeypatch)

    assert result.code == Exit.INCOMPLETE
    assert "Classement incomplet" in result.out
    assert "les scores sont des minimums" in result.out.lower()


def test_a_following_page_and_the_known_date_are_named(monkeypatch: pytest.MonkeyPatch) -> None:
    document = exposure_result(more=True, known_at="2026-11-01T08:00:00Z")
    result = run(["exposure"], api_with(document), monkeypatch)

    assert result.code == Exit.OK
    assert "Graphe : tel que connu le 2026-11-01T08:00:00Z" in result.out
    assert f"relancer avec --cursor {EXPOSURE_CURSOR}" in result.out


def test_a_graph_without_answer_exposes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    document = exposure_result() | {"datasets": []}
    result = run(["exposure"], api_with(document), monkeypatch)

    assert result.code == Exit.OK
    assert "Aucun jeu de données n'est exposé : aucune réponse n'en dépend." in result.out


@pytest.mark.parametrize(
    "argv",
    [
        ["--limit", "0"],
        ["--limit", "201"],
        ["--cursor", "hier"],
        ["--at", "hier"],
        ["--at", "2026-11-01T12:00:00"],
        ["--at", "1969-12-31T23:59:59Z"],
        ["--known-at", "1969-12-31T23:59:59Z"],
        ["--format", "xml"],
    ],
)
def test_a_wrong_option_is_a_usage_fault_and_nothing_is_asked(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    api = api_with(exposure_result())
    result = run(["exposure", *argv], api, monkeypatch)
    assert result.code == Exit.USAGE
    assert result.out == ""
    assert api.requests == []


@pytest.mark.parametrize(
    ("status", "document", "code", "message"),
    [
        (
            422,
            {"errors": [{"path": "/query/at", "type": "x", "message": "y"}]},
            Exit.USAGE,
            "L'API a refusé un paramètre : --at.",
        ),
        (
            422,
            {"errors": [{"path": "/query/cursor", "type": "x", "message": "y"}]},
            Exit.USAGE,
            "L'API a refusé un paramètre : --cursor.",
        ),
        (
            422,
            {"errors": [{"path": f"/query/{CANARY}", "type": "x", "message": "y"}]},
            Exit.USAGE,
            "L'API a refusé un paramètre de la requête.",
        ),
        (
            503,
            {"error": "query_timeout"},
            Exit.API_ERROR,
            "La lecture a dépassé son délai maximal",
        ),
        (
            403,
            {"error": "host_not_allowed"},
            Exit.API_ERROR,
            "ANSWERLINEAGE_ALLOWED_HOSTS",
        ),
        (500, {"error": CANARY}, Exit.API_ERROR, "L'API a répondu HTTP 500"),
        (500, [CANARY], Exit.API_ERROR, "L'API a répondu HTTP 500"),
    ],
)
def test_a_refusal_of_the_api_is_said_by_a_message_written_in_advance(
    monkeypatch: pytest.MonkeyPatch, status: int, document: Any, code: int, message: str
) -> None:
    result = run(["exposure"], api_with(document, status), monkeypatch)
    assert result.code == code
    assert result.out == ""
    assert message in result.err
    assert CANARY not in result.err


def test_a_refusal_that_is_not_json_is_named_by_its_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeApi().route(EXPOSURE, lambda _: httpx.Response(502, text=f"<html>{CANARY}</html>"))
    result = run(["exposure"], api, monkeypatch)
    assert result.code == Exit.API_ERROR
    assert "HTTP 502" in result.err
    assert CANARY not in result.err


def test_an_unreachable_api_is_said_so(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run(["exposure"], httpx.MockTransport(unreachable), monkeypatch)
    assert result.code == Exit.UNREACHABLE


def changed(edit: Callable[[dict[str, Any]], object]) -> dict[str, Any]:
    document = exposure_result()
    edit(document)
    return document


def first(field: str, value: Any) -> Callable[[dict[str, Any]], object]:
    return lambda document: document["datasets"][0].__setitem__(field, value)


def first_column(field: str, value: Any) -> Callable[[dict[str, Any]], object]:
    return lambda document: document["datasets"][0]["columns"][0].__setitem__(field, value)


HOSTILE: dict[str, tuple[dict[str, Any], str]] = {
    "terminal-escape-in-a-name": (
        changed(first("name", f"ventes\x1b[2J{CANARY}")),
        "/datasets/0/name",
    ),
    "newline-in-a-namespace": (
        changed(first("namespace", f"bigquery\nClassement complet {CANARY}")),
        "/datasets/0/namespace",
    ),
    "markup-in-a-column": (
        changed(first_column("name", f"<script>{CANARY}</script>")),
        "/datasets/0/columns/0/name",
    ),
    "negative-score": (changed(first("score", -1)), "/datasets/0/score"),
    "score-as-text": (changed(first("score", "10")), "/datasets/0/score"),
    "score-as-boolean": (changed(first("score", True)), "/datasets/0/score"),
    "column-score-as-text": (
        changed(first_column("score", CANARY)),
        "/datasets/0/columns/0/score",
    ),
    "count-as-text": (changed(first("answers", "5")), "/datasets/0/answers"),
    "flag-as-number": (changed(first("more_columns", 1)), "/datasets/0/more_columns"),
    "missing-recipients": (
        changed(lambda document: document["datasets"][0].pop("recipients")),
        "/datasets/0/recipients",
    ),
    "too-many-columns": (
        changed(first("columns", [{"name": "c", "score": 1, "answers": 1}] * 101)),
        "/datasets/0/columns",
    ),
    "too-many-datasets": (
        changed(lambda document: document.__setitem__("datasets", document["datasets"] * 101)),
        "/datasets",
    ),
    "more-without-cursor": (
        changed(lambda document: document.__setitem__("more", True)),
        "/next_cursor",
    ),
    "cursor-without-more": (
        changed(lambda document: document.__setitem__("next_cursor", EXPOSURE_CURSOR)),
        "/next_cursor",
    ),
    "cursor-not-a-cursor": (
        changed(
            lambda document: document.update({"more": True, "next_cursor": CANARY}),
        ),
        "/next_cursor",
    ),
    "time-without-zone": (
        changed(lambda document: document.__setitem__("at", "2026-11-01T12:00:00")),
        "/at",
    ),
    "known-date-not-a-date": (
        changed(lambda document: document.__setitem__("known_at", CANARY)),
        "/known_at",
    ),
}


@pytest.mark.parametrize(("document", "where"), HOSTILE.values(), ids=HOSTILE.keys())
def test_a_hostile_response_fails_the_command_without_being_shown(
    monkeypatch: pytest.MonkeyPatch, document: dict[str, Any], where: str
) -> None:
    for output in ("text", "json"):
        result = run(["exposure", "--format", output], api_with(document), monkeypatch)
        assert result.code == Exit.API_ERROR
        assert result.out == ""
        assert CANARY not in result.err
        assert "\x1b" not in result.err
        assert f"Réponse de l'API invalide en {where} :" in result.err


def test_a_body_beyond_its_bound_is_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(exposure, "MAX_RESPONSE_BYTES", 4096)
    padded = {**exposure_result(), "padding": CANARY * 1000}
    result = run(["exposure"], api_with(padded), monkeypatch)
    assert result.code == Exit.API_ERROR
    assert "trop grande" in result.err
    assert CANARY not in result.err


def test_a_score_is_written_at_the_millionth_without_useless_zero() -> None:
    assert exposure.number(4.0) == "4"
    assert exposure.number(0.5) == "0,5"
    assert exposure.number(1.95432) == "1,95432"
    assert exposure.number(0.0000004) == "0"
