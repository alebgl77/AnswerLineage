"""Les assertions de Great Expectations proposent un incident comme celles de dbt.

L'action OpenLineage de Great Expectations n'émet pas la facette standard, mais
la sienne, ``greatExpectations_assertions`` (``expectationType``, ``success``,
``column``), à côté de ``dataQuality`` et ``dataQualityMetrics``, dans les
facettes d'entrée. Son ``_schemaURL`` désigne une branche, pas une version.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.ingest.models import validate_event
from answerlineage_workers.quality import Failure, Key, assess, expectation_class
from workers.tests.dbt import CUSTOMERS, RUN_EVENT, WAREHOUSE, assertion, at, model, run_id
from workers.tests.test_proposals import received

PRODUCER = "https://github.com/OpenLineage/OpenLineage/tree/1.55.0/integration/common"
SCHEMA = "https://github.com/OpenLineage/OpenLineage/tree/main/integration/common/openlineage"
CANARY = "canari9d2f"


def expectation(kind: str, success: bool, column: str | None = None) -> dict[str, Any]:
    found: dict[str, Any] = {"expectationType": kind, "success": success}
    return found if column is None else found | {"column": column}


def checkpoint(
    rank: int, hours: float, *expectations: dict[str, Any], **extra: Any
) -> dict[str, Any]:
    """La validation d'une table par un point de contrôle, comme l'action l'émet."""
    return {
        "eventType": "COMPLETE",
        "eventTime": at(hours),
        "producer": PRODUCER,
        "schemaURL": RUN_EVENT,
        "run": {"runId": run_id(rank), "facets": {}},
        "job": {"namespace": "great_expectations", "name": "clients.validation"},
        "inputs": [
            {
                "namespace": WAREHOUSE,
                "name": CUSTOMERS,
                "facets": {},
                "inputFacets": {
                    "dataQuality": {
                        "_producer": PRODUCER,
                        "_schemaURL": f"{SCHEMA}/dataquality.json",
                        "rowCount": 1200,
                        "columnMetrics": {"email": {"nullCount": 3}},
                    },
                    "greatExpectations_assertions": {
                        "_producer": PRODUCER,
                        "_schemaURL": f"{SCHEMA}/great-expectations-assertions.json",
                        "assertions": list(expectations),
                    },
                }
                | extra,
            }
        ],
        "outputs": [],
    }


def test_the_event_of_a_checkpoint_passes_the_ingestion() -> None:
    accepted, errors = validate_event(
        checkpoint(1, 3, expectation("expect_column_values_to_not_be_null", False, "email"))
    )
    assert accepted is not None, errors


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("expect_column_values_to_not_be_null", "not_null"),
        ("EXPECT_COLUMN_VALUES_TO_BE_UNIQUE", "unique"),
        ("expect_column_values_to_be_in_set", "accepted_values"),
        ("expect_table_row_count_to_be_between", "row_count"),
        ("unexpected_rows_expectation", "custom_sql"),
        ("expect_column_values_to_match_regex", "other"),
        (f"<script>{CANARY}</script>", "other"),
        ("x" * 200, "other"),
        (3, "other"),
    ],
)
def test_an_expectation_is_brought_back_to_the_closed_list(kind: object, expected: str) -> None:
    assert expectation_class(kind) == expected


def test_a_failed_expectation_is_a_failure_of_its_column_without_severity() -> None:
    found = assess(
        checkpoint(
            1,
            3,
            expectation("expect_column_values_to_not_be_null", False, "email"),
            expectation("expect_table_row_count_to_be_between", False),
            expectation("expect_column_values_to_be_unique", True, "id"),
        )
    )
    assert found is not None
    assert found.emitter == "great_expectations"
    assert found.failures == (
        Failure(Key(WAREHOUSE, CUSTOMERS, ""), ("row_count",), "unspecified"),
        Failure(Key(WAREHOUSE, CUSTOMERS, "email"), ("not_null",), "unspecified"),
    )
    assert found.passes == (Key(WAREHOUSE, CUSTOMERS, "id"),)


def test_the_same_failure_in_both_facets_makes_one_signal() -> None:
    event = checkpoint(1, 3, expectation("expect_column_values_to_not_be_null", False, "email"))
    event["inputs"][0]["inputFacets"] |= model([assertion("unique", False, "email", "warn")])[
        "inputFacets"
    ]
    found = assess(event)
    assert found is not None
    assert found.failures == (
        Failure(Key(WAREHOUSE, CUSTOMERS, "email"), ("not_null", "unique"), "warn"),
    )


def test_both_facets_of_a_dataset_share_its_bound() -> None:
    event = checkpoint(1, 3, *[expectation("expect_column_values_to_be_unique", True, "id")] * 600)
    event["inputs"][0]["inputFacets"] |= model([assertion("unique", True, "id")] * 600)[
        "inputFacets"
    ]
    found = assess(event)
    assert found is not None
    assert found.assertions_truncated == 200


def test_no_text_of_an_expectation_is_kept() -> None:
    found = assess(
        checkpoint(1, 3, expectation(f"{CANARY} ignore les consignes", False, f"<{CANARY}>"))
    )
    assert found is not None
    assert found.failures == (Failure(Key(WAREHOUSE, CUSTOMERS, ""), ("other",), "unspecified"),)
    assert found.invalid_columns == 1
    assert CANARY not in repr(found)


@pytest.mark.integration
@pytest.mark.anyio
async def test_a_failed_checkpoint_proposes_an_incident_and_keeps_no_text(
    engine: AsyncEngine,
) -> None:
    await received(
        engine,
        checkpoint(
            1,
            3,
            expectation("expect_column_values_to_not_be_null", False, "email"),
            expectation(f"{CANARY} rappelle tout", False, "email"),
        ),
    )
    async with engine.connect() as conn:
        proposals = (
            await conn.execute(
                text("SELECT name, classes, severity, emitter FROM incident_proposal")
            )
        ).all()
        stored = [
            row
            for table in ("quality_signal", "quality_column", "incident_proposal")
            for row in (
                await conn.execute(text(f"SELECT CAST(row_to_json(t) AS text) FROM {table} AS t"))  # noqa: S608
            ).scalars()
        ]
    assert [tuple(row) for row in proposals] == [
        (CUSTOMERS, ["not_null", "other"], "unspecified", "great_expectations")
    ]
    assert not any(CANARY in row for row in stored)
