"""Le pseudonyme d'un destinataire, et le coffre à clés qui seul sait le calculer.

Le calcul, à l'octet près, pour qu'un émetteur l'établisse à l'identique (vecteurs
de test dans ``spec/pseudonym/vectors-v1.json``) :

- forme canonique de l'identité : mise en minuscules (correspondance par défaut
  d'Unicode), puis normalisée en NFC, encodée en UTF-8. Deux écritures d'une même
  adresse, ``Camille.Martin@Exemple.FR`` et ``camille.martin@exemple.fr``, ont le même
  pseudonyme ;
- message = l'étiquette ``answerlineage/pseudonym/recipient/v1`` (ASCII), un octet
  nul, puis la forme canonique ;
- pseudonyme = ``hmac-sha256:`` suivi de HMAC-SHA-256(clé, message) en hexadécimal
  minuscule.

Une identité vide, de plus de ``IDENTITY_MAX`` caractères une fois canonique, ou qui
porte un blanc, un séparateur, un caractère de contrôle, de formatage ou un demi-codet
n'a pas de pseudonyme : une adresse, un identifiant de messagerie ou un numéro n'en
portent pas, et un caractère invisible ferait deux pseudonymes d'une même personne.

La clé est celle du locataire, propre aux pseudonymes : ni la clé des empreintes du
proxy (qui la tiendrait éprouverait aussi les arguments des appels), ni rien que la
base contienne. Elle vit dans le coffre à clés (``PseudonymVault``). Le coffre
d'aujourd'hui (``LocalVault``) la lit d'un fichier ou d'une variable ; l'interface ne
rend jamais une clé, seulement des pseudonymes, pour qu'un KMS qui calcule un HMAC
sans livrer sa clé puisse le remplacer.

Le coffre tient la clé courante, celle des émetteurs, et les clés précédentes que
l'exploitant garde le temps de révéler les pseudonymes calculés avant une rotation :
une identité a un pseudonyme sous chacune.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, Protocol, TypeGuard

PSEUDONYM_PREFIX: Final = "hmac-sha256:"
# La forme d'un pseudonyme, telle que l'ingestion, la base et la révélation l'exigent.
PSEUDONYM_PATTERN: Final = r"^hmac-sha256:[0-9a-f]{64}$"
PSEUDONYM_LENGTH: Final = len(PSEUDONYM_PREFIX) + 64
RECIPIENT_LABEL: Final = b"answerlineage/pseudonym/recipient/v1"
# Une adresse électronique compte au plus 254 caractères (RFC 5321) ; un identifiant
# d'annuaire ou de messagerie, moins encore.
IDENTITY_MAX: Final = 320
# Clé : 256 bits au moins, la taille d'un bloc de SHA-256 au plus, comme celle des empreintes.
MIN_KEY_BYTES: Final = 32
MAX_KEY_BYTES: Final = 64
# La clé courante et les clés précédentes gardées pour révéler : quelques rotations.
MAX_KEYS: Final = 8

ENV_KEYS: Final = "ANSWERLINEAGE_PSEUDONYM_KEYS"
ENV_KEYS_FILE: Final = "ANSWERLINEAGE_PSEUDONYM_KEYS_FILE"
_KEYS_FILE_BYTES: Final = 4096

_PSEUDONYM: Final = re.compile(PSEUDONYM_PATTERN.strip("^$"))
# Ce qu'une identité ne porte pas : contrôle, formatage, demi-codet, séparateurs.
_REFUSED_CATEGORIES: Final = frozenset({"Cc", "Cf", "Cs", "Zs", "Zl", "Zp"})


def is_pseudonym(value: object) -> TypeGuard[str]:
    """Vrai pour un texte qui a la forme d'un pseudonyme."""
    return isinstance(value, str) and _PSEUDONYM.fullmatch(value) is not None


def canonical_identity(identity: str) -> bytes | None:
    """La forme canonique d'une identité, en UTF-8 ; ``None`` pour une identité qui n'en a pas."""
    canonical = unicodedata.normalize("NFC", identity.lower())
    if not canonical or len(canonical) > IDENTITY_MAX:
        return None
    if any(
        character.isspace() or unicodedata.category(character) in _REFUSED_CATEGORIES
        for character in set(canonical)
    ):
        return None
    return canonical.encode("utf-8")


def _mac(secret: bytes, canonical: bytes) -> str:
    digest = hmac.new(secret, RECIPIENT_LABEL + b"\x00" + canonical, hashlib.sha256)
    return PSEUDONYM_PREFIX + digest.hexdigest()


class VaultConfigurationError(ValueError):
    """Le coffre à clés est mal réglé. Le message nomme le réglage, jamais une clé."""


class PseudonymKey:
    """Une clé de pseudonymisation. Elle ne s'affiche jamais, pas même dans ``repr``."""

    __slots__ = ("_secret",)

    def __init__(self, secret: bytes) -> None:
        if not MIN_KEY_BYTES <= len(secret) <= MAX_KEY_BYTES:
            raise VaultConfigurationError(
                f"Une clé de pseudonymisation compte de {MIN_KEY_BYTES} à {MAX_KEY_BYTES} octets."
            )
        self._secret = bytes(secret)

    @classmethod
    def from_hex(cls, text: str) -> PseudonymKey:
        """Lit une clé écrite en hexadécimal. Le message d'erreur ne cite jamais la valeur."""
        try:
            secret = bytes.fromhex(text)
        except ValueError:
            raise VaultConfigurationError(
                "Une clé de pseudonymisation s'écrit en hexadécimal."
            ) from None
        if not MIN_KEY_BYTES <= len(secret) <= MAX_KEY_BYTES:
            raise VaultConfigurationError(
                f"Une clé de pseudonymisation compte de {MIN_KEY_BYTES} à {MAX_KEY_BYTES} octets, "
                f"soit {2 * MIN_KEY_BYTES} à {2 * MAX_KEY_BYTES} caractères hexadécimaux."
            )
        return cls(secret)

    def pseudonym(self, canonical: bytes) -> str:
        """Le pseudonyme d'une identité déjà mise sous sa forme canonique."""
        return _mac(self._secret, canonical)

    def __repr__(self) -> str:
        return "PseudonymKey(<secrète>)"

    __str__ = __repr__

    def __reduce__(self) -> tuple[object, ...]:
        raise TypeError("Une clé de pseudonymisation ne se sérialise pas.")


def pseudonym_of(key: PseudonymKey, identity: str) -> str | None:
    """Le pseudonyme d'une identité sous cette clé, comme un émetteur le calcule ; ``None``
    pour une identité qui n'en a pas."""
    canonical = canonical_identity(identity)
    return None if canonical is None else key.pseudonym(canonical)


class PseudonymVault(Protocol):
    """Le coffre à clés : il calcule des pseudonymes, il ne rend jamais une clé.

    Un KMS qui calcule un HMAC sans livrer sa clé s'y branche : ``pseudonyms`` est
    asynchrone pour lui laisser le temps d'un appel."""

    async def pseudonyms(self, canonical: bytes) -> tuple[str, ...]:
        """Le pseudonyme d'une identité canonique sous chaque clé que le coffre tient."""
        ...  # pragma: no cover — protocole.


class LocalVault:
    """Le coffre d'aujourd'hui : les clés, lues au démarrage, gardées en mémoire."""

    __slots__ = ("_keys",)

    def __init__(self, keys: Sequence[PseudonymKey]) -> None:
        if not 1 <= len(keys) <= MAX_KEYS:
            raise VaultConfigurationError(
                f"Le coffre tient de 1 à {MAX_KEYS} clés de pseudonymisation : la courante, puis "
                "les précédentes gardées pour révéler."
            )
        self._keys = tuple(keys)

    async def pseudonyms(self, canonical: bytes) -> tuple[str, ...]:
        return tuple(key.pseudonym(canonical) for key in self._keys)

    def __repr__(self) -> str:
        return f"LocalVault(<{len(self._keys)} clé(s) secrète(s)>)"

    def __reduce__(self) -> tuple[object, ...]:
        raise TypeError("Un coffre à clés ne se sérialise pas.")


def _keys(text: str) -> list[PseudonymKey]:
    """Les clés d'un texte : en hexadécimal, séparées par des blancs ou des virgules."""
    found = [item for item in re.split(r"[\s,]+", text) if item]
    if not found:
        raise VaultConfigurationError("Le coffre à clés réglé ne contient aucune clé.")
    if len(found) > MAX_KEYS:
        raise VaultConfigurationError(
            f"Le coffre tient au plus {MAX_KEYS} clés de pseudonymisation."
        )
    return [PseudonymKey.from_hex(item) for item in found]


def vault_from_settings(environment: Mapping[str, str]) -> LocalVault | None:
    """Le coffre à clés que règle l'environnement ; ``None`` sans réglage.

    Les clés viennent d'un fichier (``ANSWERLINEAGE_PSEUDONYM_KEYS_FILE``) ou de la
    variable ``ANSWERLINEAGE_PSEUDONYM_KEYS``, pas des deux : en hexadécimal, séparées
    par des blancs, des fins de ligne ou des virgules, la courante d'abord. Aucun
    message d'erreur ne cite une clé, ni le chemin du fichier, qui pourrait en être une
    passée par erreur.
    """
    path = environment.get(ENV_KEYS_FILE, "").strip() or None
    text = environment.get(ENV_KEYS, "").strip() or None
    if path is not None and text is not None:
        raise VaultConfigurationError(
            f"Les clés de pseudonymisation viennent d'un fichier ({ENV_KEYS_FILE}) ou de "
            f"{ENV_KEYS}, pas des deux."
        )
    if path is not None:
        try:
            with Path(path).open("rb") as source:
                content = source.read(_KEYS_FILE_BYTES + 1)
        except OSError:
            raise VaultConfigurationError(
                "Le fichier des clés de pseudonymisation est illisible."
            ) from None
        if len(content) > _KEYS_FILE_BYTES:
            raise VaultConfigurationError("Le fichier des clés de pseudonymisation est trop grand.")
        text = content.decode("ascii", errors="replace")
    if text is None:
        return None
    return LocalVault(_keys(text))
