"""Canaris : des valeurs inventées, reconnaissables, que l'intégrateur fait traverser son émetteur.

Un canari vaut ``canari-<rôle>-<tirage>`` : douze hexadécimaux tirés au sort,
derrière un rôle en clair. Il n'est fait que de minuscules, de chiffres et de
tirets, pour que ni JSON ni une URL ne le déforment : placé dans un prompt, un
résultat d'outil ou une réponse, il se retrouve tel quel là où il passe.

La recherche ne s'arrête pas à la copie exacte. Un émetteur fautif change la
casse, encode en base64 ou en hexadécimal, ou refait un nom à partir du texte :
le tirage seul, qui survit à un changement de séparateur, suffit à le trahir.
"""

from __future__ import annotations

import base64
import re
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from answerlineage_conformance.sources import InputError

Role = Literal["prompt", "tool_result", "answer"]
Form = Literal["exact", "case_insensitive", "base64", "hex", "draw"]

ROLES: Final[tuple[Role, ...]] = ("prompt", "tool_result", "answer")
# Le rôle tel qu'il s'écrit dans le canari.
MARKERS: Final[dict[Role, str]] = {
    "prompt": "prompt",
    "tool_result": "resultat",
    "answer": "reponse",
}
_DRAW_BYTES: Final = 6
_CANARY: Final = re.compile(r"canari-(?P<marker>[a-z]+)-(?P<draw>[0-9a-f]{12})")
# Alphabet de base64 « sûr pour les URL », ramené à l'alphabet courant.
_URL_SAFE: Final = str.maketrans("-_", "+/")


@dataclass(frozen=True)
class Canary:
    role: Role
    value: str

    @property
    def draw(self) -> str:
        """Le tirage : les douze hexadécimaux qui finissent le canari."""
        return self.value.rsplit("-", 1)[-1]


@dataclass(frozen=True, order=True)
class Sighting:
    """Un canari retrouvé dans un texte, et sous quelle forme."""

    role: Role
    form: Form


def new_canaries(token: Callable[[int], str] = secrets.token_hex) -> tuple[Canary, ...]:
    """Tire trois canaris, un par rôle, de tirages distincts."""
    draws: list[str] = []
    for _ in ROLES:
        draw = token(_DRAW_BYTES)
        while draw in draws:
            draw = token(_DRAW_BYTES)
        draws.append(draw)
    return tuple(
        Canary(role, f"canari-{MARKERS[role]}-{draw}")
        for role, draw in zip(ROLES, draws, strict=True)
    )


def canaries_document(canaries: Sequence[Canary]) -> dict[str, str]:
    """Le contenu du fichier de canaris : un canari par rôle."""
    return {canary.role: canary.value for canary in canaries}


def parse_canaries(document: object) -> tuple[Canary, ...]:
    """Relit un fichier de canaris. Lève ``InputError`` s'il n'a pas la forme écrite par le kit.

    Un canari d'une autre forme rendrait la recherche fausse : trop court, il se
    verrait partout ; sans tirage, sa copie tronquée passerait.
    """
    if not isinstance(document, dict) or set(document) != set(ROLES):
        raise InputError(
            "ce ne sont pas des canaris écrits par le kit : un objet de trois champs "
            "est attendu, « prompt », « tool_result » et « answer »."
        )
    canaries = []
    for role in ROLES:
        value = document[role]
        found = _CANARY.fullmatch(value) if isinstance(value, str) else None
        if found is None or found["marker"] != MARKERS[role]:
            raise InputError(
                "ce ne sont pas des canaris écrits par le kit : chaque canari vaut "
                "« canari-<rôle>-<douze hexadécimaux> », avec le rôle de son champ."
            )
        canaries.append(Canary(role, value))
    if len({canary.draw for canary in canaries}) != len(canaries):
        raise InputError(
            "ce ne sont pas des canaris écrits par le kit : deux canaris ont le même tirage."
        )
    return tuple(canaries)


def _base64_cores(value: bytes) -> tuple[str, ...]:
    """Ce que le base64 d'un texte qui contient ``value`` contient toujours.

    L'encodage groupe les octets par trois : il dépend de la place de la valeur
    dans le texte. Pour chacun des trois alignements, les caractères qui ne
    tiennent qu'à la valeur sont gardés, ceux qui mêlent ses bits à ceux de ses
    voisins sont retirés.
    """
    cores = []
    for offset in range(3):
        encoded = base64.b64encode(b"\x00" * offset + value).decode("ascii").rstrip("=")
        # Devant : 0, 2 ou 3 caractères portent des bits du texte qui précède.
        head = (0, 2, 3)[offset]
        # Derrière : le dernier caractère d'un groupe incomplet porte des bits de la suite.
        tail = 0 if (offset + len(value)) % 3 == 0 else 1
        cores.append(encoded[head : len(encoded) - tail])
    return tuple(cores)


@dataclass(frozen=True)
class _Needles:
    canary: Canary
    hexadecimal: str
    base64: tuple[str, ...]


class Searcher:
    """Cherche les canaris dans un texte, sous toutes les formes où un émetteur les laisse."""

    def __init__(self, canaries: Sequence[Canary]) -> None:
        self.canaries = tuple(canaries)
        self._needles = tuple(
            _Needles(
                canary,
                canary.value.encode("ascii").hex(),
                _base64_cores(canary.value.encode("ascii")),
            )
            for canary in self.canaries
        )

    def sightings(self, text: str) -> list[Sighting]:
        """Les canaris vus dans ``text``, une fois par rôle et par forme.

        De la copie exacte à la copie tronquée, seule la forme la plus fidèle
        est rapportée ; base64 et hexadécimal se rapportent à part.
        """
        lowered = text.lower()
        standard = text.translate(_URL_SAFE)
        seen = []
        for needles in self._needles:
            role, value = needles.canary.role, needles.canary.value
            if value in text:
                seen.append(Sighting(role, "exact"))
            elif value in lowered:
                seen.append(Sighting(role, "case_insensitive"))
            elif needles.canary.draw in lowered:
                seen.append(Sighting(role, "draw"))
            if any(core in standard for core in needles.base64):
                seen.append(Sighting(role, "base64"))
            if needles.hexadecimal in lowered:
                seen.append(Sighting(role, "hex"))
        return sorted(seen)

    def missing(self, text: str) -> list[Role]:
        """Les rôles dont le canari ne figure pas tel quel dans ``text``."""
        return [canary.role for canary in self.canaries if canary.value not in text]
