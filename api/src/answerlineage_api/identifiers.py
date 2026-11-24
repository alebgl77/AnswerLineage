"""Identifiants reçus : jeu de caractères admis, longueur bornée, forme normalisée.

Un identifiant (espace de noms, nom de jeu de données, d'outil, d'agent, canal,
référence de message, destinataire, empreinte, colonne, version) vient d'un
émetteur et finit dans le graphe, l'explorateur et les gabarits de rappel. Il
n'y entre que s'il a la forme d'un nom. Le nom d'une facette suit la même
règle : c'est une clé reçue, qu'une erreur de validation citerait.

La forme d'un nom :

- lettres, marques et chiffres Unicode, plus la ponctuation des noms de
  données (``PUNCTUATION``), utile aux chemins, aux URI et aux sujets Kafka ;
- ni espace ni blanc d'aucune sorte : une phrase n'est pas un nom, et une
  instruction en langage naturel en est une ;
- ni caractère de contrôle, ni caractère invisible ou de formatage (largeur
  nulle, marque de direction, sélecteur de variante, étiquette) ;
- ni chevron, ni guillemet, ni accent grave, même sous une forme de
  compatibilité comme le chevron pleine chasse (U+FF1C) : rien qui forme une
  balise ou sorte d'une chaîne ;
- aucun mot de passe : une adresse s'écrit sans ``utilisateur:motdepasse@``,
  sinon le secret d'un émetteur mal réglé entrerait dans le graphe ;
- une longueur bornée, comptée en caractères de la forme normalisée.

La forme normalisée est NFC : deux écritures d'un même nom, composée et
décomposée, désignent un seul objet du graphe. Le contrôle porte sur cette
forme, et c'est elle qui entre dans le graphe.

Le motif d'un refus est un texte fixe, propre à son code : il ne cite jamais
la valeur reçue.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

NORMAL_FORM: Final = "NFC"

# Ponctuation admise : . et _ des noms qualifiés, - des noms composés, / : @ des
# URI et des chemins, = + ~ % # des chemins partitionnés ou encodés, , des
# listes de serveurs Kafka, $ * des noms de vues système et des tables
# génériques, [ ] des adresses IPv6.
PUNCTUATION: Final = frozenset("._-/:@=+~%,#$*[]")

# Bornes, en caractères de la forme NFC.
NAMESPACE_MAX: Final = 512
DATASET_NAME_MAX: Final = 1024
JOB_NAME_MAX: Final = 512
TOOL_MAX: Final = 255
COLUMN_MAX: Final = 300
VERSION_MAX: Final = 255
CHANNEL_MAX: Final = 64
MESSAGE_REF_MAX: Final = 512
RECIPIENT_MAX: Final = 255
FINGERPRINT_MAX: Final = 255
# Instance d'un émetteur, dans un relevé de couverture : un nom de pod, d'hôte, un tirage.
INSTANCE_MAX: Final = 255
# Nom d'une facette, choisi par l'émetteur : ``answerlineage_toolCall``,
# ``spark.logicalPlan``, ``environment-properties``.
FACET_NAME_MAX: Final = 128

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

# Motif de chaque code ; ``{max_length}`` est la borne du champ, jamais sa valeur.
FAULT_CODES: Final[Mapping[str, str]] = {
    "identifier_empty": "Un identifiant ne peut pas être vide.",
    "identifier_too_long": "Un identifiant compte au plus {max_length} caractères.",
    "identifier_control": "Un identifiant ne contient aucun caractère de contrôle.",
    "identifier_invisible": (
        "Un identifiant ne contient aucun caractère invisible ou de formatage "
        "(largeur nulle, marque de direction, sélecteur de variante)."
    ),
    "identifier_markup": (
        "Un identifiant ne contient ni chevron, ni guillemet, ni accent grave : "
        "rien qui puisse former une balise."
    ),
    "identifier_whitespace": (
        "Un identifiant ne contient ni espace ni blanc : une phrase n'est pas un nom."
    ),
    "identifier_forbidden_character": (
        "Un identifiant ne contient que des lettres, des chiffres et les signes "
        ". _ - / : @ = + ~ % , # $ * [ ]."
    ),
    "identifier_credentials": (
        "Un identifiant ne porte pas de mot de passe : une adresse s'écrit sans "
        "« utilisateur:motdepasse@ »."
    ),
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

# Un mot de passe devant l'hôte d'une adresse : « schéma://utilisateur:motdepasse@hôte ».
_CREDENTIALS: Final = re.compile(r"://[^/@]*:[^/@]*@")

# Propriété Unicode Default_Ignorable_Code_Point (DerivedCoreProperties.txt) :
# des caractères sans rendu, dont certains sont classés lettres ou marques
# (remplissages hangûl, sélecteurs de variante).
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
class Fault:
    """Motif du refus d'un identifiant : un code stable et un texte fixe."""

    code: FaultCode
    message: str


class InvalidIdentifierError(ValueError):
    """Un identifiant n'a pas une forme admise."""

    def __init__(self, fault: Fault) -> None:
        super().__init__(fault.code)
        self.fault = fault


def _fault(code: FaultCode, max_length: int) -> Fault:
    return Fault(code, FAULT_CODES[code].format(max_length=max_length))


def _invisible(code_point: int) -> bool:
    return any(low <= code_point <= high for low, high in _DEFAULT_IGNORABLE)


def _character_fault(character: str) -> FaultCode | None:
    if character in PUNCTUATION:
        return None
    if character.isspace():
        return "identifier_whitespace"
    category = unicodedata.category(character)
    if category == "Cc":
        return "identifier_control"
    if category == "Cf" or _invisible(ord(character)):
        return "identifier_invisible"
    if _MARKUP.intersection(unicodedata.normalize("NFKC", character)):
        return "identifier_markup"
    if category[0] in "LMN":
        return None
    return "identifier_forbidden_character"


def normalize_identifier(value: str) -> str:
    """Forme normalisée (NFC) d'un identifiant."""
    return unicodedata.normalize(NORMAL_FORM, value)


def identifier_fault(value: str, *, max_length: int) -> Fault | None:
    """Motif du refus d'un identifiant, ou ``None`` s'il a une forme admise."""
    normalized = normalize_identifier(value)
    if not normalized:
        return _fault("identifier_empty", max_length)
    found = {_character_fault(character) for character in set(normalized)}
    for code in _PRIORITY:
        if code in found:
            return _fault(code, max_length)
    if _CREDENTIALS.search(normalized):
        return _fault("identifier_credentials", max_length)
    if len(normalized) > max_length:
        return _fault("identifier_too_long", max_length)
    return None


def checked_identifier(value: str, *, max_length: int) -> str:
    """Forme normalisée d'un identifiant admis ; lève ``InvalidIdentifierError`` sinon."""
    fault = identifier_fault(value, max_length=max_length)
    if fault is not None:
        raise InvalidIdentifierError(fault)
    return normalize_identifier(value)
