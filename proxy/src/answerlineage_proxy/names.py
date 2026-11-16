"""Forme d'un nom de jeu de données ou de colonne, la même que celle de l'ingestion.

Un nom déclaré dans une règle de lecture finit dans l'événement, puis dans le
graphe : le proxy n'accepte que ce que l'API d'ingestion accepterait. Ces règles
sont écrites deux fois, ici et dans l'API (``answerlineage_api.identifiers``),
pour qu'aucun service ne dépende de l'autre ;
``tests/test_contract_identifiers.py`` les tient d'accord.

La forme d'un nom : lettres, marques et chiffres Unicode, plus la ponctuation des
noms de données ; ni blanc, ni caractère de contrôle, invisible ou de balisage ;
aucun mot de passe dans une adresse ; une longueur bornée, comptée sur la forme
normalisée NFC. Le motif d'un refus est un code et un texte fixes : jamais la
valeur reçue.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Final, Literal

NORMAL_FORM: Final = "NFC"
PUNCTUATION: Final = frozenset("._-/:@=+~%,#$*[]")

# Bornes de l'ingestion, en caractères de la forme NFC.
NAMESPACE_MAX: Final = 512
DATASET_NAME_MAX: Final = 1024
COLUMN_MAX: Final = 300

FaultCode = Literal[
    "identifier_empty",
    "identifier_too_long",
    "identifier_control",
    "identifier_invisible",
    "identifier_markup",
    "identifier_whitespace",
    "identifier_forbidden_character",
    "identifier_credentials",
]

_MESSAGES: Final[dict[FaultCode, str]] = {
    "identifier_empty": "un nom ne peut pas être vide",
    "identifier_too_long": "un nom compte au plus {max_length} caractères",
    "identifier_control": "un nom ne contient aucun caractère de contrôle",
    "identifier_invisible": "un nom ne contient aucun caractère invisible ou de formatage",
    "identifier_markup": "un nom ne contient ni chevron, ni guillemet, ni accent grave",
    "identifier_whitespace": "un nom ne contient ni espace ni blanc",
    "identifier_forbidden_character": (
        "un nom ne contient que des lettres, des chiffres "
        "et les signes . _ - / : @ = + ~ % , # $ * [ ]"
    ),
    "identifier_credentials": "une adresse s'écrit sans « utilisateur:motdepasse@ »",
}

# Quand une valeur a plusieurs défauts, le plus grave est rapporté.
_PRIORITY: Final[tuple[FaultCode, ...]] = (
    "identifier_control",
    "identifier_invisible",
    "identifier_markup",
    "identifier_whitespace",
    "identifier_forbidden_character",
)
_MARKUP: Final = frozenset("<>\"'`")
_CREDENTIALS: Final = re.compile(r"://[^/@]*:[^/@]*@")
# Propriété Unicode Default_Ignorable_Code_Point : des caractères sans rendu.
_DEFAULT_IGNORABLE: Final = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)


@dataclass(frozen=True)
class NameFault:
    """Motif du refus d'un nom : le code de l'ingestion et un texte fixe."""

    code: FaultCode
    message: str


def _character_fault(character: str) -> FaultCode | None:
    if character in PUNCTUATION:
        return None
    if character.isspace():
        return "identifier_whitespace"
    category = unicodedata.category(character)
    if category == "Cc":
        return "identifier_control"
    code_point = ord(character)
    if category == "Cf" or any(low <= code_point <= high for low, high in _DEFAULT_IGNORABLE):
        return "identifier_invisible"
    if _MARKUP.intersection(unicodedata.normalize("NFKC", character)):
        return "identifier_markup"
    if category[0] in "LMN":
        return None
    return "identifier_forbidden_character"


def normalize_name(value: str) -> str:
    return unicodedata.normalize(NORMAL_FORM, value)


def name_fault(value: str, *, max_length: int) -> NameFault | None:
    """Motif du refus d'un nom, ou ``None`` s'il a une forme que l'ingestion admet."""
    normalized = normalize_name(value)
    code: FaultCode | None = None
    if not normalized:
        code = "identifier_empty"
    else:
        found = {_character_fault(character) for character in set(normalized)}
        code = next((candidate for candidate in _PRIORITY if candidate in found), None)
        if code is None and _CREDENTIALS.search(normalized):
            code = "identifier_credentials"
        if code is None and len(normalized) > max_length:
            code = "identifier_too_long"
    if code is None:
        return None
    return NameFault(code, _MESSAGES[code].format(max_length=max_length))
