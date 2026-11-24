"""Les schémas embarqués dans le paquet sont ceux que le dépôt publie.

Le kit installé juge sans le dépôt : il porte sa copie des schémas des facettes
propres (``spec/facets/``), du schéma d'OpenLineage qu'ils prolongent
(``spec/openlineage/<version>/OpenLineage.json``) et de ceux de l'extension
A2A (``spec/a2a/``). ``spec/`` reste la seule source : cette copie lui est
identique à l'octet près, fichier pour fichier.
"""

from __future__ import annotations

import hashlib
import json
from importlib import resources
from pathlib import Path

from jsonschema import Draft202012Validator

from answerlineage_conformance.a2a import FAMILY, URI
from answerlineage_conformance.schemas import FACETS, violations

ROOT = Path(__file__).resolve().parents[2]
PUBLISHED = ROOT / "spec"
EMBEDDED = Path(str(resources.files("answerlineage_conformance"))) / "schemas"
# Les facettes propres, toutes versions, le schéma d'OpenLineage de chaque version copiée,
# et les schémas de chaque version de l'extension A2A.
EMBEDDED_FILES = ("facets/*/*.json", "openlineage/*/OpenLineage.json", "a2a/*/*.json")
RAW = "https://raw.githubusercontent.com/alebgl77/AnswerLineage/main/"


def digests(root: Path) -> dict[str, str]:
    """Empreinte de chaque schéma que le kit embarque, par chemin."""
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for pattern in EMBEDDED_FILES
        for path in sorted(root.glob(pattern))
    }


def test_the_embedded_schemas_are_the_published_ones_to_the_byte() -> None:
    published = digests(PUBLISHED)
    assert len(published) > 5
    assert digests(EMBEDDED) == published, (
        "Recopier les schémas de spec/facets et de spec/openlineage dans "
        "conformance/src/answerlineage_conformance/schemas."
    )


def test_nothing_else_is_embedded_than_schemas() -> None:
    embedded = {
        path.relative_to(EMBEDDED).as_posix() for path in EMBEDDED.rglob("*") if path.is_file()
    }
    assert embedded == set(digests(EMBEDDED))


def test_what_the_kit_knows_of_a_facet_comes_from_its_schema() -> None:
    assert {key: spec.places for key, spec in FACETS.items()} == {
        "answerlineage_agent": ("run.facets",),
        "answerlineage_toolCall": ("run.facets",),
        "answerlineage_qos": ("run.facets",),
        "answerlineage_delivery": ("outputs.outputFacets",),
        "answerlineage_keys": ("inputs.inputFacets",),
    }
    for spec in FACETS.values():
        assert spec.latest.endswith(f"/{spec.name}.json#/$defs/{spec.name}")


def test_every_reference_to_openlineage_is_resolved_without_the_network() -> None:
    """La facette de base d'OpenLineage, que chaque version prolonge, est dans le paquet :
    elle exige ``_producer`` et ``_schemaURL``."""
    base = {(("_producer",), "required"), (("_schemaURL",), "required")}
    addresses = [address for spec in FACETS.values() for address in spec.addresses]
    assert len(addresses) >= len(FACETS) == 5
    for address in addresses:
        assert base <= set(violations(address, {}))
        assert violations(address, "une facette est un objet") == [((), "type")]


def test_each_a2a_schema_is_served_at_its_address_and_is_a_valid_schema() -> None:
    """Comme une facette : le ``$id`` d'un schéma de l'extension est l'adresse de son fichier."""
    paths = sorted((PUBLISHED / "a2a").glob("*/*.json"))
    assert paths
    for path in paths:
        document = json.loads(path.read_text("utf-8"))
        assert document["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert document["$id"] == RAW + path.relative_to(ROOT).as_posix()
        assert document["$ref"] == f"#/$defs/{path.stem}"
        Draft202012Validator.check_schema(document)


def test_the_extension_uri_is_the_address_of_the_folder_of_its_version() -> None:
    """L'URI se résout : elle mène au dossier qui publie les schémas de sa version."""
    declaration = json.loads((PUBLISHED / "a2a" / "v1" / "AgentExtension.json").read_text("utf-8"))
    assert declaration["$defs"]["AgentExtension"]["properties"]["uri"]["const"] == URI
    assert URI.startswith(FAMILY)
    assert (PUBLISHED / "a2a" / URI.removeprefix(FAMILY) / "README.md").is_file()
