"""Lecture bornée du corps d'une requête d'ingestion, avant toute analyse JSON.

Trois bornes empêchent une entrée piégée d'épuiser le service :

- la taille : le corps est lu morceau par morceau et la lecture s'arrête dès
  que la taille maximale est franchie ; un ``Content-Length`` qui l'annonce
  d'emblée est refusé sans rien lire ;
- la compression : un corps ``gzip`` est déployé au fil de la lecture, jamais
  au-delà de la même taille maximale ; une bombe est refusée sans être
  déployée. La borne vaut pour les octets reçus comme pour les octets déployés ;
- la profondeur : l'imbrication est mesurée par un parcours linéaire des
  octets, avant que l'analyseur JSON, récursif, ne s'y engage.
"""

from __future__ import annotations

import re
import zlib
from array import array
from collections.abc import AsyncIterator, Mapping
from itertools import accumulate
from typing import Final
from zlib import decompressobj

from starlette.datastructures import Headers

ENV_MAX_BODY_BYTES: Final = "ANSWERLINEAGE_MAX_BODY_BYTES"

# Un lot de 1 000 appels d'outil réels pèse de 1 à 3 Mio : 16 Mio laissent
# plus de 16 Kio par événement d'un lot plein.
DEFAULT_MAX_BODY_BYTES: Final = 16 * 1024 * 1024

# Profondeur maximale, objets et tableaux confondus. Un lot de RunEvents
# atteint 7 niveaux pour les colonnes d'une facette ``schema`` et 12 pour la
# facette standard ``columnLineage`` ; 64 laissent encore 28 niveaux de
# structures imbriquées dans un schéma, au-delà des 15 qu'admet BigQuery.
MAX_DEPTH: Final = 64

BODY_TOO_LARGE: Final = "body_too_large"
INVALID_GZIP: Final = "invalid_gzip"
UNSUPPORTED_ENCODING: Final = "unsupported_content_encoding"

# En-tête gzip, déploiement et contrôle de la somme : zlib le lit seul.
_GZIP_WBITS: Final = 16 + zlib.MAX_WBITS
_SETTING = re.compile(r"[0-9]{1,15}")
_LENGTH = re.compile(r"[0-9]{1,18}")

# Une chaîne JSON, échappements compris. Les quantificateurs possessifs et la
# fin de document admise à la place du guillemet fermant font que toute
# tentative aboutit sans retour en arrière : le parcours reste linéaire, même
# sur des chaînes jamais fermées.
_STRING = re.compile(rb'"(?:[^"\\]++|\\.)*+(?:"|\\?\Z)', re.DOTALL)
_NOT_A_BRACKET = bytes(sorted(set(range(256)) - set(b"[]{}")))
# Un crochet ouvrant vaut +1, un fermant -1 (0xff lu comme octet signé).
_STEPS = bytes.maketrans(b"[{]}", b"\x01\x01\xff\xff")


class BodyRefusedError(Exception):
    """Le corps est refusé avant d'être lu en entier : un statut et un code stable."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def max_body_bytes_from_settings(value: int | None, environment: Mapping[str, str]) -> int:
    """Taille maximale du corps : celle de l'appelant, sinon celle de l'environnement."""
    if value is None:
        text = environment.get(ENV_MAX_BODY_BYTES, "").strip()
        if not text:
            return DEFAULT_MAX_BODY_BYTES
        value = int(text) if _SETTING.fullmatch(text) else 0
    if value < 1:
        raise ValueError(
            f"La taille maximale du corps est un nombre d'octets positif ({ENV_MAX_BODY_BYTES})."
        )
    return value


def _gzip(headers: Headers) -> bool:
    """Vrai pour un corps gzip ; tout autre encodage, ou un encodage empilé, est refusé."""
    encodings = headers.getlist("content-encoding")
    if not encodings:
        return False
    if len(encodings) == 1 and encodings[0].strip().lower() == "gzip":
        return True
    raise BodyRefusedError(415, UNSUPPORTED_ENCODING)


def _announced_length(headers: Headers) -> int | None:
    lengths = headers.getlist("content-length")
    if len(lengths) == 1 and _LENGTH.fullmatch(lengths[0].strip()):
        return int(lengths[0])
    return None


class _Inflater:
    """Déploiement gzip incrémental, qui ne produit jamais plus d'un octet au-delà de la borne.

    Plusieurs membres gzip qui se suivent forment un seul corps, comme le veut
    la RFC 1952.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._member = decompressobj(_GZIP_WBITS)
        self._parts: list[bytes] = []
        self._size = 0

    def _keep(self, data: bytes) -> None:
        self._size += len(data)
        if self._size > self._limit:
            raise BodyRefusedError(413, BODY_TOO_LARGE)
        self._parts.append(data)

    def _room(self) -> int:
        return self._limit - self._size + 1

    def feed(self, data: bytes) -> None:
        try:
            while data:
                if self._member.eof:
                    self._member = decompressobj(_GZIP_WBITS)
                self._keep(self._member.decompress(data, self._room()))
                data = self._member.unconsumed_tail or self._member.unused_data
        except zlib.error as error:
            raise BodyRefusedError(400, INVALID_GZIP) from error

    def finish(self) -> bytes:
        """Le corps déployé ; un flux tronqué est refusé.

        zlib ne retient de sortie que lorsque la place offerte est pleine, et une
        place pleine est un dépassement, déjà refusé : tout a donc été produit.
        """
        if not self._member.eof:
            raise BodyRefusedError(400, INVALID_GZIP)
        return b"".join(self._parts)


async def read_body(chunks: AsyncIterator[bytes], headers: Headers, limit: int) -> bytes:
    """Le corps de la requête, déployé s'il est compressé, d'au plus ``limit`` octets.

    Lève ``BodyRefusedError`` dès qu'une borne est franchie : la suite n'est pas lue.
    """
    compressed = _gzip(headers)
    announced = _announced_length(headers)
    if announced is not None and announced > limit:
        raise BodyRefusedError(413, BODY_TOO_LARGE)
    inflater = _Inflater(limit) if compressed else None
    parts: list[bytes] = []
    received = 0
    async for chunk in chunks:
        received += len(chunk)
        if received > limit:
            raise BodyRefusedError(413, BODY_TOO_LARGE)
        if inflater is not None:
            inflater.feed(chunk)
        else:
            parts.append(chunk)
    return inflater.finish() if inflater is not None else b"".join(parts)


def nesting_depth(document: bytes) -> int:
    """Profondeur d'imbrication d'un document JSON, objets et tableaux confondus.

    Un parcours linéaire des octets, sans analyse : les chaînes, échappements
    compris, sont écartées, puis chaque crochet ou accolade ouvre ou ferme un
    niveau. Le document doit être de l'UTF-8, où aucun octet d'un caractère
    multi-octets ne se confond avec un crochet ou un guillemet.
    """
    steps = array("b", _STRING.sub(b"", document).translate(_STEPS, _NOT_A_BRACKET))
    return max(0, max(accumulate(steps), default=0))
