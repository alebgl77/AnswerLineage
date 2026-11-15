"""Pointeurs JSON (RFC 6901) du rapport, sans jamais recopier une clé douteuse.

Un pointeur dit où est un problème dans un événement. Ses segments sont des
rangs et des clés, et une clé est une valeur reçue : elle n'est recopiée que si
elle a la forme d'un nom, celle que l'ingestion exige du nom d'une facette, et
ne porte aucun canari. Sinon elle est remplacée par ``MASKED``, qu'aucune clé
recopiée ne peut valoir : un chevron n'a pas la forme d'un nom.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from answerlineage_api.identifiers import FACET_NAME_MAX, identifier_fault
from answerlineage_conformance.canaries import Searcher

Location = tuple[int | str, ...]

MASKED: Final = "<clé masquée>"


def location_of(pointer: str) -> Location:
    """L'emplacement que désigne un pointeur JSON écrit par l'ingestion."""
    return tuple(
        segment.replace("~1", "/").replace("~0", "~") for segment in pointer.split("/")[1:]
    )


class Masker:
    """Écrit les pointeurs d'un rapport ; ``searcher`` connaît les canaris à ne pas recopier."""

    def __init__(self, searcher: Searcher | None = None) -> None:
        self._searcher = searcher

    def _segment(self, part: int | str) -> str:
        if isinstance(part, int):
            return str(part)
        if identifier_fault(part, max_length=FACET_NAME_MAX) is not None:
            return MASKED
        if self._searcher is not None and self._searcher.sightings(part):
            return MASKED
        return part.replace("~", "~0").replace("/", "~1")

    def pointer(self, location: Sequence[int | str]) -> str:
        return "".join("/" + self._segment(part) for part in location)
