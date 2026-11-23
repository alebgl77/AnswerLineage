"""Empreintes à clé des arguments et du résultat d'un appel d'outil.

Une empreinte est un HMAC-SHA-256, calculé avec une clé secrète propre à un
locataire : la même valeur donne chez deux locataires deux empreintes
différentes, et qui ne détient pas la clé ne retrouve pas une valeur peu
variée en essayant des candidates. Sans clé, aucune empreinte n'est calculée :
un hachage sans clé s'inverserait par simple énumération.

Le calcul, à l'octet près, pour qu'un autre émetteur l'établisse à l'identique
(vecteurs de test dans ``spec/fingerprint/vectors-v1.json``) :

- message = étiquette de l'usage (ASCII), un octet nul, puis les octets
  empreints ;
- empreinte = ``hmac-sha256:`` suivi de HMAC-SHA-256(clé, message) en
  hexadécimal minuscule ;
- arguments : la forme canonique JSON (RFC 8785) de ``params.arguments``, en
  UTF-8 ; un appel sans arguments est empreint comme ``{}`` ;
- résultat : les octets du corps de la réponse tels que le proxy les relaie ;
  pour un adaptateur, qui ne voit que le résultat décodé, sa forme canonique
  JSON.

La clé se lit d'un fichier ou d'une variable d'environnement que chaque
émetteur nomme (``read_key``) : jamais de sa ligne de commande.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from answerlineage_emitter.config import ConfigurationError

FINGERPRINT_PREFIX: Final = "hmac-sha256:"
ARGUMENTS_LABEL: Final = b"answerlineage/fingerprint/arguments/v1"
RESULT_LABEL: Final = b"answerlineage/fingerprint/result/v1"
# Longueur de la clé, en octets : 256 bits au moins, la taille d'un bloc de SHA-256 au plus.
MIN_KEY_BYTES: Final = 32
MAX_KEY_BYTES: Final = 64
# Plus grand entier qu'un nombre JSON en double précision représente exactement.
_EXACT_INTEGER: Final = 2**53
# Fichier de la clé lu au plus : une clé en hexadécimal, ses blancs et sa fin de ligne.
_KEY_FILE_BYTES: Final = 4096


class FingerprintKey:
    """Clé secrète d'empreinte. Elle ne s'affiche jamais, pas même dans ``repr``."""

    __slots__ = ("_secret",)

    def __init__(self, secret: bytes) -> None:
        if not MIN_KEY_BYTES <= len(secret) <= MAX_KEY_BYTES:
            raise ValueError(
                f"La clé d'empreinte doit compter de {MIN_KEY_BYTES} à {MAX_KEY_BYTES} octets."
            )
        self._secret = bytes(secret)

    @classmethod
    def from_hex(cls, text: str) -> FingerprintKey:
        """Lit une clé écrite en hexadécimal. Le message d'erreur ne cite jamais la valeur."""
        try:
            secret = bytes.fromhex(text.strip())
        except ValueError:
            raise ValueError("La clé d'empreinte doit être écrite en hexadécimal.") from None
        if not MIN_KEY_BYTES <= len(secret) <= MAX_KEY_BYTES:
            raise ValueError(
                f"La clé d'empreinte doit compter de {MIN_KEY_BYTES} à {MAX_KEY_BYTES} octets, "
                f"soit {2 * MIN_KEY_BYTES} à {2 * MAX_KEY_BYTES} caractères hexadécimaux."
            )
        return cls(secret)

    def __repr__(self) -> str:
        return "FingerprintKey(<secrète>)"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FingerprintKey):
            return NotImplemented
        return hmac.compare_digest(self._secret, other._secret)

    def __hash__(self) -> int:
        return hash(FingerprintKey)

    def __reduce__(self) -> tuple[object, ...]:
        raise TypeError("Une clé d'empreinte ne se sérialise pas.")

    def digest(self, label: bytes) -> Digest:
        """Empreinte en cours de calcul, pour l'usage que nomme ``label``."""
        return Digest(self._secret, label)

    def fingerprint(self, label: bytes, data: bytes) -> str:
        digest = self.digest(label)
        digest.update(data)
        return digest.fingerprint()


class Digest:
    """Empreinte calculée au fil de l'eau : les octets ne sont pas gardés."""

    __slots__ = ("_mac",)

    def __init__(self, secret: bytes, label: bytes) -> None:
        self._mac = hmac.new(secret, label + b"\x00", hashlib.sha256)

    def update(self, data: bytes) -> None:
        self._mac.update(data)

    def fingerprint(self) -> str:
        return FINGERPRINT_PREFIX + self._mac.hexdigest()


def _number(value: int | float) -> str:
    """Un nombre comme l'écrit ECMAScript, ce que la RFC 8785 exige (section 3.2.2.3)."""
    if isinstance(value, int) and abs(value) <= _EXACT_INTEGER:
        return str(value)
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nombre non fini")
    if number == 0:
        return "0"
    sign = "-" if number < 0 else ""
    # repr donne les chiffres les plus courts qui redonnent le même nombre.
    mantissa, _, exponent_text = repr(abs(number)).partition("e")
    whole, _, fraction = mantissa.partition(".")
    raw = whole + fraction
    leading = len(raw) - len(raw.lstrip("0"))
    digits = raw.strip("0")
    # Le nombre vaut 0,<digits> fois 10 puissance point.
    point = int(exponent_text or 0) + len(whole) - leading
    count = len(digits)
    if count <= point <= 21:
        text = digits + "0" * (point - count)
    elif 0 < point <= 21:
        text = f"{digits[:point]}.{digits[point:]}"
    elif -6 < point <= 0:
        text = "0." + "0" * -point + digits
    else:
        exponent = point - 1
        head = digits[0] + (f".{digits[1:]}" if count > 1 else "")
        text = f"{head}e{'+' if exponent >= 0 else '-'}{abs(exponent)}"
    return sign + text


def _canonical(value: object) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        # Mêmes échappements que la RFC 8785 : « " », « \ » et les caractères de contrôle.
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int | float):
        return _number(value)
    if isinstance(value, list):
        return "[" + ",".join(_canonical(item) for item in value) + "]"
    if isinstance(value, dict):
        # Clés triées sur leurs unités de code UTF-16, comme la RFC 8785 le prescrit.
        keys = sorted(value, key=lambda key: str(key).encode("utf-16-be", "surrogatepass"))
        members = (f"{_canonical(str(key))}:{_canonical(value[key])}" for key in keys)
        return "{" + ",".join(members) + "}"
    raise ValueError("valeur hors de JSON")


def canonical_json(value: object) -> bytes | None:
    """Forme canonique JSON (RFC 8785) d'une valeur lue dans un corps JSON.

    ``None`` pour une valeur sans forme canonique (nombre non fini, texte qui
    n'est pas de l'Unicode valide, imbrication trop profonde) : on n'en calcule
    pas d'empreinte plutôt qu'une empreinte qu'un autre émetteur ne retrouverait pas.
    """
    try:
        return _canonical(value).encode("utf-8")
    except (ValueError, OverflowError, RecursionError, UnicodeEncodeError):
        return None


def arguments_fingerprint(key: FingerprintKey, arguments: object) -> str | None:
    """Empreinte des arguments d'un appel, indépendante de l'écriture du JSON reçu."""
    canonical = canonical_json(arguments)
    return key.fingerprint(ARGUMENTS_LABEL, canonical) if canonical is not None else None


def read_key(
    environment: Mapping[str, str],
    key_file: str | None = None,
    *,
    variable: str,
    file_variable: str,
) -> FingerprintKey | None:
    """Lit la clé d'empreinte : dans un fichier (``key_file``, sinon celui que nomme la
    variable ``file_variable``) ou dans la variable ``variable``, en hexadécimal. ``None``
    sans clé.

    Aucun message d'erreur ne cite la clé, ni le chemin du fichier, qui pourrait
    être une clé passée par erreur.
    """
    path = key_file or environment.get(file_variable) or None
    text = environment.get(variable) or None
    if path is not None and text is not None:
        raise ConfigurationError(
            f"La clé d'empreinte vient d'un fichier ou de {variable}, pas des deux."
        )
    if path is not None:
        try:
            with Path(path).open("rb") as source:
                content = source.read(_KEY_FILE_BYTES + 1)
        except OSError:
            raise ConfigurationError("Le fichier de la clé d'empreinte est illisible.") from None
        if len(content) > _KEY_FILE_BYTES:
            raise ConfigurationError("Le fichier de la clé d'empreinte est trop grand.")
        text = content.decode("ascii", errors="replace")
    if text is None:
        return None
    try:
        return FingerprintKey.from_hex(text)
    except ValueError as error:
        # Le message de FingerprintKey ne cite jamais la valeur.
        raise ConfigurationError(str(error)) from None
