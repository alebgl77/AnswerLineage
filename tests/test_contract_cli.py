"""Contrat entre la ligne de commande et l'API qu'elle lit.

La commande ne dépend d'aucun service : la forme d'un identifiant, les états
qu'elle sait lire et les bornes des paramètres y sont écrits une seconde fois.
Ce test les tient d'accord avec l'ingestion et avec le contrat publié : un
nom que l'ingestion admet s'affiche, un état que l'API peut rendre se lit, et
un paramètre que la commande accepte, l'API l'accepte aussi.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from answerlineage_api import identifiers as api
from answerlineage_cli import doctor, impact, names
from answerlineage_cli.names import is_identifier
from tests.test_contract_identifiers import SAMPLES

PUBLISHED = Path(__file__).resolve().parents[1] / "spec" / "openapi" / "answerlineage-v1.json"
BOUNDS = [
    (names.NAMESPACE_MAX, api.NAMESPACE_MAX),
    (names.DATASET_NAME_MAX, api.DATASET_NAME_MAX),
    (names.JOB_NAME_MAX, api.JOB_NAME_MAX),
    (names.TOOL_MAX, api.TOOL_MAX),
    (names.CHANNEL_MAX, api.CHANNEL_MAX),
    (names.RECIPIENT_MAX, api.RECIPIENT_MAX),
    (names.FINGERPRINT_MAX, api.FINGERPRINT_MAX),
]


def admitted_by_the_ingestion(value: str, bound: int) -> bool:
    return api.identifier_fault(value, max_length=bound) is None


@pytest.fixture(scope="module")
def contract() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(PUBLISHED.read_text(encoding="utf-8"))
    return document


def enum(schema: dict[str, Any]) -> list[str]:
    """Les valeurs d'une énumération, nulle admise ou non."""
    if "enum" in schema:
        return list(schema["enum"])
    (values,) = [option["enum"] for option in schema["anyOf"] if "enum" in option]
    return list(values)


def test_the_bounds_and_the_signs_are_those_of_the_ingestion() -> None:
    assert all(mine == theirs for mine, theirs in BOUNDS)
    assert names.PUNCTUATION == api.PUNCTUATION
    assert names.NORMAL_FORM == api.NORMAL_FORM


@pytest.mark.parametrize("bounds", BOUNDS)
@pytest.mark.parametrize("value", SAMPLES)
def test_the_command_shows_a_name_only_when_the_ingestion_admits_it(
    value: str, bounds: tuple[int, int]
) -> None:
    assert is_identifier(value, max_length=bounds[0]) == admitted_by_the_ingestion(value, bounds[1])


@given(st.text(max_size=40))
def test_any_text_is_judged_alike(value: str) -> None:
    assert is_identifier(value, max_length=32) == admitted_by_the_ingestion(value, 32)


def test_the_states_the_command_reads_are_those_the_api_publishes(
    contract: dict[str, Any],
) -> None:
    schemas = contract["components"]["schemas"]
    assert enum(schemas["Coverage"]["properties"]["status"]) == list(impact.COVERAGE_STATUSES)
    assert enum(schemas["CoverageHole"]["properties"]["kind"]) == list(impact.HOLE_KINDS)
    assert enum(schemas["Health"]["properties"]["database"]) == list(doctor.DATABASE_STATES)
    assert enum(schemas["Health"]["properties"]["migrations"]) == list(doctor.MIGRATION_STATES)
    assert contract["info"]["version"].split(".")[0] == str(doctor.API_MAJOR)


def test_the_parameters_the_command_accepts_are_those_of_the_route(
    contract: dict[str, Any],
) -> None:
    route = contract["paths"]["/api/v1/impact"]["get"]
    parameters = {parameter["name"]: parameter["schema"] for parameter in route["parameters"]}
    assert set(parameters) == set(impact.OPTIONS_BY_PARAMETER)
    assert parameters["limit"]["maximum"] == impact.MAX_LIMIT
    for name in ("namespace", "name"):
        assert parameters[name]["maxLength"] == impact.MAX_DATASET_PARAMETER
    assert parameters["cursor"]["anyOf"][0]["pattern"] == f"^{impact.CURSOR.pattern}$"
