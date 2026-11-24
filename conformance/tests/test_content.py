"""Contenu : un canari du prompt, du résultat d'outil ou de la réponse ne ressort nulle part."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Any

import pytest

from answerlineage_conformance.canaries import (
    ROLES,
    Canary,
    Searcher,
    Sighting,
    canaries_document,
    new_canaries,
    parse_canaries,
)
from answerlineage_conformance.content import Evidence
from answerlineage_conformance.report import Report, render_json, render_text
from answerlineage_conformance.sources import InputError
from conformance.tests.captures import (
    ANSWER,
    CANARIES,
    DRAWS,
    PROMPT,
    RECEIVED_AT,
    TOOL_RESULT,
    evidence,
    judged,
    mutated,
    request,
    witness,
)

SEARCHER = Searcher(CANARIES)


def draws(*values: str) -> Callable[[int], str]:
    """Un tirage joué d'avance, à la place de la source aléatoire."""
    remaining = list(values)

    def token(size: int) -> str:
        assert size == 6
        return remaining.pop(0)

    return token


def test_three_canaries_are_drawn_one_per_role() -> None:
    canaries = new_canaries(draws("0f1e2d3c4b5a", "6978695a4b3c", "a1b2c3d4e5f6"))
    assert canaries == CANARIES
    assert [canary.role for canary in canaries] == list(ROLES)
    assert [canary.draw for canary in canaries] == list(DRAWS.values())


def test_a_canary_is_drawn_at_random_and_survives_json_and_urls() -> None:
    first, second = new_canaries(), new_canaries()
    assert {canary.value for canary in first}.isdisjoint(canary.value for canary in second)
    for canary in first:
        assert canary.value.startswith("canari-")
        assert len(canary.draw) == 12
        # Ni JSON ni une URL ne déforment un canari : il se retrouve tel quel.
        assert json.dumps(canary.value) == f'"{canary.value}"'
        assert set(canary.value) <= set("abcdefghijklmnopqrstuvwxyz0123456789-")


def test_two_canaries_never_share_their_draw() -> None:
    canaries = new_canaries(draws("aaaaaaaaaaaa", "aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"))
    assert [canary.draw for canary in canaries] == ["aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"]


def test_the_canaries_file_is_read_back_as_it_was_written() -> None:
    document = canaries_document(CANARIES)
    assert document == {"prompt": PROMPT, "tool_result": TOOL_RESULT, "answer": ANSWER}
    assert parse_canaries(json.loads(json.dumps(document))) == CANARIES


UNUSABLE_CANARIES: dict[str, Any] = {
    "pas un objet": [PROMPT, TOOL_RESULT, ANSWER],
    "un rôle manque": {"prompt": PROMPT, "tool_result": TOOL_RESULT},
    "un rôle en trop": {**canaries_document(CANARIES), "jeton": PROMPT},
    "une valeur qui n'est pas un texte": {**canaries_document(CANARIES), "answer": 3},
    "un canari sans tirage": {**canaries_document(CANARIES), "prompt": "canari-prompt-"},
    "un tirage trop court": {**canaries_document(CANARIES), "prompt": "canari-prompt-0f1e2d"},
    "un canari d'un autre rôle": {**canaries_document(CANARIES), "prompt": ANSWER},
    "deux canaris du même tirage": {
        **canaries_document(CANARIES),
        "answer": f"canari-reponse-{DRAWS['prompt']}",
    },
}


@pytest.mark.parametrize("case", list(UNUSABLE_CANARIES))
def test_canaries_the_kit_did_not_write_are_refused(case: str) -> None:
    with pytest.raises(InputError) as refused:
        parse_canaries(UNUSABLE_CANARIES[case])
    # Le motif ne cite jamais ce que le fichier contient.
    assert SEARCHER.sightings(str(refused.value)) == []


def content(events: list[Any], proof: Evidence) -> list[tuple[Any, ...]]:
    """Les problèmes de contenu : code, lieu, rôle du canari, forme sous laquelle il est vu."""
    return [
        (
            problem.code,
            problem.event if problem.file is None else problem.file,
            problem.pointer if problem.file is None else problem.line,
            problem.canary,
            problem.form,
        )
        for problem in judged(events, evidence=proof).problems
        if problem.group == "content"
    ]


def test_a_request_that_leaks_nothing_is_conformant() -> None:
    proof = evidence(logs="INFO appel relayé\nINFO réponse diffusée\n", metrics="appels_total 1\n")
    judgement = judged(request(), evidence=proof)
    assert judgement.problems == ()
    assert {control.code for control in judgement.controls if control.group == "content"} == {
        "canary_missing_from_witness",
        "canary_in_events",
        "canary_in_logs",
        "canary_in_metrics",
    }


def test_a_file_that_was_not_given_is_not_said_to_have_been_searched() -> None:
    codes = {control.code for control in judged(request(), evidence=evidence()).controls}
    assert "canary_in_events" in codes
    assert codes.isdisjoint({"canary_in_logs", "canary_in_metrics"})


def test_a_judgement_without_canaries_says_that_the_content_was_not_judged() -> None:
    judgement = judged(request())
    assert "content" not in {control.group for control in judgement.controls}
    report = Report(judgement=judgement, received_at=RECEIVED_AT, files=())
    assert "  contenu : non jugé, aucun canari n'a été fourni\n" in render_text(report)
    assert "content" not in {
        control["group"] for control in json.loads(render_json(report))["controls"]
    }


def test_a_prompt_copied_into_a_facet_is_caught() -> None:
    path: list[str | int] = ["run", "facets", "answerlineage_agent", "taskClass"]
    events = mutated(request(), 0, path, f"Quel est le taux du jour ? {PROMPT}")
    assert content(events, evidence()) == [
        ("canary_in_events", 0, "/run/facets/answerlineage_agent/taskClass", "prompt", "exact")
    ]


def encoded(text: str, *, urlsafe: bool = False) -> str:
    raw = text.encode("utf-8")
    return (base64.urlsafe_b64encode(raw) if urlsafe else base64.b64encode(raw)).decode("ascii")


@pytest.mark.parametrize("before", ["", "x", "xy", "résultat : "])
@pytest.mark.parametrize("after", ["", "z", "zz", " fin"])
@pytest.mark.parametrize("urlsafe", [False, True])
def test_a_tool_result_in_base64_in_a_log_is_caught(before: str, after: str, urlsafe: bool) -> None:
    """Quelle que soit sa place dans le texte encodé : l'encodage dépend de l'alignement."""
    line = f"DEBUG résultat={encoded(before + TOOL_RESULT + after, urlsafe=urlsafe)}"
    proof = evidence(logs=f"INFO appel relayé\n{line}\nINFO fin\n")
    assert content(request(), proof) == [
        ("canary_in_logs", "agent.log", 2, "tool_result", "base64")
    ]


@pytest.mark.parametrize("upper", [False, True])
def test_an_answer_in_hexadecimal_in_a_metric_is_caught(upper: bool) -> None:
    digits = f"réponse : {ANSWER}".encode().hex()
    metric = f'reponses_total{{derniere="{digits.upper() if upper else digits}"}} 1'
    proof = evidence(metrics=f"appels_total 1\n# TYPE reponses_total counter\n{metric}\n")
    assert content(request(), proof) == [("canary_in_metrics", "metriques.txt", 3, "answer", "hex")]


def test_a_truncated_canary_is_caught_by_its_draw_alone() -> None:
    """Un nom de métrique refait à partir du prompt : séparateurs changés, début coupé."""
    metric = f"demandes_prompt_{DRAWS['prompt'].upper()}_total 1"
    assert content(request(), evidence(metrics=f"{metric}\n")) == [
        ("canary_in_metrics", "metriques.txt", 1, "prompt", "draw")
    ]


def test_a_canary_whose_case_changed_is_caught() -> None:
    proof = evidence(logs=f"WARN {ANSWER.upper()}\n")
    assert content(request(), proof) == [
        ("canary_in_logs", "agent.log", 1, "answer", "case_insensitive")
    ]


def test_a_canary_used_as_a_key_is_caught_and_never_copied_into_the_pointer() -> None:
    events = mutated(request(), 0, ["run", "facets", "answerlineage_agent", PROMPT], 1)
    events = mutated(events, 2, ["outputs", 0, ANSWER.upper()], {"a": [1, {TOOL_RESULT: 2}]})
    assert content(events, evidence()) == [
        (
            "canary_in_events",
            0,
            "/run/facets/answerlineage_agent/<clé masquée>",
            "prompt",
            "exact",
        ),
        ("canary_in_events", 2, "/outputs/0/<clé masquée>", "answer", "case_insensitive"),
        (
            "canary_in_events",
            2,
            "/outputs/0/<clé masquée>/a/1/<clé masquée>",
            "tool_result",
            "exact",
        ),
    ]


def test_a_canary_in_a_number_is_caught() -> None:
    """Un tirage fait de chiffres seuls peut ressortir dans un nombre."""
    digits = Canary("prompt", "canari-prompt-204060801030")
    proof = Evidence(
        canaries=(digits, *CANARIES[1:]), witness=witness(digits.value, TOOL_RESULT, ANSWER)
    )
    path: list[str | int] = ["run", "facets", "answerlineage_agent", "compteur"]
    events = mutated(request(), 0, path, 9204060801030)
    assert content(events, proof) == [
        ("canary_in_events", 0, "/run/facets/answerlineage_agent/compteur", "prompt", "draw")
    ]


def test_what_is_neither_a_text_nor_a_number_carries_no_canary() -> None:
    path: list[str | int] = ["run", "facets", "answerlineage_agent", "futur"]
    events = mutated(request(), 0, path, [True, None, 1.5, {}])
    assert judged(events, evidence=evidence()).problems == ()


def test_a_log_written_in_utf_16_is_searched_too() -> None:
    log = f"INFO début\r\nDEBUG {TOOL_RESULT}\r\n".encode("utf-16")
    assert content(request(), evidence(logs=log)) == [
        ("canary_in_logs", "agent.log", 2, "tool_result", "exact")
    ]


def test_every_leak_of_a_line_is_reported_once_per_canary_and_form() -> None:
    line = f"{PROMPT} {PROMPT} {encoded(PROMPT)} {ANSWER}"
    assert content(request(), evidence(logs=line)) == [
        ("canary_in_logs", "agent.log", 1, "answer", "exact"),
        ("canary_in_logs", "agent.log", 1, "prompt", "base64"),
        ("canary_in_logs", "agent.log", 1, "prompt", "exact"),
    ]


def test_a_witness_without_a_canary_fails_because_an_empty_run_would_be_green() -> None:
    seen = witness(f"utilisateur : {PROMPT}", f"agent : {ANSWER.upper()}")
    assert content(request(), evidence(seen=seen)) == [
        ("canary_missing_from_witness", "echange.txt", None, "answer", None),
        ("canary_missing_from_witness", "echange.txt", None, "tool_result", None),
    ]


def test_the_searcher_names_the_most_faithful_form_it_sees() -> None:
    assert SEARCHER.sightings(f"a {PROMPT} b") == [Sighting("prompt", "exact")]
    assert SEARCHER.sightings(PROMPT.title()) == [Sighting("prompt", "case_insensitive")]
    assert SEARCHER.sightings(PROMPT[-12:]) == [Sighting("prompt", "draw")]
    assert SEARCHER.sightings(PROMPT[:-1]) == []
    assert SEARCHER.sightings("canari-prompt-, canari-resultat-, canari-reponse-") == []


LEAKS: dict[str, tuple[Callable[[], list[Any]], Callable[[], Evidence]]] = {
    "dans une facette": (
        lambda: mutated(request(), 0, ["run", "facets", "answerlineage_agent", "owner"], PROMPT),
        evidence,
    ),
    "dans une clé": (lambda: mutated(request(), 2, ["outputs", 0, ANSWER], 1), evidence),
    "en base64 dans un journal": (request, lambda: evidence(logs=encoded(TOOL_RESULT))),
    "en hexadécimal dans une métrique": (
        request,
        lambda: evidence(metrics=ANSWER.encode().hex()),
    ),
    "tronqué": (request, lambda: evidence(logs=DRAWS["tool_result"])),
    "absent du témoin": (request, lambda: evidence(seen=witness("rien"))),
}


@pytest.mark.parametrize("case", list(LEAKS))
@pytest.mark.parametrize("render", [render_text, render_json])
def test_the_report_of_a_leak_never_quotes_the_canary(
    case: str, render: Callable[[Report], str]
) -> None:
    make_events, make_evidence = LEAKS[case]
    events, proof = make_events(), make_evidence()
    judgement = judged(events, evidence=proof)
    assert not judgement.conformant
    files = (proof.witness, *proof.logs, *proof.metrics)
    report = render(Report(judgement=judgement, received_at=RECEIVED_AT, files=files))
    assert SEARCHER.sightings(report) == []
