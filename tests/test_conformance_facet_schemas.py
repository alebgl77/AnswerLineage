"""Le kit de conformité et les schémas publiés des facettes disent la même chose.

Le kit juge avec la copie des schémas qu'il embarque. Sur les exemples de
``tests/test_facet_schemas.py``, validés là contre les fichiers de ``spec/``,
il accepte les mêmes facettes et désigne les mêmes champs fautifs.
"""

from __future__ import annotations

from typing import Any

import pytest

from answerlineage_conformance.judgement import judge
from conformance.tests.captures import RECEIVED_AT, request
from tests.test_facet_schemas import EXAMPLES, FAULTY, NAMES, facet, validator

KEYS = {name: key for key, name in NAMES.items()}
# Où poser chaque facette dans une demande : rang de l'événement, puis porte-facettes.
PLACES: dict[str, tuple[int, list[str | int]]] = {
    "AgentRunFacet": (0, ["run", "facets"]),
    "ToolCallRunFacet": (1, ["run", "facets"]),
    "QosRunFacet": (0, ["run", "facets"]),
    "DeliveryOutputDatasetFacet": (2, ["outputs", 0, "outputFacets"]),
    "KeysInputDatasetFacet": (1, ["inputs", 0, "inputFacets"]),
}


def published(name: str, emitted: dict[str, Any]) -> set[str]:
    """Les champs fautifs d'une facette, d'après son schéma publié dans ``spec/``."""
    fields: set[str] = set()
    for error in validator(name).iter_errors(emitted):
        path = "".join(f"/{part}" for part in error.absolute_path)
        if error.validator == "required":
            missing = [field for field in error.validator_value if field not in error.instance]
            fields.update(f"{path}/{field}" for field in missing)
        else:
            fields.add(path)
    return fields


def judged_by_the_kit(name: str, emitted: dict[str, Any]) -> set[str]:
    """Les champs fautifs de la même facette, posée à sa place dans une demande jugée par le kit."""
    rank, container = PLACES[name]
    events = request()
    owner: Any = events[rank]
    for part in container[:-1]:
        owner = owner[part]
    owner.setdefault(container[-1], {})[KEYS[name]] = emitted
    pointer = "".join(f"/{part}" for part in [*container, KEYS[name]])
    violations = [
        problem
        for problem in judge(events, profile="adapter", received_at=RECEIVED_AT).problems
        if problem.code == "facet_schema_violation"
    ]
    assert all(problem.event == rank for problem in violations)
    assert all((problem.pointer or "").startswith(pointer) for problem in violations)
    return {(problem.pointer or "").removeprefix(pointer) for problem in violations}


@pytest.mark.parametrize(
    ("name", "kind"), [(name, kind) for name, kinds in EXAMPLES.items() for kind in kinds]
)
def test_the_kit_accepts_an_example_the_published_schema_accepts(name: str, kind: str) -> None:
    emitted = facet(name, **EXAMPLES[name][kind])
    assert published(name, emitted) == set()
    assert judged_by_the_kit(name, emitted) == set()


@pytest.mark.parametrize(
    ("name", "case"), [(name, case) for name, cases in FAULTY.items() for case in cases]
)
def test_the_kit_names_the_faulty_fields_the_published_schema_names(name: str, case: str) -> None:
    emitted = facet(name, **FAULTY[name][case])
    expected = published(name, emitted)
    assert expected
    assert judged_by_the_kit(name, emitted) == expected


@pytest.mark.parametrize("name", list(PLACES))
@pytest.mark.parametrize("missing", ["_producer", "_schemaURL"])
def test_the_kit_requires_the_base_facet_of_openlineage_as_the_schema_does(
    name: str, missing: str
) -> None:
    emitted = facet(name, **EXAMPLES[name]["minimal"])
    del emitted[missing]
    assert published(name, emitted) == {f"/{missing}"}
    assert judged_by_the_kit(name, emitted) == {f"/{missing}"}


def test_every_published_facet_has_its_place_in_this_comparison() -> None:
    assert set(PLACES) == set(NAMES.values()) == set(EXAMPLES) == set(FAULTY)
