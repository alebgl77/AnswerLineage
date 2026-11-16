"""Schémas de facettes publiés : immuables, et compatibles d'une version à l'autre.

Les cinq facettes propres à AnswerLineage sont décrites dans ``spec/facets/``,
un fichier par facette et par version : ``spec/facets/<version>/<Facette>.json``.
L'adresse d'un fichier, celle du dépôt sur la branche principale, est son ``$id``
et sert de ``_schemaURL`` aux émetteurs. Un émetteur écrit contre une version doit
continuer de fonctionner : ce contrôle échoue dès que

- un fichier déjà publié sur la branche cible a changé ou a disparu : un fichier
  de version ne change plus jamais, une évolution passe par une nouvelle version ;
- une nouvelle version retire un champ, rend un champ obligatoire, ne garantit plus
  un champ obligatoire ou change le type d'un champ par rapport à la version
  précédente de la même facette (une rupture demande une nouvelle facette) ;
- un fichier ne respecte pas le rangement : chemin, ``$id``, définition sous
  ``$defs/<Facette>`` qui prolonge par ``allOf`` la définition de base d'OpenLineage.

Ajouter un champ facultatif est compatible. Le contrôle compare les champs, leur
caractère obligatoire et leur type, pas les bornes ni les valeurs admises.

Les fichiers de l'extension A2A (``spec/a2a/<version>/``) sont figés de même, tous,
schémas et texte : un fichier publié ne change ni ne disparaît. Ils n'ont pas
d'autre règle : leur version est dans l'URI de l'extension, et une évolution est
une nouvelle version, dans un nouveau dossier.

Usage : ``python -m scripts.check_facets_compat`` (``BASE_SHA`` désigne la
branche cible ; à défaut, ``origin/main``).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator, Mapping
from itertools import pairwise
from pathlib import Path
from typing import Any

FACETS = Path("spec/facets")
# Dossiers figés : chaque fichier publié y est immuable, sans autre règle de rangement.
FROZEN = (Path("spec/a2a"),)
BASE_URL = "https://raw.githubusercontent.com/alebgl77/AnswerLineage/main/spec/facets/"
JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
OPENLINEAGE_DEFINITIONS = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/"
# Définitions de base d'OpenLineage qu'une facette prolonge, selon sa portée.
BASE_DEFINITIONS = ("RunFacet", "InputDatasetFacet", "OutputDatasetFacet")

_FILE = re.compile(r"(?:.*/)?(\d+)-(\d+)-(\d+)/([A-Z][A-Za-z0-9]*)\.json")
_MAX_DEPTH = 30

Version = tuple[int, int, int]


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _location(path: str) -> tuple[Version, str] | None:
    """Version et nom de facette d'un chemin ``…/<version>/<Facette>.json``."""
    found = _FILE.fullmatch(path)
    if found is None:
        return None
    return (int(found[1]), int(found[2]), int(found[3])), found[4]


def _version_text(version: Version) -> str:
    return "-".join(str(part) for part in version)


def layout_problems(path: str, schema: object) -> list[str]:
    """Écarts d'un fichier avec le rangement des facettes."""
    location = _location(path)
    if location is None:
        return [f"{path} : le chemin doit être <version>/<NomDeLaFacette>.json (version 1-0-0)"]
    version, name = location
    document = _mapping(schema)
    if not document:
        return [f"{path} : le fichier doit contenir un objet JSON"]
    problems: list[str] = []
    if document.get("$schema") != JSON_SCHEMA_DIALECT:
        problems.append(f"{path} : $schema doit valoir {JSON_SCHEMA_DIALECT} (JSON Schema 2020-12)")
    address = f"{BASE_URL}{_version_text(version)}/{name}.json"
    if document.get("$id") != address:
        problems.append(f"{path} : $id doit être l'adresse du fichier, {address}")
    definition = _mapping(_mapping(document.get("$defs")).get(name))
    if not definition:
        problems.append(f"{path} : la définition $defs/{name} est absente")
        return problems
    declared = _mapping(document.get("properties"))
    references = [_mapping(item).get("$ref") for item in declared.values()]
    if len(declared) != 1 or references != [f"#/$defs/{name}"]:
        problems.append(f"{path} : properties doit nommer la facette et viser #/$defs/{name}")
    bases = [
        base
        for item in definition.get("allOf") or []
        for base in BASE_DEFINITIONS
        if _mapping(item).get("$ref") == OPENLINEAGE_DEFINITIONS + base
    ]
    if not bases:
        problems.append(
            f"{path} : $defs/{name} doit prolonger par allOf une définition de base d'OpenLineage "
            f"({OPENLINEAGE_DEFINITIONS}{'/'.join(BASE_DEFINITIONS)})"
        )
    elif not name.endswith(bases[0]):
        problems.append(
            f"{path} : le nom d'une facette qui prolonge {bases[0]} se termine par {bases[0]}"
        )
    return problems


def _resolve(document: Mapping[str, Any], schema: object) -> Mapping[str, Any]:
    """Suit les ``$ref`` internes ; une référence qui ne mène nulle part donne un schéma vide."""
    current = _mapping(schema)
    for _ in range(_MAX_DEPTH):
        reference = current.get("$ref")
        if not isinstance(reference, str) or not reference.startswith("#/"):
            return current
        target: object = document
        for part in reference[2:].split("/"):
            target = _mapping(target).get(part.replace("~1", "/").replace("~0", "~"))
        current = _mapping(target)
    return current


def _types(schema: Mapping[str, Any]) -> set[str]:
    declared = schema.get("type")
    if isinstance(declared, str):
        return {declared}
    if isinstance(declared, list):
        return {item for item in declared if isinstance(item, str)}
    alternatives = schema.get("anyOf") or schema.get("oneOf")
    if isinstance(alternatives, list):
        return {kind for item in alternatives for kind in _types(_mapping(item))}
    return set()


def _fields(
    document: Mapping[str, Any], schema: object, depth: int = 0
) -> tuple[dict[str, Any], set[str]]:
    """Champs d'un objet et ceux qui sont obligatoires, ``allOf`` aplati."""
    resolved = _resolve(document, schema)
    properties = dict(_mapping(resolved.get("properties")))
    required = {item for item in resolved.get("required") or [] if isinstance(item, str)}
    if depth < _MAX_DEPTH:
        for member in resolved.get("allOf") or []:
            inner_properties, inner_required = _fields(document, member, depth + 1)
            properties.update(inner_properties)
            required |= inner_required
    return properties, required


def _join(where: str, name: str) -> str:
    return f"{where}/{name}" if where else name


def _changes(
    old_document: Mapping[str, Any],
    new_document: Mapping[str, Any],
    old: object,
    new: object,
    where: str,
    depth: int = 0,
) -> Iterator[str]:
    if depth > _MAX_DEPTH:
        return
    old_schema, new_schema = _resolve(old_document, old), _resolve(new_document, new)
    old_types, new_types = _types(old_schema), _types(new_schema)
    if old_types != new_types:
        before = ", ".join(sorted(old_types)) or "non précisé"
        after = ", ".join(sorted(new_types)) or "non précisé"
        yield f"{where} : le type change ({before} → {after})"
        return
    old_properties, old_required = _fields(old_document, old_schema)
    new_properties, new_required = _fields(new_document, new_schema)
    for name in sorted(set(old_properties) - set(new_properties)):
        yield f"{_join(where, name)} : le champ est retiré"
    for name in sorted(new_required - old_required):
        yield f"{_join(where, name)} : le champ devient obligatoire"
    for name in sorted((old_required - new_required) & set(new_properties)):
        yield f"{_join(where, name)} : le champ n'est plus obligatoire, plus rien ne le garantit"
    for name in sorted(set(old_properties) & set(new_properties)):
        yield from _changes(
            old_document,
            new_document,
            old_properties[name],
            new_properties[name],
            _join(where, name),
            depth + 1,
        )
    if "items" in old_schema and "items" in new_schema:
        yield from _changes(
            old_document,
            new_document,
            old_schema["items"],
            new_schema["items"],
            f"{where}[]",
            depth + 1,
        )


def _definition(document: Mapping[str, Any]) -> object:
    """La définition de la facette : celle que vise ``properties`` à la racine du fichier."""
    for item in _mapping(document.get("properties")).values():
        return item
    return {}


def breaking_changes(published: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    """Changements d'une version à la suivante qui casseraient un émetteur de la première."""
    return list(_changes(published, current, _definition(published), _definition(current), ""))


def version_changes(files: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Ruptures entre versions successives de chaque facette, ``files`` étant indexé par chemin."""
    by_name: dict[str, dict[Version, Mapping[str, Any]]] = {}
    for path, schema in files.items():
        location = _location(path)
        if location is not None:
            by_name.setdefault(location[1], {})[location[0]] = schema
    problems: list[str] = []
    for name, versions in sorted(by_name.items()):
        ordered = sorted(versions)
        for previous, following in pairwise(ordered):
            steps = f"{name} {_version_text(previous)} → {_version_text(following)}"
            problems.extend(
                f"{steps} : {change}"
                for change in breaking_changes(versions[previous], versions[following])
            )
    return problems


def local_problems(root: Path = FACETS) -> list[str]:
    """Rangement des fichiers de ``root`` et compatibilité de ses versions entre elles."""
    schemas: dict[str, Mapping[str, Any]] = {}
    problems: list[str] = []
    for file in sorted(root.rglob("*.json")):
        path = file.as_posix()
        try:
            schema = json.loads(file.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            problems.append(f"{path} : le fichier n'est pas du JSON")
            continue
        problems.extend(layout_problems(path, schema))
        if isinstance(schema, dict):
            schemas[path] = schema
    return problems + version_changes(schemas)


def published_on(reference: str, root: Path = FACETS, suffix: str = ".json") -> dict[str, bytes]:
    """Fichiers publiés sous ``root`` sur une référence Git, vides si elle n'en a pas.

    Seuls comptent les fichiers dont le nom finit par ``suffix`` ; tous, s'il est vide.
    """
    listing = subprocess.run(  # noqa: S603 — commande fixe, référence validée par l'appelant.
        ["git", "ls-tree", "-r", "--name-only", reference, "--", root.as_posix()],  # noqa: S607
        capture_output=True,
        check=False,
        text=True,
    )
    published: dict[str, bytes] = {}
    if listing.returncode != 0:
        return published
    for path in sorted(listing.stdout.splitlines()):
        if not path.endswith(suffix):
            continue
        content = subprocess.run(  # noqa: S603 — commande fixe, référence validée par l'appelant.
            ["git", "show", f"{reference}:{path}"],  # noqa: S607
            capture_output=True,
            check=False,
        )
        if content.returncode == 0:
            published[path] = content.stdout
    return published


def published_changes(published: Mapping[str, bytes]) -> list[str]:
    """Fichiers publiés qui ne sont plus, à l'octet près, ce qu'ils étaient."""
    problems: list[str] = []
    for path, content in sorted(published.items()):
        file = Path(path)
        if not file.is_file():
            problems.append(f"{path} : publié et retiré, un fichier de version ne change plus")
        elif file.read_bytes() != content:
            problems.append(f"{path} : publié et modifié, un fichier de version ne change plus")
    return problems


def main(
    environment: Mapping[str, str] | None = None,
    root: Path = FACETS,
    frozen: tuple[Path, ...] = FROZEN,
) -> int:
    env = os.environ if environment is None else environment
    reference = env.get("BASE_SHA") or "origin/main"
    if not re.fullmatch(r"[0-9a-f]{40}|[\w./-]{1,200}", reference) or reference.startswith("-"):
        print("Référence de la branche cible illisible.")
        return 2
    published = published_on(reference, root)
    for folder in frozen:
        published.update(published_on(reference, folder, suffix=""))
    problems = local_problems(root) + published_changes(published)
    if problems:
        print(
            "Schémas de facettes ou de l'extension A2A incompatibles avec ceux qui sont publiés :"
        )
        for problem in problems:
            print(f"  {problem}")
        print(
            "Un fichier de version publié ne change plus jamais : une évolution passe par une "
            "nouvelle version, qui ne retire rien, ne rend rien obligatoire, ne change aucun type."
        )
        return 1
    if not published:
        print(f"Aucun schéma publié sur {reference} : rien à comparer.")
        return 0
    print(f"Schémas compatibles avec ceux qui sont publiés sur {reference}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
