"""Signature des webhooks d'AnswerLineage, et sa vérification de référence.

Ce module n'emploie que la bibliothèque standard de Python et n'importe rien
d'AnswerLineage : un récepteur peut l'importer, ou le copier tel quel. Les
vecteurs de ``spec/webhooks/v1/vectors.json`` l'éprouvent, et éprouvent toute
autre écriture de la vérification.

Chaque message part avec un en-tête qui porte l'heure d'émission et la
signature, à la manière des webhooks signés courants :

    AnswerLineage-Signature: t=1763892000,v1=5f2b…

- ``t`` est l'heure d'émission, en secondes depuis le 1er janvier 1970 (UTC),
  écrite en décimal ;
- ``v1`` est HMAC-SHA-256, en hexadécimal minuscule, sous le secret partagé,
  de ``t``, d'un point, puis du corps tel qu'il est reçu, octet pour octet ;
- pendant une rotation du secret, l'en-tête porte une signature ``v1`` par
  secret en service : le récepteur accepte le message dès que l'une d'elles
  est juste sous l'un des secrets qu'il connaît.

La vérification refuse un en-tête illisible, une signature fausse (corps
altéré, secret autre) et un message trop ancien ou venu du futur, au-delà
d'une tolérance de cinq minutes par défaut. Le message signé porte aussi un
identifiant de livraison, le même d'un envoi à l'autre : un récepteur qui garde
ceux qu'il a vus pendant la tolérance écarte aussi un rejeu dans la fenêtre.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from collections.abc import Sequence
from typing import Final

HEADER: Final = "AnswerLineage-Signature"
SCHEME: Final = "v1"
# Écart admis entre l'heure d'émission et l'horloge du récepteur, dans les deux sens.
DEFAULT_TOLERANCE: Final = 300
# Un en-tête plus long, ou qui porte plus de signatures, n'est pas lu.
MAX_HEADER_LENGTH: Final = 1024
MAX_SIGNATURES: Final = 8

# Codes d'un refus, d'une liste fermée.
MALFORMED_HEADER: Final = "malformed_header"
SIGNATURE_MISMATCH: Final = "signature_mismatch"
OUTSIDE_TOLERANCE: Final = "timestamp_outside_tolerance"

_TIMESTAMP = re.compile(r"[0-9]{1,12}")
_SIGNATURE = re.compile(r"[0-9a-f]{64}")
_KEY = re.compile(r"[a-z0-9]{1,16}")


class SignatureError(ValueError):
    """Le message est refusé. ``code`` dit pourquoi : un en-tête illisible, une signature
    fausse, ou une heure d'émission hors de la tolérance."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _key(secret: str | bytes) -> bytes:
    return secret.encode() if isinstance(secret, str) else secret


def sign(secret: str | bytes, timestamp: int, body: bytes) -> str:
    """La signature ``v1`` du corps émis à ``timestamp``, sous ``secret``."""
    signed = str(timestamp).encode("ascii") + b"." + body
    return hmac.new(_key(secret), signed, hashlib.sha256).hexdigest()


def signature_header(secrets: Sequence[str | bytes], timestamp: int, body: bytes) -> str:
    """La valeur de l'en-tête : l'heure d'émission, puis une signature par secret."""
    if not secrets or len(secrets) > MAX_SIGNATURES or timestamp < 0:
        raise ValueError("De un à huit secrets, et une heure d'émission positive.")
    signatures = ",".join(f"{SCHEME}={sign(secret, timestamp, body)}" for secret in secrets)
    return f"t={timestamp},{signatures}"


def _parsed(header: str | None) -> tuple[int, list[str]]:
    """L'heure d'émission et les signatures ``v1`` de l'en-tête ; les autres clés sont
    ignorées, pour qu'un schéma de signature à venir n'empêche pas de lire celui-ci."""
    if header is None or len(header) > MAX_HEADER_LENGTH:
        raise SignatureError(MALFORMED_HEADER)
    timestamps: list[str] = []
    signatures: list[str] = []
    for part in header.strip().split(","):
        key, equals, value = part.partition("=")
        if not equals or not _KEY.fullmatch(key):
            raise SignatureError(MALFORMED_HEADER)
        if key == "t":
            timestamps.append(value)
        elif key == SCHEME:
            if not _SIGNATURE.fullmatch(value):
                raise SignatureError(MALFORMED_HEADER)
            signatures.append(value)
    if len(timestamps) != 1 or not _TIMESTAMP.fullmatch(timestamps[0]):
        raise SignatureError(MALFORMED_HEADER)
    if not signatures or len(signatures) > MAX_SIGNATURES:
        raise SignatureError(MALFORMED_HEADER)
    return int(timestamps[0]), signatures


def verify(
    header: str | None,
    body: bytes,
    secrets: Sequence[str | bytes],
    *,
    tolerance: int = DEFAULT_TOLERANCE,
    now: float | None = None,
) -> int:
    """Vérifie le message reçu ; rend son heure d'émission, ou lève ``SignatureError``.

    ``header`` est la valeur reçue de l'en-tête ``AnswerLineage-Signature`` ;
    ``body``, le corps tel qu'il est reçu, avant toute lecture du JSON ;
    ``secrets``, le secret partagé, et le précédent pendant une rotation.
    L'heure d'émission doit être à ``tolerance`` secondes au plus de ``now``
    (l'horloge du récepteur par défaut), avant comme après.
    """
    if not secrets or tolerance < 0:
        raise ValueError("Au moins un secret, et une tolérance positive.")
    timestamp, signatures = _parsed(header)
    expected = [sign(secret, timestamp, body) for secret in secrets]
    # Chaque comparaison prend le même temps, juste ou fausse.
    matches = [
        hmac.compare_digest(candidate, signature)
        for candidate in expected
        for signature in signatures
    ]
    if not any(matches):
        raise SignatureError(SIGNATURE_MISMATCH)
    current = time.time() if now is None else now
    if abs(current - timestamp) > tolerance:
        raise SignatureError(OUTSIDE_TOLERANCE)
    return timestamp
