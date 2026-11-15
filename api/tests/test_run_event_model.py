"""US-0301 : un événement invalide est rejeté avec le chemin du champ fautif."""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from answerlineage_api.ingest.models import json_pointer, validate_event
from answerlineage_api.ingest.store import UnstorableEventError, event_identifier
from api.tests.events import facet, minimal_event, mutated, official_run_event_schema, run_event

OFFICIAL = Draft202012Validator(official_run_event_schema(), format_checker=FormatChecker())

VALID: dict[str, Any] = {
    "appel d'outil complet": run_event(),
    "événement minimal": minimal_event(),
    "sans type d'événement": mutated(["eventType"]),
    "sans entrées ni sorties": mutated(["inputs"], base=mutated(["outputs"])),
    "avec une sortie": mutated(
        ["outputs"],
        [
            {
                "namespace": "answer://slack",
                "name": "C042/1731747600.000200",
                "outputFacets": {"answerlineage_delivery": facet(channel="slack")},
            }
        ],
    ),
    "heure avec décalage": mutated(["eventTime"], "2026-11-02T10:00:00.5+01:00"),
    "champ inconnu à la racine": mutated(["extension"], {"x": 1}),
    "producteur en URN": mutated(["producer"], "urn:answerlineage:proxy"),
    "facette de jeu de données d'entrée": mutated(
        ["inputs", 0, "inputFacets"], {"subset": facet(partitions=["2026-11"])}
    ),
}

INVALID: dict[str, tuple[Any, str]] = {
    "identifiant de run absent": (mutated(["run", "runId"]), "/run/runId"),
    "identifiant de run illisible": (mutated(["run", "runId"], "pas-un-uuid"), "/run/runId"),
    "identifiant de run numérique": (mutated(["run", "runId"], 42), "/run/runId"),
    "run absent": (mutated(["run"]), "/run"),
    "job absent": (mutated(["job"]), "/job"),
    "espace de noms du job absent": (mutated(["job", "namespace"]), "/job/namespace"),
    "nom du job numérique": (mutated(["job", "name"], 7), "/job/name"),
    "heure absente": (mutated(["eventTime"]), "/eventTime"),
    "heure sans fuseau": (mutated(["eventTime"], "2026-11-02T09:00:00"), "/eventTime"),
    "heure en nombre": (mutated(["eventTime"], 1730538000), "/eventTime"),
    "heure en nombre écrit": (mutated(["eventTime"], "1730538000"), "/eventTime"),
    "date seule": (mutated(["eventTime"], "2026-11-02"), "/eventTime"),
    "type d'événement inconnu": (mutated(["eventType"], "DONE"), "/eventType"),
    "producteur absent": (mutated(["producer"]), "/producer"),
    "producteur relatif": (mutated(["producer"], "pas une uri"), "/producer"),
    "schéma absent": (mutated(["schemaURL"]), "/schemaURL"),
    "entrées qui ne sont pas un tableau": (mutated(["inputs"], {"a": 1}), "/inputs"),
    "nom du jeu de données absent": (mutated(["inputs", 0, "name"]), "/inputs/0/name"),
    "espace de noms du jeu de données numérique": (
        mutated(["inputs", 0, "namespace"], 3),
        "/inputs/0/namespace",
    ),
    "facette sans producteur": (
        mutated(["run", "facets", "parent", "_producer"]),
        "/run/facets/parent/_producer",
    ),
    "facette sans schéma": (
        mutated(["inputs", 0, "facets", "schema", "_schemaURL"]),
        "/inputs/0/facets/schema/_schemaURL",
    ),
    "facettes qui ne sont pas un objet": (mutated(["run", "facets"], ["x"]), "/run/facets"),
    "sortie sans nom": (mutated(["outputs"], [{"namespace": "x"}]), "/outputs/0/name"),
}


@pytest.mark.parametrize("name", list(VALID))
def test_valid_event_is_accepted_like_the_official_schema(name: str) -> None:
    raw = VALID[name]
    assert OFFICIAL.is_valid(raw), "le schéma officiel devrait accepter cet événement"
    event, errors = validate_event(raw)
    assert errors == []
    assert event is not None
    assert str(event.run.run_id) == raw["run"]["runId"].lower()
    assert event.event_time.tzinfo is not None


@pytest.mark.parametrize("name", list(INVALID))
def test_invalid_event_is_rejected_like_the_official_schema_with_the_faulty_path(name: str) -> None:
    raw, path = INVALID[name]
    assert not OFFICIAL.is_valid(raw), "le schéma officiel devrait rejeter cet événement"
    event, errors = validate_event(raw)
    assert event is None
    assert path in [error.path for error in errors]
    assert all(error.type and error.message for error in errors)


def test_every_faulty_field_of_an_event_is_reported() -> None:
    raw = mutated(["run", "runId"], "x", base=mutated(["job", "name"], base=mutated(["producer"])))
    _, errors = validate_event(raw)
    assert sorted(error.path for error in errors) == ["/job/name", "/producer", "/run/runId"]


@pytest.mark.parametrize("raw", ["chaîne", 12, None, ["liste"], True])
def test_event_that_is_not_an_object_is_rejected(raw: object) -> None:
    event, errors = validate_event(raw)
    assert event is None
    assert [(error.path, error.type) for error in errors] == [("", "not_an_object")]


def test_rejection_never_echoes_the_received_value() -> None:
    received = "canari-valeur-recue-4d2e"
    for raw in (mutated(["run", "runId"], received), mutated(["producer"], received)):
        _, errors = validate_event(raw)
        assert errors
        assert received not in str([error.model_dump() for error in errors])


def test_event_type_defaults_to_other() -> None:
    event, _ = validate_event(mutated(["eventType"]))
    assert event is not None
    assert event.event_type == "OTHER"


def test_json_pointer_escapes_special_characters() -> None:
    assert json_pointer(()) == ""
    assert json_pointer(("inputs", 0, "facets", "a/b~c")) == "/inputs/0/facets/a~1b~0c"


def test_event_identifier_is_deterministic_and_ignores_key_order() -> None:
    first = run_event()
    reordered = dict(reversed(list(first.items())))
    assert event_identifier(first) == event_identifier(reordered)
    assert len(event_identifier(first)) == 64
    assert event_identifier(first) != event_identifier(run_event(1))


@pytest.mark.parametrize("text", ["nul\x00caché", "\ud800 demi-codet isolé"])
def test_text_that_postgresql_cannot_store_is_refused(text: str) -> None:
    with pytest.raises(UnstorableEventError):
        event_identifier(mutated(["job", "name"], text))
