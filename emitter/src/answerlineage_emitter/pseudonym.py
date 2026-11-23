"""Le pseudonyme d'un destinataire, calculé là où l'émetteur connaît l'identité.

Un destinataire n'entre dans AnswerLineage que sous son pseudonyme : un HMAC de
son identité, sous la clé de pseudonymisation du locataire. L'émetteur le
calcule dans son processus ; l'identité n'en sort jamais, et ne traverse pas
l'API. L'API fait le même calcul pour révéler un destinataire : les deux sont
écrits chacun de leur côté, pour qu'aucun composant ne dépende de l'autre, et
les vecteurs publiés (``spec/pseudonym/vectors-v1.json``) les tiennent d'accord.

Le calcul, à l'octet près :

- forme canonique de l'identité : mise en minuscules (correspondance par défaut
  d'Unicode), puis normalisée en NFC, encodée en UTF-8 ;
- message = l'étiquette ``answerlineage/pseudonym/recipient/v1`` (ASCII), un
  octet nul, puis la forme canonique ;
- pseudonyme = ``hmac-sha256:`` suivi de HMAC-SHA-256(clé, message) en
  hexadécimal minuscule.

Une identité vide, de plus de 320 caractères une fois canonique, ou qui porte un
blanc, un séparateur, un caractère de contrôle, de formatage ou un demi-codet
n'a pas de pseudonyme : l'émetteur l'écarte, sans la recopier.

La clé est propre aux pseudonymes : ce n'est pas celle des empreintes, que
l'API ne doit pas tenir. Elle se lit d'un fichier ou d'une variable
d'environnement que l'émetteur nomme (``read_pseudonym_key``), jamais de sa
ligne de commande ; elle ne s'affiche ni ne se sérialise.
"""

from __future__ import annotations

import hashlib
import hmac
import unicodedata
from collections.abc import Mapping
from typing import Final

from answerlineage_emitter.config import ConfigurationError, key_text
from answerlineage_emitter.fingerprint import FingerprintKey

PSEUDONYM_PREFIX: Final = "hmac-sha256:"
RECIPIENT_LABEL: Final = b"answerlineage/pseudonym/recipient/v1"
# Une adresse électronique compte au plus 254 caractères ; un identifiant de messagerie,
# moins encore.
IDENTITY_MAX: Final = 320
# Clé : 256 bits au moins, la taille d'un bloc de SHA-256 au plus.
MIN_KEY_BYTES: Final = 32
MAX_KEY_BYTES: Final = 64
# La normalisation NFC réunit au plus quatre caractères en un (« ᾂ » décomposé) : une
# identité plus longue que ceci n'a pas de forme canonique assez courte, et n'est pas
# parcourue.
_RAW_MAX: Final = 4 * IDENTITY_MAX
# Contrôle, formatage, demi-codet, séparateurs d'espace, de ligne et de paragraphe.
_REFUSED: Final = frozenset({"Cc", "Cf", "Cs", "Zs", "Zl", "Zp"})
_WHAT: Final = "la clé des pseudonymes"
# L'étiquette qui compare deux clés, propre à cette comparaison.
_KEY_CHECK_LABEL: Final = b"answerlineage/key-check/v1"


def canonical_identity(identity: object) -> bytes | None:
    """La forme canonique d'une identité, en UTF-8 ; ``None`` pour ce qui n'en a pas."""
    if not isinstance(identity, str) or len(identity) > _RAW_MAX:
        return None
    canonical = unicodedata.normalize("NFC", identity.lower())
    if not 0 < len(canonical) <= IDENTITY_MAX:
        return None
    for character in canonical:
        if character.isspace() or unicodedata.category(character) in _REFUSED:
            return None
    return canonical.encode("utf-8")


class PseudonymKey:
    """Clé de pseudonymisation du locataire. Elle ne s'affiche jamais, pas même dans ``repr``."""

    __slots__ = ("_secret",)

    def __init__(self, secret: bytes) -> None:
        if not MIN_KEY_BYTES <= len(secret) <= MAX_KEY_BYTES:
            raise ValueError(
                f"La clé des pseudonymes compte de {MIN_KEY_BYTES} à {MAX_KEY_BYTES} octets."
            )
        self._secret = bytes(secret)

    @classmethod
    def from_hex(cls, text: str) -> PseudonymKey:
        """Lit une clé écrite en hexadécimal. Le message d'erreur ne cite jamais la valeur."""
        try:
            secret = bytes.fromhex(text.strip())
        except ValueError:
            raise ValueError("La clé des pseudonymes s'écrit en hexadécimal.") from None
        if not MIN_KEY_BYTES <= len(secret) <= MAX_KEY_BYTES:
            raise ValueError(
                f"La clé des pseudonymes compte de {MIN_KEY_BYTES} à {MAX_KEY_BYTES} octets, "
                f"soit {2 * MIN_KEY_BYTES} à {2 * MAX_KEY_BYTES} caractères hexadécimaux."
            )
        return cls(secret)

    def pseudonym(self, identity: object) -> str | None:
        """Le pseudonyme d'une identité sous cette clé ; ``None`` pour une identité qui n'en
        a pas."""
        canonical = canonical_identity(identity)
        if canonical is None:
            return None
        digest = hmac.new(self._secret, RECIPIENT_LABEL + b"\x00" + canonical, hashlib.sha256)
        return PSEUDONYM_PREFIX + digest.hexdigest()

    def shares_secret_with(self, key: FingerprintKey) -> bool:
        """Vrai quand la clé d'empreinte a le même secret. Les deux clés empreignent un même
        message fixe, sous une étiquette qu'aucun usage ne prend : aucune ne livre son secret."""
        message = _KEY_CHECK_LABEL + b"\x00"
        mine = PSEUDONYM_PREFIX + hmac.new(self._secret, message, hashlib.sha256).hexdigest()
        return hmac.compare_digest(mine, key.fingerprint(_KEY_CHECK_LABEL, b""))

    def __repr__(self) -> str:
        return "PseudonymKey(<secrète>)"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PseudonymKey):
            return NotImplemented
        return hmac.compare_digest(self._secret, other._secret)

    def __hash__(self) -> int:
        return hash(PseudonymKey)

    def __reduce__(self) -> tuple[object, ...]:
        raise TypeError("Une clé des pseudonymes ne se sérialise pas.")


def read_pseudonym_key(
    environment: Mapping[str, str],
    key_file: str | None = None,
    *,
    variable: str,
    file_variable: str,
) -> PseudonymKey | None:
    """Lit la clé des pseudonymes : dans un fichier (``key_file``, sinon celui que nomme la
    variable ``file_variable``) ou dans la variable ``variable``, en hexadécimal. ``None``
    sans clé. Aucun message ne cite la clé, ni le chemin du fichier."""
    text = key_text(
        environment, key_file, variable=variable, file_variable=file_variable, what=_WHAT
    )
    if text is None:
        return None
    try:
        return PseudonymKey.from_hex(text)
    except ValueError as error:
        # Le message de PseudonymKey ne cite jamais la valeur.
        raise ConfigurationError(str(error)) from None
