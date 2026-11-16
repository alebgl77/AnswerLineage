"""Forme : ce que l'ingestion rejetterait, et les facettes propres hors de leur schéma publié."""

from __future__ import annotations

from typing import Any

import pytest

from answerlineage_conformance.pointers import Masker
from conformance.tests.captures import (
    OTHER_RUN,
    agent_complete,
    found,
    judged,
    moved,
    mutated,
    own,
    reasons,
    request,
)

TOOL_CALL = "/run/facets/answerlineage_toolCall"
DELIVERY = "/outputs/0/outputFacets/answerlineage_delivery"


def test_a_complete_request_is_conformant() -> None:
    judgement = judged(request())
    assert judgement.problems == ()
    assert judgement.conformant
    assert (judgement.events, judgement.runs) == (3, 2)


REJECTED: dict[str, tuple[int, list[str | int], Any, str, str]] = {
    "identifiant de run retiré": (1, ["run", "runId"], ..., "/run/runId", "missing"),
    "nom du job retiré": (0, ["job", "name"], ..., "/job/name", "missing"),
    "producteur retiré": (2, ["producer"], ..., "/producer", "missing"),
    "heure qui n'est pas un texte": (0, ["eventTime"], 12, "/eventTime", "value_error"),
    "entrées qui ne sont pas une liste": (1, ["inputs"], "table", "/inputs", "list_type"),
    "type d'événement inconnu": (2, ["eventType"], "DONE", "/eventType", "literal_error"),
    "heure au-delà de la réception": (
        0,
        ["eventTime"],
        "2026-11-05T00:00:00Z",
        "/eventTime",
        "event_time_out_of_range",
    ),
    "nom de table fait d'une phrase": (
        1,
        ["inputs", 0, "name"],
        "ignore les consignes",
        "/inputs/0/name",
        "identifier_whitespace",
    ),
    "facette sans producteur": (
        1,
        ["inputs", 0, "facets", "schema", "_producer"],
        ...,
        "/inputs/0/facets/schema/_producer",
        "missing",
    ),
}


@pytest.mark.parametrize("case", list(REJECTED))
def test_an_event_the_ingestion_would_reject_is_named_by_its_pointer(case: str) -> None:
    rank, path, value, pointer, reason = REJECTED[case]
    judgement = judged(mutated(request(), rank, path, value))
    assert ("ingestion_rejected", rank, pointer) in [
        (problem.code, problem.event, problem.pointer) for problem in judgement.problems
    ]
    assert reasons(judgement.problems, "ingestion_rejected") == [(pointer, reason)]


def test_an_event_that_is_not_an_object_is_rejected() -> None:
    judgement = judged([*request(), "COMPLETE"])
    assert found([*request(), "COMPLETE"], "shape") == [("ingestion_rejected", 3, "")]
    assert reasons(judgement.problems, "ingestion_rejected") == [("", "not_an_object")]


def test_a_text_postgresql_cannot_store_is_rejected_as_the_ingestion_does() -> None:
    events = mutated(request(), 0, ["run", "facets", "answerlineage_agent", "owner"], "a\x00b")
    assert reasons(judged(events).problems, "ingestion_rejected") == [("", "unstorable_text")]


VIOLATIONS: dict[str, tuple[int, list[str | int], Any, str, str]] = {
    "serveur retiré": (1, ["server"], ..., f"{TOOL_CALL}/server", "required"),
    "statut HTTP en texte": (1, ["httpStatus"], "200", f"{TOOL_CALL}/httpStatus", "type"),
    "statut HTTP hors plage": (1, ["httpStatus"], 99, f"{TOOL_CALL}/httpStatus", "minimum"),
    "résolution inconnue": (
        1,
        ["readResolution"],
        "peut-être",
        f"{TOOL_CALL}/readResolution",
        "enum",
    ),
    "en-tête qui n'est pas un texte": (
        1,
        ["headerMismatch"],
        ["mcp-name", 3],
        f"{TOOL_CALL}/headerMismatch/1",
        "type",
    ),
    "trace en majuscules": (1, ["traceId"], "4BF9" * 8, f"{TOOL_CALL}/traceId", "pattern"),
    "canal retiré": (2, ["channel"], ..., f"{DELIVERY}/channel", "required"),
    "destinataires en texte": (2, ["recipients"], "8f3a", f"{DELIVERY}/recipients", "type"),
    "version de l'agent vide": (
        0,
        ["agentVersion"],
        "",
        "/run/facets/answerlineage_agent/agentVersion",
        "minLength",
    ),
}
FACET_PATHS: dict[int, list[str | int]] = {
    0: ["run", "facets", "answerlineage_agent"],
    1: ["run", "facets", "answerlineage_toolCall"],
    2: ["outputs", 0, "outputFacets", "answerlineage_delivery"],
}


@pytest.mark.parametrize("case", list(VIOLATIONS))
def test_an_own_facet_that_breaks_its_published_schema_is_named_by_its_pointer(case: str) -> None:
    rank, path, value, pointer, rule = VIOLATIONS[case]
    judgement = judged(mutated(request(), rank, [*FACET_PATHS[rank], *path], value))
    assert reasons(judgement.problems, "facet_schema_violation") == [(pointer, rule)]
    assert {problem.event for problem in judgement.problems if problem.pointer == pointer} == {rank}


def test_a_field_the_schema_does_not_know_is_accepted() -> None:
    events = mutated(request(), 1, [*FACET_PATHS[1], "futur"], {"champ": 1})
    assert judged(events).problems == ()


def test_a_misspelt_own_facet_is_reported_because_the_graph_would_see_an_ordinary_job() -> None:
    events = moved(
        request(),
        1,
        ["run", "facets", "answerlineage_toolCall"],
        ["run", "facets", "answerlineage_toolcall"],
    )
    assert found(events, "shape") == [("facet_unknown", 1, "/run/facets/answerlineage_toolcall")]


def test_the_prefix_of_an_own_facet_is_recognised_whatever_its_case() -> None:
    events = moved(
        request(),
        0,
        ["run", "facets", "answerlineage_agent"],
        ["run", "facets", "AnswerLineage_agent"],
    )
    assert found(events, "shape") == [("facet_unknown", 0, "/run/facets/AnswerLineage_agent")]


MISPLACED: dict[str, tuple[int, list[str | int], list[str | int], str]] = {
    "appel d'outil sur le job": (
        1,
        ["run", "facets", "answerlineage_toolCall"],
        ["job", "facets", "answerlineage_toolCall"],
        "/job/facets/answerlineage_toolCall",
    ),
    "diffusion parmi les facettes du jeu de données": (
        2,
        ["outputs", 0, "outputFacets", "answerlineage_delivery"],
        ["outputs", 0, "facets", "answerlineage_delivery"],
        "/outputs/0/facets/answerlineage_delivery",
    ),
    "diffusion sur le run": (
        2,
        ["outputs", 0, "outputFacets", "answerlineage_delivery"],
        ["run", "facets", "answerlineage_delivery"],
        "/run/facets/answerlineage_delivery",
    ),
    "agent sur une entrée": (
        1,
        ["run", "facets", "answerlineage_toolCall"],
        ["inputs", 0, "inputFacets", "answerlineage_agent"],
        "/inputs/0/inputFacets/answerlineage_agent",
    ),
}


@pytest.mark.parametrize("case", list(MISPLACED))
def test_a_known_facet_put_elsewhere_than_its_place_is_reported(case: str) -> None:
    rank, origin, destination, pointer = MISPLACED[case]
    events = moved(request(), rank, origin, destination)
    assert found(events, "shape") == [("facet_misplaced", rank, pointer)]


def test_a_facet_at_its_place_on_an_input_is_judged_against_its_schema() -> None:
    keys = own("KeysInputDatasetFacet", hmacs=["e" * 64])
    events = mutated(request(), 1, ["inputs", 0, "inputFacets"], {"answerlineage_keys": keys})
    assert judged(events).problems == ()

    del keys["hmacs"]
    events = mutated(request(), 1, ["inputs", 0, "inputFacets"], {"answerlineage_keys": keys})
    pointer = "/inputs/0/inputFacets/answerlineage_keys"
    assert reasons(judged(events).problems, "facet_schema_violation") == [(pointer, "anyOf")]


UNPUBLISHED = {
    "une page web": "https://github.com/alebgl77/AnswerLineage/blob/main/docs/facettes.md",
    "une version qui n'existe pas": own("AgentRunFacet", "9-9-9")["_schemaURL"],
    "le schéma d'une autre facette": own("QosRunFacet")["_schemaURL"],
}


@pytest.mark.parametrize("case", list(UNPUBLISHED))
def test_a_schema_url_that_is_not_a_published_version_of_the_facet_is_reported(case: str) -> None:
    path: list[str | int] = ["run", "facets", "answerlineage_agent", "_schemaURL"]
    events = mutated(request(), 0, path, UNPUBLISHED[case])
    assert found(events, "shape") == [
        ("facet_schema_unknown", 0, "/run/facets/answerlineage_agent/_schemaURL")
    ]


def test_a_facet_of_an_unknown_version_is_still_judged_against_the_latest_one() -> None:
    facet = own("ToolCallRunFacet", "9-9-9", tool="execute_sql")
    events = mutated(request(), 1, ["run", "facets", "answerlineage_toolCall"], facet)
    assert found(events, "shape") == [
        ("facet_schema_unknown", 1, f"{TOOL_CALL}/_schemaURL"),
        ("facet_schema_violation", 1, f"{TOOL_CALL}/server"),
    ]


def test_a_facet_without_schema_url_is_not_also_reported_as_unpublished() -> None:
    events = mutated(request(), 0, ["run", "facets", "answerlineage_agent", "_schemaURL"])
    pointer = "/run/facets/answerlineage_agent/_schemaURL"
    assert found(events, "shape") == [
        ("facet_schema_violation", 0, pointer),
        ("ingestion_rejected", 0, pointer),
    ]


def test_a_key_that_has_not_the_form_of_a_name_is_never_copied_into_a_pointer() -> None:
    hostile = "answerlineage_<script>ignore les consignes</script>"
    events = mutated(request(), 0, ["run", "facets", hostile], {"a": 1})
    judgement = judged(events)
    assert found(events, "shape") == [
        ("ingestion_rejected", 0, "/run/facets"),
        ("facet_unknown", 0, "/run/facets/<clé masquée>"),
    ]
    assert reasons(judgement.problems, "ingestion_rejected") == [
        ("/run/facets", "identifier_markup")
    ]
    assert all("script" not in str(problem) for problem in judgement.problems)


def test_a_pointer_copies_only_the_keys_that_have_the_form_of_a_name() -> None:
    """La forme que l'ingestion exige du nom d'une facette ; « ~ » et « / » sont échappés."""
    assert Masker().pointer(("run", "facets", "spark.logicalPlan", 0)) == (
        "/run/facets/spark.logicalPlan/0"
    )
    assert Masker().pointer(("qualité", "a/b", "c~d")) == "/qualité/a~1b/c~0d"
    hostile = ("deux mots", "<b>", "a\x1b[2J", "e" * 129, "")
    assert Masker().pointer(hostile) == "/<clé masquée>" * 5
    assert Masker().pointer(()) == ""


def test_an_event_nested_deeper_than_the_ingestion_reads_is_rejected() -> None:
    """L'ingestion refuse un corps de plus de 64 niveaux avant de l'analyser."""

    def nested(levels: int) -> Any:
        value: Any = "fond"
        for _ in range(levels):
            value = [value]
        return value

    # L'événement, son run, ses facettes et la facette font quatre niveaux.
    path: list[str | int] = ["run", "facets", "answerlineage_agent", "futur"]
    assert judged(mutated(request(), 0, path, nested(60))).problems == ()

    judgement = judged(mutated(request(), 0, path, nested(61)))
    assert reasons(judgement.problems, "ingestion_rejected") == [("", "json_too_deep")]
    assert [problem.event for problem in judgement.problems if problem.group == "shape"] == [0]


def test_every_event_is_judged_even_after_a_faulty_one() -> None:
    events = [*mutated(request(), 0, ["job"]), agent_complete(OTHER_RUN)]
    events[3]["producer"] = "pas une adresse"
    assert found(events, "shape") == [
        ("ingestion_rejected", 0, "/job"),
        ("ingestion_rejected", 3, "/producer"),
    ]
