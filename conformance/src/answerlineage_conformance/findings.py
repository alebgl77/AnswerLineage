"""Ce qu'un contrôle relève, et la lecture tolérante d'un événement reçu.

Un événement d'une capture est une entrée hostile : rien n'y a forcément le
type attendu. Les contrôles le lisent sans rien supposer, et laissent à
l'ingestion le soin de dire ce qu'elle rejetterait.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from answerlineage_conformance.pointers import Location

TERMINAL: Final = frozenset({"COMPLETE", "FAIL", "ABORT"})
_UUID: Final = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


@dataclass(frozen=True)
class Finding:
    """Un problème relevé, là où il est : dans un événement, ou dans un fichier."""

    code: str
    # Rang de l'événement dans la capture, à partir de zéro, et emplacement dans l'événement.
    event: int | None = None
    location: Location | None = None
    # Fichier fouillé, et numéro de la ligne, à partir de un.
    file: str | None = None
    line: int | None = None
    # Code d'ingestion, règle du schéma ou classe d'erreur : jamais une valeur reçue.
    reason: str | None = None
    canary: str | None = None
    form: str | None = None


def mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def items(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def canonical_uuid(value: object) -> UUID | None:
    """L'UUID qu'écrit un texte de forme canonique, comme l'ingestion la contrôle."""
    if isinstance(value, str) and _UUID.fullmatch(value):
        return UUID(value)
    return None


def run_facets(event: object) -> Mapping[str, Any]:
    return mapping(mapping(mapping(event).get("run")).get("facets"))


def run_id(event: object) -> UUID | None:
    return canonical_uuid(mapping(mapping(event).get("run")).get("runId"))
