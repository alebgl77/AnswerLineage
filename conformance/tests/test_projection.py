"""Projection : ce que l'émetteur déclare et que le graphe ignorerait."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from answerlineage_workers.projection import project
from conformance.tests.captures import found, judged, moved, mutated, reasons, request

PARENT = "/run/facets/parent"
DELIVERY = "/outputs/0/outputFacets/answerlineage_delivery"
FIELDS = "/inputs/0/facets/schema/fields"
PARENT_ID: list[str | int] = ["run", "facets", "parent", "run", "runId"]
DELIVERY_PATH: list[str | int] = ["outputs", 0, "outputFacets", "answerlineage_delivery"]
FIELDS_PATH: list[str | int] = ["inputs", 0, "facets", "schema", "fields"]


def test_a_declared_parent_the_graph_would_ignore_is_reported() -> None:
    events = mutated(request(), 1, PARENT_ID, "run-de-l-agent")
    assert found(events, "projection") == [("parent_dropped", 1, PARENT)]
    # Un point de capture ne doit aucun parent, mais celui qu'il déclare doit servir.
    assert found(events, "projection", profile="capture") == [("parent_dropped", 1, PARENT)]


DROPPED_DELIVERIES: dict[str, tuple[list[str | int], Any, str]] = {
    "empreinte absente : la sortie compterait comme une écriture": (
        ["fingerprint"],
        ...,
        DELIVERY,
    ),
    "empreinte qui n'est pas un texte": (["fingerprint"], 12, DELIVERY),
    "canal absent : la réponse resterait sans diffusion": (["channel"], ..., DELIVERY),
    "référence du message absente": (["messageRef"], ..., DELIVERY),
    "destinataire qui n'est pas un texte": (
        ["recipients"],
        ["8f3a" * 8, 3],
        f"{DELIVERY}/recipients",
    ),
    "destinataires qui ne sont pas une liste": (
        ["recipients"],
        {"a": "8f3a"},
        f"{DELIVERY}/recipients",
    ),
}


@pytest.mark.parametrize("case", list(DROPPED_DELIVERIES))
def test_a_declared_delivery_the_graph_would_ignore_is_reported(case: str) -> None:
    path, value, pointer = DROPPED_DELIVERIES[case]
    events = mutated(request(), 2, [*DELIVERY_PATH, *path], value)
    assert found(events, "projection") == [("delivery_dropped", 2, pointer)]


def test_a_recipient_declared_twice_is_one_recipient() -> None:
    twice = ["8f3a" * 8, "8f3a" * 8, "équipe", "équipe"]
    events = mutated(request(), 2, [*DELIVERY_PATH, "recipients"], twice)
    assert found(events, "projection") == []


def test_a_delivery_among_the_dataset_facets_is_misplaced_but_projected() -> None:
    events = moved(request(), 2, DELIVERY_PATH, ["outputs", 0, "facets", "answerlineage_delivery"])
    assert found(events, "projection") == []
    assert [code for code, _, _ in found(events, "shape")] == ["facet_misplaced"]


def test_an_output_without_delivery_is_an_ordinary_write() -> None:
    events = mutated(request(), 1, ["outputs"], [{"namespace": "memoire", "name": "notes"}])
    assert judged(events).problems == ()


DROPPED_COLUMNS: dict[str, Any] = {
    "colonne sans nom": [{"name": "devise"}, {"nom": "taux"}],
    "nom qui n'est pas un texte": [{"name": "devise"}, {"name": 3}],
    "colonne qui n'est pas un objet": ["devise", "taux"],
    "colonnes qui ne sont pas une liste": "devise, taux",
}


@pytest.mark.parametrize("case", list(DROPPED_COLUMNS))
def test_declared_columns_the_graph_would_ignore_are_reported(case: str) -> None:
    events = mutated(request(), 1, FIELDS_PATH, DROPPED_COLUMNS[case])
    assert found(events, "projection") == [("columns_dropped", 1, FIELDS)]


def test_a_column_declared_twice_is_one_column() -> None:
    twice = [{"name": "devise"}, {"name": "devise"}, {"name": "coût"}, {"name": "coût"}]
    events = mutated(request(), 1, FIELDS_PATH, twice)
    assert found(events, "projection") == []


def test_a_read_without_declared_columns_is_a_read_of_the_whole_table() -> None:
    events = mutated(request(), 1, ["inputs", 0, "facets"], {})
    assert judged(events).problems == ()
    events = mutated(request(), 1, FIELDS_PATH)
    assert judged(events).problems == ()


def test_an_event_the_projection_cannot_read_is_reported_by_the_class_of_its_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    def failing(payload: Mapping[str, Any]) -> Any:
        if payload["eventType"] == "START":
            raise LookupError("le message d'une erreur peut citer l'événement")
        return project(payload)

    monkeypatch.setattr("answerlineage_conformance.projection.project", failing)
    judgement = judged(request())
    assert found(request(), "projection") == [("projection_failed", 0, "")]
    assert reasons(judgement.problems, "projection_failed") == [("", "LookupError")]
    assert all("citer" not in str(problem) for problem in judgement.problems)


def test_an_event_the_ingestion_rejects_is_not_projected() -> None:
    events = mutated(request(), 1, PARENT_ID, "run-de-l-agent")
    events[1]["producer"] = "pas une adresse"
    assert found(events, "projection") == []
