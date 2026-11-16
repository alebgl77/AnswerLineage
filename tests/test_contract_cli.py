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
from answerlineage_api.store.schema import INCIDENT_WINDOW_DAYS
from answerlineage_cli import dead_letters, doctor, impact, incident, names
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


def test_the_incident_lists_and_bounds_of_the_command_are_those_of_the_api(
    contract: dict[str, Any],
) -> None:
    schemas = contract["components"]["schemas"]
    assert enum(schemas["IncidentDeclaration"]["properties"]["severity"]) == list(
        incident.SEVERITIES
    )
    assert enum(schemas["Incident"]["properties"]["severity"]) == list(incident.SEVERITIES)
    assert enum(schemas["Incident"]["properties"]["state"]) == list(incident.INCIDENT_STATES)
    assert enum(schemas["Analysis"]["properties"]["state"]) == list(incident.ANALYSIS_STATES)
    assert enum(schemas["Analysis"]["properties"]["error"]) == list(incident.ANALYSIS_ERRORS)
    assert schemas["DeclaredScope"]["properties"]["datasets"]["maxItems"] == incident.MAX_DATASETS
    assert incident.MAX_WINDOW_DAYS == INCIDENT_WINDOW_DAYS

    route = contract["paths"]["/api/v1/incidents/{incident_id}"]["get"]
    parameters = {parameter["name"]: parameter["schema"] for parameter in route["parameters"]}
    assert parameters["limit"]["maximum"] == incident.MAX_LIMIT
    assert parameters["cursor"]["anyOf"][0]["pattern"] == f"^{impact.CURSOR.pattern}$"
    assert parameters["incident_id"]["pattern"] == f"^{incident.INCIDENT_ID.pattern}$"


def test_the_dead_letter_reasons_and_bounds_of_the_command_are_those_of_the_api(
    contract: dict[str, Any],
) -> None:
    schemas = contract["components"]["schemas"]
    assert enum(schemas["DeadLetter"]["properties"]["reason"]) == list(dead_letters.REASONS)
    assert enum(schemas["ReasonCount"]["properties"]["reason"]) == list(dead_letters.REASONS)
    assert set(dead_letters.REASON_LABELS) == set(dead_letters.REASONS)

    route = contract["paths"]["/api/v1/ingestion/dead-letters"]["get"]
    parameters = {parameter["name"]: parameter["schema"] for parameter in route["parameters"]}
    assert set(parameters) == {"emitter", "reason", "limit", "cursor"}
    assert parameters["limit"]["maximum"] == dead_letters.MAX_LIMIT
    assert parameters["emitter"]["anyOf"][0]["maxLength"] == dead_letters.MAX_EMITTER
    assert enum(parameters["reason"]) == list(dead_letters.REASONS)
    assert parameters["cursor"]["anyOf"][0]["pattern"] == f"^{dead_letters.CURSOR.pattern}$"

    replay = schemas["DeadLetterReplay"]["properties"]
    assert set(replay) == {"emitter", "reason", "event_ids", "limit"}
    assert replay["limit"]["maximum"] == dead_letters.MAX_REPLAY
    assert replay["event_ids"]["maxItems"] == dead_letters.MAX_EVENT_IDS
    assert replay["event_ids"]["anyOf"][0]["items"]["pattern"] == (
        f"^{dead_letters.EVENT_ID.pattern}$"
    )
    assert replay["emitter"]["maxLength"] == dead_letters.MAX_EMITTER
