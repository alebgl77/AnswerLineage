"""Forme d'un identifiant reçu de l'API, avant de l'afficher.

Un nom de jeu de données, d'outil, d'agent, un canal, une empreinte, un
pseudonyme ou un émetteur ne s'affiche que s'il a la forme d'un nom : lettres,
marques et chiffres Unicode, plus la ponctuation des noms de données ; ni
espace, ni caractère de contrôle, invisible ou de formatage, ni chevron,
guillemet ou accent grave, ni mot de passe dans une adresse ; une longueur
bornée. Une séquence d'échappement du terminal, un saut de ligne qui
simulerait une autre ligne de la sortie ou une phrase glissée dans un nom ne
passent donc pas.

C'est la règle de l'ingestion (``answerlineage_api.identifiers``), écrite une
seconde fois pour que la commande ne dépende d'aucun service ;
``tests/test_contract_cli.py`` tient les deux d'accord.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

NORMAL_FORM: Final = "NFC"
PUNCTUATION: Final = frozenset("._-/:@=+~%,#$*[]")

# Bornes de l'ingestion, en caractères de la forme NFC.
NAMESPACE_MAX: Final = 512
DATASET_NAME_MAX: Final = 1024
JOB_NAME_MAX: Final = 512
TOOL_MAX: Final = 255
CHANNEL_MAX: Final = 64
RECIPIENT_MAX: Final = 255
FINGERPRINT_MAX: Final = 255

_MARKUP: Final = frozenset("<>\"'`")
_CREDENTIALS: Final = re.compile(r"://[^/@]*:[^/@]*@")

# Propriété Unicode Default_Ignorable_Code_Point : des caractères sans rendu,
# dont certains sont classés lettres ou marques.
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


def _admitted(character: str) -> bool:
    if character in PUNCTUATION:
        return True
    if character.isspace():
        return False
    code_point = ord(character)
    if any(low <= code_point <= high for low, high in _DEFAULT_IGNORABLE):
        return False
    if _MARKUP.intersection(unicodedata.normalize("NFKC", character)):
        return False
    return unicodedata.category(character)[0] in "LMN"


def is_identifier(value: str, *, max_length: int) -> bool:
    """Vrai quand la valeur a la forme d'un nom que l'ingestion admet."""
    normalized = unicodedata.normalize(NORMAL_FORM, value)
    return (
        0 < len(normalized) <= max_length
        and all(_admitted(character) for character in set(normalized))
        and _CREDENTIALS.search(normalized) is None
    )
