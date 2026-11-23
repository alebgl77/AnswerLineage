"""Ce qu'un événement dit de ses tests de qualité : une lecture fermée, bornée, sans texte libre.

Les événements ont la forme qu'émet dbt-ol 1.55 (``dbt.py``) ; ils sont validés
contre la copie du schéma de la facette standard.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from answerlineage_api.ingest.models import validate_event
from answerlineage_workers import quality
from answerlineage_workers.quality import Failure, Key, assess
from workers.tests.dbt import (
    CUSTOMERS,
    FACET_URL,
    ORDERS,
    WAREHOUSE,
    assertion,
    dbt_test,
    failing,
    model,
    passing,
)

ROOT = Path(__file__).resolve().parents[2]
CANARY = "canari5e3a"
FREE_FIELDS = ("name", "description", "content", "contentType", "expected", "actual")


def key(column: str = "email", name: str = CUSTOMERS) -> Key:
    return Key(WAREHOUSE, name, column)


def validator() -> Draft202012Validator:
    documents = [
        json.loads((ROOT / "spec" / "openlineage" / "2-0-2" / "OpenLineage.json").read_text()),
        json.loads(
            (
                ROOT
                / "spec"
                / "openlineage"
                / "facets"
                / "1-1-0"
                / "DataQualityAssertionsDatasetFacet.json"
            ).read_text()
        ),
    ]
    registry: Registry[Any] = Registry().with_resources(
        (document["$id"], Resource.from_contents(document)) for document in documents
    )
    return Draft202012Validator(
        {"$ref": FACET_URL}, registry=registry, format_checker=FormatChecker()
    )


def every_assertion() -> dict[str, Any]:
    """Une assertion qui porte tous les champs de la facette, chaque texte libre marqué."""
    free: dict[str, Any] = {field: f"{field} {CANARY}" for field in FREE_FIELDS}
    return assertion("not_null", False, "email", params={CANARY: CANARY}, **free)


EVENTS = {
    "échec générique": failing(1, 0),
    "réussite": passing(2, 0),
    "test singulier": dbt_test(3, 0, model([assertion("assert_no_orphans", False)])),
    "tous les champs": dbt_test(4, 0, model([every_assertion()])),
}


@pytest.mark.parametrize("event", EVENTS.values(), ids=list(EVENTS))
def test_the_test_events_follow_the_standard_facet_and_pass_the_ingestion(
    event: dict[str, Any],
) -> None:
    standard = validator()
    for dataset in event["inputs"]:
        for container in ("inputFacets", "facets"):
            facet = dataset[container]["dataQualityAssertions"]
            assert list(standard.iter_errors(facet)) == []
    accepted, errors = validate_event(event)
    assert accepted is not None, errors


def test_a_failing_assertion_is_a_failure_of_its_column() -> None:
    found = assess(failing(1, 0))
    assert found is not None
    assert found.emitter == "dbt"
    assert found.failures == (Failure(key(), ("not_null",), "error"),)
    assert found.passes == ()
    assert not found.anomalies


@pytest.mark.parametrize("containers", [("inputFacets",), ("facets",), ("inputFacets", "facets")])
def test_the_facet_is_read_in_the_input_facets_or_else_in_the_facets(
    containers: tuple[str, ...],
) -> None:
    event = dbt_test(1, 0, model([assertion("unique", False, "id")], containers=containers))
    found = assess(event)
    assert found is not None
    # dbt écrit la facette deux fois : elle n'est lue qu'une.
    assert found.failures == (Failure(key("id"), ("unique",), "error"),)


def test_the_generic_tests_keep_their_class_and_the_others_are_other() -> None:
    assertions = [
        assertion("not_null", False, "email"),
        assertion("unique", False, "email"),
        assertion("ACCEPTED_VALUES", False, "email"),
        assertion("relationships", False, "email"),
        assertion("assert_no_orphans", False, "email"),
        assertion("x" * 100, False, "email"),
        {"assertion": None, "success": False, "column": "email"},
    ]
    found = assess(dbt_test(1, 0, model(assertions)))
    assert found is not None
    (failure,) = found.failures
    assert failure.classes == ("accepted_values", "not_null", "other", "relationships", "unique")


def test_a_singular_test_without_column_is_a_failure_of_the_whole_table() -> None:
    found = assess(EVENTS["test singulier"])
    assert found is not None
    assert found.failures == (Failure(key(""), ("other",), "error"),)
    assert found.invalid_columns == 0


@pytest.mark.parametrize(
    ("severities", "strongest"),
    [
        (("warn",), "warn"),
        (("warn", "error"), "error"),
        (("ERROR",), "error"),
        (("bloquant", "warn"), "warn"),
        (("",), "unspecified"),
    ],
)
def test_the_strongest_severity_of_a_column_is_kept_and_warn_is_a_failure(
    severities: tuple[str, ...], strongest: str
) -> None:
    assertions = [assertion("not_null", False, "email", severity) for severity in severities]
    found = assess(dbt_test(1, 0, model(assertions)))
    assert found is not None
    assert [failure.severity for failure in found.failures] == [strongest]


@pytest.mark.parametrize(
    "column", ["lower(email)", "e mail", "<b>email</b>", "x" * 301, 3, ["email"]]
)
def test_a_column_that_is_not_a_name_makes_the_failure_one_of_the_whole_table(
    column: object,
) -> None:
    found = assess(dbt_test(1, 0, model([assertion("not_null", False, column)])))  # type: ignore[arg-type]
    assert found is not None
    assert found.failures == (Failure(key(""), ("not_null",), "error"),)
    assert found.invalid_columns == 1


def test_a_column_is_kept_under_its_normalised_form() -> None:
    found = assess(dbt_test(1, 0, model([assertion("unique", False, "région")])))
    assert found is not None
    assert found.failures[0].key.column == "région"


@pytest.mark.parametrize("success", ["false", 0, None, 1])
def test_an_assertion_whose_success_is_not_a_boolean_is_ignored(success: object) -> None:
    event = dbt_test(1, 0, model([{"assertion": "not_null", "success": success}]))
    found = assess(event)
    assert found is not None and found.empty


def test_passes_and_failures_are_told_apart_by_column() -> None:
    assertions = [
        assertion("not_null", True, "email"),
        assertion("unique", False, "email"),
        assertion("not_null", True, "id"),
    ]
    found = assess(dbt_test(1, 0, model(assertions)))
    assert found is not None
    assert found.failures == (Failure(key("email"), ("unique",), "error"),)
    assert found.passes == (key("id"),)


def test_each_tested_dataset_has_its_own_failures() -> None:
    event = dbt_test(
        1,
        0,
        model([assertion("not_null", False, "email")]),
        model([assertion("not_null", False, "montant")], name=ORDERS),
    )
    found = assess(event)
    assert found is not None
    assert [failure.key for failure in found.failures] == [key("email"), key("montant", ORDERS)]


def test_beyond_its_bound_a_dataset_is_read_no_further_and_the_rest_is_counted() -> None:
    assertions = [assertion("not_null", True, f"c{rank}") for rank in range(1_000)]
    assertions.append(assertion("not_null", False, "email"))
    found = assess(dbt_test(1, 0, model(assertions)))
    assert found is not None
    assert found.failures == ()
    assert len(found.passes) == 1_000
    assert found.assertions_truncated == 1


def test_an_event_reads_at_most_its_bound_of_assertions() -> None:
    inputs = [
        model([assertion("not_null", False, "email")] * 1_000, name=f"analytique.public.t{rank}")
        for rank in range(11)
    ]
    found = assess(dbt_test(1, 0, *inputs))
    assert found is not None
    assert len(found.failures) == 10
    assert found.assertions_truncated == 1_000


def test_beyond_its_bound_an_event_keeps_its_failures_first_and_counts_the_rest() -> None:
    inputs = [
        model(
            [assertion("not_null", rank % 2 == 0, f"c{column}") for column in range(500)],
            name=f"analytique.public.t{rank}",
        )
        for rank in range(4)
    ]
    found = assess(dbt_test(1, 0, *inputs))
    assert found is not None
    assert len(found.failures) == 1_000
    assert found.passes == ()
    assert found.dropped == 1_000


@pytest.mark.parametrize(
    "dataset",
    [
        {"namespace": WAREHOUSE, "name": CUSTOMERS, "inputFacets": {"dataQualityAssertions": []}},
        {"namespace": WAREHOUSE, "name": CUSTOMERS, "facets": {"dataQualityAssertions": {}}},
        {
            "namespace": WAREHOUSE,
            "name": CUSTOMERS,
            "inputFacets": {"dataQualityAssertions": {"assertions": "tout va bien"}},
        },
        {"namespace": WAREHOUSE, "name": CUSTOMERS, "inputFacets": "rien"},
        {"namespace": "<b>", "name": CUSTOMERS, "inputFacets": {"dataQualityAssertions": {}}},
        "pas un jeu",
    ],
)
def test_a_malformed_facet_is_ignored(dataset: object) -> None:
    assert assess(dbt_test(1, 0, dataset)) is None  # type: ignore[arg-type]


def test_an_assertion_that_is_not_an_object_is_ignored() -> None:
    found = assess(dbt_test(1, 0, model(["not_null", 3, None])))
    assert found is not None and found.empty


@pytest.mark.parametrize(
    "event",
    [
        {"inputs": []},
        {"inputs": "rien"},
        {
            "run": {"runId": "pas un uuid"},
            "job": {"namespace": "dbt"},
            "eventTime": "2026-11-20T06:00:00Z",
            "inputs": [model([assertion("not_null", False)])],
        },
        {
            "run": {"runId": "019a0000-0000-7000-8000-000000000001"},
            "job": {"namespace": "<b>dbt</b>"},
            "eventTime": "2026-11-20T06:00:00Z",
            "inputs": [model([assertion("not_null", False)])],
        },
    ],
)
def test_an_event_without_tests_or_whose_run_cannot_be_read_says_nothing(
    event: dict[str, Any],
) -> None:
    assert assess(event) is None


def test_no_free_text_of_an_assertion_is_ever_read() -> None:
    found = assess(EVENTS["tous les champs"])
    assert found is not None
    assert found.failures == (Failure(key(), ("not_null",), "error"),)
    assert CANARY not in repr(found)


def test_the_closed_lists_are_those_of_the_schema() -> None:
    assert quality.assertion_class("freshness") == "freshness"
    assert quality.assertion_class("row_count") == "row_count"
    assert quality.assertion_class("custom_sql") == "custom_sql"
    assert quality.assertion_class("other") == "other"
    assert quality.assertion_severity("WARN") == "warn"
    assert quality.stronger("warn", "unspecified") == "warn"
    assert quality.stronger("unspecified", "error") == "error"
