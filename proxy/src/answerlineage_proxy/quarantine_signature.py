"""La vérification de la diffusion des périmètres en quarantaine, et le secret qu'elle emploie.

L'API signe et date chaque réponse de ``GET /api/v1/quarantine`` dans un
en-tête, sous un secret qu'elle partage avec les proxys :

    AnswerLineage-Signature: t=1794819310,v1=af95…

- ``t`` est l'heure de signature, en secondes depuis le 1er janvier 1970 (UTC),
  écrite en décimal sans zéro en tête ;
- ``v1`` est HMAC-SHA-256, en hexadécimal minuscule, sous le secret partagé, de
  l'étiquette ``answerlineage/quarantine/v1``, d'un octet nul, de ``t``, d'un
  point, puis de la charge : le corps d'un ``200`` tel qu'il est reçu, ou, pour
  un ``304``, qui n'a pas de corps, la version que le proxy tient. L'étiquette
  sépare ce domaine des autres : la signature d'un webhook ne vaut jamais pour
  une diffusion ;
- pendant une rotation, l'API signe sous ses deux secrets : le proxy accepte dès
  que l'une des signatures est juste sous le sien.

La vérification refuse un en-tête absent ou illisible, une signature fausse
(charge altérée, secret autre, version autre), puis une heure à plus de cinq
minutes de l'horloge du proxy, dans un sens ou dans l'autre : une réponse
captée ne se rejoue pas au-delà, et un ``304`` rejoué ne prolonge pas un état
levé. Chaque refus a un code d'une liste fermée, qui ne cite rien de la réponse.

L'API signe de son côté (``answerlineage_api.incidents.signature``) : aucun des
deux ne dépend de l'autre, et le test de contrat les tient d'accord sur les
vecteurs publiés (``spec/quarantine/v1/vectors.json``).

Le secret se lit d'un fichier ou d'une variable d'environnement, jamais de la
ligne de commande, que la liste des processus montre ; aucun message ne le cite,
ni le chemin de son fichier, et le serveur MCP lancé en stdio ne le reçoit pas.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from answerlineage_proxy.config import ProxyConfigurationError

HEADER: Final = "AnswerLineage-Signature"
SCHEME: Final = "v1"
LABEL: Final = b"answerlineage/quarantine/v1"
# Écart admis entre l'heure de signature et l'horloge du proxy, dans les deux sens.
TOLERANCE: Final = 300
# Un en-tête plus long, ou qui porte plus de signatures, n'est pas lu.
MAX_HEADER_LENGTH: Final = 1024
MAX_SIGNATURES: Final = 8

# Le secret : jamais sur la ligne de commande. Les noms des variables, pas des secrets.
ENV_SECRET: Final = "ANSWERLINEAGE_PROXY_QUARANTINE_SECRET"  # noqa: S105
ENV_SECRET_FILE: Final = "ANSWERLINEAGE_PROXY_QUARANTINE_SECRET_FILE"  # noqa: S105
# Un secret partagé : de l'ASCII imprimable sans espace, de 32 à 512 caractères, comme
# celui que l'API admet.
MIN_SECRET_LENGTH: Final = 32
MAX_SECRET_LENGTH: Final = 512
_SECRET_FILE_BYTES: Final = 4096

# Codes d'un refus, d'une liste fermée.
UNSIGNED: Final = "unsigned"
MALFORMED: Final = "malformed_signature"
MISMATCH: Final = "signature_mismatch"
EXPIRED: Final = "signature_expired"
FROM_FUTURE: Final = "signature_from_future"

_TIMESTAMP = re.compile(r"0|[1-9][0-9]{0,11}")
_SIGNATURE = re.compile(r"[0-9a-f]{64}")
_KEY = re.compile(r"[a-z0-9]{1,16}")
_SECRET = re.compile(rf"[\x21-\x7e]{{{MIN_SECRET_LENGTH},{MAX_SECRET_LENGTH}}}")


class SignatureRefusedError(Exception):
    """La diffusion est écartée. ``code`` dit pourquoi, d'une liste fermée."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _signed(secret: str, timestamp: int, payload: bytes) -> str:
    message = LABEL + b"\x00" + str(timestamp).encode("ascii") + b"." + payload
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _parsed(header: str | None) -> tuple[int, list[str]]:
    """L'heure et les signatures ``v1`` de l'en-tête ; les autres clés sont ignorées, pour
    qu'un schéma de signature à venir n'empêche pas de lire celui-ci."""
    if header is None:
        raise SignatureRefusedError(UNSIGNED)
    if len(header) > MAX_HEADER_LENGTH:
        raise SignatureRefusedError(MALFORMED)
    timestamps: list[str] = []
    signatures: list[str] = []
    for part in header.split(","):
        key, equals, value = part.partition("=")
        if not equals or not _KEY.fullmatch(key):
            raise SignatureRefusedError(MALFORMED)
        if key == "t":
            timestamps.append(value)
        elif key == SCHEME:
            if not _SIGNATURE.fullmatch(value):
                raise SignatureRefusedError(MALFORMED)
            signatures.append(value)
    if len(timestamps) != 1 or not _TIMESTAMP.fullmatch(timestamps[0]):
        raise SignatureRefusedError(MALFORMED)
    if not signatures or len(signatures) > MAX_SIGNATURES:
        raise SignatureRefusedError(MALFORMED)
    return int(timestamps[0]), signatures


def verify(
    header: str | None,
    payload: bytes,
    secret: str,
    *,
    now: float,
    tolerance: int = TOLERANCE,
) -> int:
    """Vérifie une diffusion ; rend son heure de signature, ou lève ``SignatureRefusedError``.

    ``header`` est la valeur reçue de l'en-tête ``AnswerLineage-Signature``, ``None`` sans
    lui ; ``payload``, le corps reçu d'un ``200``, ou la version tenue pour un ``304`` ;
    ``now``, l'horloge du proxy, en secondes depuis 1970. La signature est jugée avant
    l'heure : seule une diffusion authentique se dit trop ancienne ou venue du futur.
    """
    if tolerance < 0:
        raise ValueError("La tolérance est un nombre de secondes positif.")
    timestamp, signatures = _parsed(header)
    expected = _signed(secret, timestamp, payload)
    # Chaque comparaison prend le même temps, juste ou fausse.
    matches = [hmac.compare_digest(expected, signature) for signature in signatures]
    if not any(matches):
        raise SignatureRefusedError(MISMATCH)
    if timestamp < now - tolerance:
        raise SignatureRefusedError(EXPIRED)
    if timestamp > now + tolerance:
        raise SignatureRefusedError(FROM_FUTURE)
    return timestamp


def checked_secret(value: str) -> str:
    """Le secret, s'il est de l'ASCII imprimable sans espace, de 32 à 512 caractères.

    Le message nomme les variables, jamais la valeur.
    """
    if not _SECRET.fullmatch(value):
        raise ProxyConfigurationError(
            f"Le secret de la diffusion des quarantaines ({ENV_SECRET} ou {ENV_SECRET_FILE}) est"
            f" de l'ASCII imprimable sans espace, de {MIN_SECRET_LENGTH} à {MAX_SECRET_LENGTH}"
            " caractères."
        )
    return value


def read_secret(environment: Mapping[str, str], secret_file: str | None = None) -> str | None:
    """Le secret de la diffusion : dans un fichier (``secret_file``, sinon celui que nomme
    ``ANSWERLINEAGE_PROXY_QUARANTINE_SECRET_FILE``) ou dans la variable
    ``ANSWERLINEAGE_PROXY_QUARANTINE_SECRET``. ``None`` sans secret.

    Aucun message ne cite le secret, ni le chemin du fichier.
    """
    path = secret_file or environment.get(ENV_SECRET_FILE) or None
    text = environment.get(ENV_SECRET) or None
    if path is not None and text is not None:
        raise ProxyConfigurationError(
            f"Le secret de la diffusion des quarantaines vient d'un fichier ou de {ENV_SECRET},"
            " pas des deux."
        )
    if path is not None:
        try:
            with Path(path).open("rb") as source:
                content = source.read(_SECRET_FILE_BYTES + 1)
        except OSError:
            raise ProxyConfigurationError(
                "Le fichier du secret de la diffusion des quarantaines est illisible."
            ) from None
        if len(content) > _SECRET_FILE_BYTES:
            raise ProxyConfigurationError(
                "Le fichier du secret de la diffusion des quarantaines est trop grand."
            )
        try:
            text = content.decode("utf-8").strip()
        except UnicodeDecodeError:
            raise ProxyConfigurationError(
                "Le fichier du secret de la diffusion des quarantaines n'est pas du texte UTF-8."
            ) from None
    return None if text is None else checked_secret(text)
