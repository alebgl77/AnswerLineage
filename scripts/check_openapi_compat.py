"""Compatibilité ascendante de l'API publiée.

La spécification OpenAPI publiée dans ``spec/openapi/`` est un contrat : un
client écrit contre la version 1 doit continuer de fonctionner. Ce contrôle
compare le contrat de la branche à celui de la branche cible et échoue sur un
changement incompatible :

- route, opération ou code de réponse retiré ;
- champ retiré d'une réponse, ou dont le type change ;
- paramètre ou champ de requête devenu obligatoire, ou dont le type change.

Ajouter une route, un champ de réponse ou un paramètre facultatif est compatible.

Usage : ``python -m scripts.check_openapi_compat`` (``BASE_SHA`` désigne la
branche cible ; à défaut, ``origin/main``).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

CONTRACT = "spec/openapi/answerlineage-v1.json"
_METHODS = ("get", "put", "post", "delete", "patch", "head", "options")
_MAX_DEPTH = 30


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _resolve(document: Mapping[str, Any], schema: object) -> Mapping[str, Any]:
    """Suit les ``$ref`` internes jusqu'au schéma qu'ils désignent."""
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


def _schema_changes(
    old_document: Mapping[str, Any],
    new_document: Mapping[str, Any],
    old: object,
    new: object,
    where: str,
    *,
    request: bool,
    depth: int = 0,
) -> Iterator[str]:
    """Écarts incompatibles entre deux schémas, pour une requête ou une réponse."""
    if depth > _MAX_DEPTH:
        return
    old_schema, new_schema = _resolve(old_document, old), _resolve(new_document, new)
    old_types, new_types = _types(old_schema), _types(new_schema)
    if old_types and new_types:
        # Une réponse ne doit pas rendre un type nouveau ; une requête ne doit plus
        # refuser un type qu'elle acceptait.
        lost = (old_types - new_types) if request else (new_types - old_types)
        if lost:
            before, after = ", ".join(sorted(old_types)), ", ".join(sorted(new_types))
            yield f"{where} : le type change ({before} → {after})"
            return
    old_properties = _mapping(old_schema.get("properties"))
    new_properties = _mapping(new_schema.get("properties"))
    old_required = set(old_schema.get("required") or [])
    new_required = set(new_schema.get("required") or [])
    if request:
        for name in sorted(new_required - old_required):
            yield f"{where}/{name} : le champ devient obligatoire"
    else:
        for name in sorted(set(old_properties) - set(new_properties)):
            yield f"{where}/{name} : le champ est retiré de la réponse"
        for name in sorted((old_required - new_required) & set(new_properties)):
            yield f"{where}/{name} : le champ n'est plus garanti dans la réponse"
    for name in sorted(set(old_properties) & set(new_properties)):
        yield from _schema_changes(
            old_document,
            new_document,
            old_properties[name],
            new_properties[name],
            f"{where}/{name}",
            request=request,
            depth=depth + 1,
        )
    if "items" in old_schema and "items" in new_schema:
        yield from _schema_changes(
            old_document,
            new_document,
            old_schema["items"],
            new_schema["items"],
            f"{where}[]",
            request=request,
            depth=depth + 1,
        )


def _json_schema(content: object) -> object:
    return _mapping(_mapping(content).get("application/json")).get("schema")


def _operation_changes(
    old_document: Mapping[str, Any],
    new_document: Mapping[str, Any],
    old: Mapping[str, Any],
    new: Mapping[str, Any],
    where: str,
) -> Iterator[str]:
    def parameters(
        document: Mapping[str, Any], operation: Mapping[str, Any]
    ) -> dict[tuple[str, str], Mapping[str, Any]]:
        resolved = (_resolve(document, item) for item in operation.get("parameters") or [])
        return {(str(item.get("name")), str(item.get("in"))): item for item in resolved}

    old_parameters, new_parameters = parameters(old_document, old), parameters(new_document, new)
    for key, parameter in sorted(new_parameters.items()):
        name = f"{where} paramètre {key[0]}"
        previous = old_parameters.get(key)
        if parameter.get("required") and (previous is None or not previous.get("required")):
            yield f"{name} : le paramètre devient obligatoire"
        if previous is not None:
            yield from _schema_changes(
                old_document,
                new_document,
                previous.get("schema"),
                parameter.get("schema"),
                name,
                request=True,
            )

    old_body, new_body = _mapping(old.get("requestBody")), _mapping(new.get("requestBody"))
    if new_body.get("required") and not old_body.get("required"):
        yield f"{where} : le corps de la requête devient obligatoire"
    yield from _schema_changes(
        old_document,
        new_document,
        _json_schema(old_body.get("content")),
        _json_schema(new_body.get("content")),
        f"{where} requête",
        request=True,
    )

    old_responses, new_responses = _mapping(old.get("responses")), _mapping(new.get("responses"))
    for status in sorted(old_responses):
        if status not in new_responses:
            yield f"{where} : la réponse {status} est retirée"
            continue
        yield from _schema_changes(
            old_document,
            new_document,
            _json_schema(_mapping(old_responses[status]).get("content")),
            _json_schema(_mapping(new_responses[status]).get("content")),
            f"{where} réponse {status}",
            request=False,
        )


def breaking_changes(published: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    """Changements du contrat qui casseraient un client de la version publiée."""
    changes: list[str] = []
    old_paths, new_paths = _mapping(published.get("paths")), _mapping(current.get("paths"))
    for path in sorted(old_paths):
        if path not in new_paths:
            changes.append(f"{path} : la route est retirée")
            continue
        old_item, new_item = _mapping(old_paths[path]), _mapping(new_paths[path])
        for method in _METHODS:
            if method not in old_item:
                continue
            where = f"{method.upper()} {path}"
            if method not in new_item:
                changes.append(f"{where} : l'opération est retirée")
                continue
            changes.extend(
                _operation_changes(
                    published,
                    current,
                    _mapping(old_item[method]),
                    _mapping(new_item[method]),
                    where,
                )
            )
    return changes


def published_on(reference: str, path: str = CONTRACT) -> dict[str, Any] | None:
    """Contrat publié sur une référence Git, ou ``None`` s'il n'y existe pas."""
    result = subprocess.run(  # noqa: S603 — commande fixe, référence validée par l'appelant.
        ["git", "show", f"{reference}:{path}"],  # noqa: S607
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    loaded = json.loads(result.stdout)
    return loaded if isinstance(loaded, dict) else None


def main(environment: Mapping[str, str] | None = None, contract: Path = Path(CONTRACT)) -> int:
    env = os.environ if environment is None else environment
    reference = env.get("BASE_SHA") or "origin/main"
    if not re.fullmatch(r"[0-9a-f]{40}|[\w./-]{1,200}", reference) or reference.startswith("-"):
        print("Référence de la branche cible illisible.")
        return 2
    published = published_on(reference, contract.as_posix())
    if published is None:
        print(f"Aucun contrat publié sur {reference} : rien à comparer.")
        return 0
    changes = breaking_changes(published, json.loads(contract.read_text(encoding="utf-8")))
    if changes:
        print("Changements incompatibles avec l'API publiée :")
        for change in changes:
            print(f"  {change}")
        print("Une rupture de contrat demande une nouvelle version de l'API.")
        return 1
    print(f"API compatible avec le contrat publié sur {reference}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
