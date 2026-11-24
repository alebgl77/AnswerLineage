"""Schémas publiés des facettes propres, embarqués dans le paquet.

Le dossier ``schemas/`` est la copie, à l'octet près, de ``spec/facets/`` et du
schéma d'OpenLineage que ces facettes prolongent : un test les tient
identiques à ceux du dépôt, et le kit installé juge ainsi sans le dépôt ni le
réseau. Tout ce que le kit sait d'une facette vient de son schéma : sa clé, ses
versions, leur adresse, et sa place dans un événement, qui est celle de la
facette d'OpenLineage qu'il prolonge.

Les messages d'erreur de ``jsonschema`` citent la valeur fautive : il n'en est
gardé que le chemin et la règle violée.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any, Final

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from answerlineage_conformance.pointers import Location

# Préfixe des facettes propres, reconnu sans égard à la casse : une faute de
# frappe dans la suite du nom ne doit pas faire passer la facette pour celle d'un autre.
OWN_PREFIX: Final = "answerlineage"

# Où se pose une facette, selon la facette d'OpenLineage que son schéma prolonge.
_PLACES: Final[Mapping[str, tuple[str, ...]]] = {
    "RunFacet": ("run.facets",),
    "JobFacet": ("job.facets",),
    "DatasetFacet": ("inputs.facets", "outputs.facets"),
    "InputDatasetFacet": ("inputs.inputFacets",),
    "OutputDatasetFacet": ("outputs.outputFacets",),
}


@dataclass(frozen=True)
class FacetSpec:
    """Une facette propre, telle que ses schémas publiés la décrivent."""

    key: str
    name: str
    places: tuple[str, ...]
    # Adresse (``_schemaURL``) de chaque version publiée, de la plus ancienne à la plus récente.
    addresses: tuple[str, ...]

    @property
    def latest(self) -> str:
        return self.addresses[-1]


def _documents(folder: str) -> Iterator[tuple[str, dict[str, Any]]]:
    """Les schémas embarqués d'un dossier : version, puis document."""
    root = resources.files("answerlineage_conformance") / "schemas" / folder
    for version in sorted(root.iterdir(), key=lambda entry: _order(entry.name)):
        for file in sorted(version.iterdir(), key=lambda entry: entry.name):
            yield version.name, json.loads(file.read_text(encoding="utf-8"))


def _order(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("-"))


def _load() -> tuple[dict[str, FacetSpec], Registry[Any]]:
    documents = [document for _, document in _documents("openlineage")]
    facets: dict[str, FacetSpec] = {}
    for _, document in _documents("facets"):
        documents.append(document)
        ((key, target),) = document["properties"].items()
        name = target["$ref"].rsplit("/", 1)[-1]
        base = document["$defs"][name]["allOf"][0]["$ref"].rsplit("/", 1)[-1]
        address = f"{document['$id']}#/$defs/{name}"
        known = facets.get(key)
        earlier = known.addresses if known is not None else ()
        facets[key] = FacetSpec(key, name, _PLACES[base], (*earlier, address))
    registry: Registry[Any] = Registry().with_resources(
        (document["$id"], Resource.from_contents(document, default_specification=DRAFT202012))
        for document in documents
    )
    return facets, registry


FACETS, _REGISTRY = _load()


def is_own(key: str) -> bool:
    """Vrai pour une clé de facette au préfixe d'AnswerLineage, quelle que soit sa casse."""
    return key.casefold().startswith(OWN_PREFIX)


@cache
def _validator(address: str) -> Draft202012Validator:
    # Les références se résolvent dans le registre embarqué : jamais par le réseau.
    return Draft202012Validator({"$ref": address}, registry=_REGISTRY)


def _fields(error: ValidationError) -> Iterator[Location]:
    """Les champs qu'une erreur désigne ; pour un champ obligatoire, celui qui manque."""
    location: Location = tuple(error.absolute_path)
    required = error.validator_value if error.validator == "required" else None
    if isinstance(required, list) and isinstance(error.instance, Mapping):
        for name in required:
            if name not in error.instance:
                yield (*location, name)
    else:
        yield location


def violations(address: str, facet: object) -> list[tuple[Location, str]]:
    """Ce qu'une facette ne respecte pas dans la version du schéma à cette adresse.

    Chaque écart est un emplacement dans la facette et la règle violée, mot-clé
    de JSON Schema : ``required``, ``type``, ``maxLength``, ``pattern``…
    """
    return rule_violations(_validator(address), facet)


def rule_violations(
    validator: Draft202012Validator, instance: object
) -> list[tuple[Location, str]]:
    """Les écarts d'un document avec un schéma : un emplacement et la règle violée, triés."""
    found = {
        (location, str(error.validator))
        for error in validator.iter_errors(instance)
        for location in _fields(error)
    }
    return sorted(found, key=lambda item: ([str(part) for part in item[0]], item[1]))
