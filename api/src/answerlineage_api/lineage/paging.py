"""Pagination des réponses, des plus récentes aux plus anciennes, sans perte ni doublon.

Plusieurs réponses peuvent porter la même heure. Elles se départagent par
l'identifiant stable de la réponse : son run, puis le condensé de son
empreinte (``answer_key``). Le curseur rendu par l'API désigne la dernière
réponse d'une page ; la page suivante commence juste après elle.

Le curseur est opaque pour l'appelant. Il s'écrit ``<heure>.<identifiant>`` :
l'heure de la réponse en microsecondes depuis le 1er janvier de l'an 1, puis
l'identifiant de la réponse. Il ne recopie rien de l'émetteur.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

CURSOR_PATTERN = r"^[0-9]{1,18}\.[0-9a-f]{48}$"

_ORIGIN = datetime(1, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)
_DIGEST_BYTES = 8

# Condensé de l'empreinte d'une réponse ``a``, calculé par la base : le même
# que celui de ``answer_key``. Un ``bytea`` se compare octet par octet, comme
# l'écriture hexadécimale de l'identifiant.
ANSWER_DIGEST = f"substr(sha256(convert_to(a.fingerprint, 'UTF8')), 1, {_DIGEST_BYTES})"
ANSWER_ORDER = f"a.event_time DESC, a.run_id DESC, {ANSWER_DIGEST} DESC"
# Les réponses qui suivent le curseur. La première condition seule suit l'index
# des heures ; la seconde départage les réponses de la même heure.
AFTER_CURSOR = f"""a.event_time <= :cursor_time
      AND (a.event_time < :cursor_time
           OR (a.run_id, {ANSWER_DIGEST}) < (:cursor_run, :cursor_digest))"""


def digest(fingerprint: str) -> bytes:
    """Condensé de l'empreinte, qui départage les réponses d'un même run."""
    return hashlib.sha256(fingerprint.encode()).digest()[:_DIGEST_BYTES]


class CursorError(ValueError):
    """Le curseur a la forme attendue, mais ne désigne aucun instant représentable."""


@dataclass(frozen=True)
class Cursor:
    """Position dans la liste des réponses : juste après celle-ci."""

    produced_at: datetime
    run_id: UUID
    digest: bytes

    def encode(self) -> str:
        micros = (self.produced_at - _ORIGIN) // _MICROSECOND
        return f"{micros}.{self.run_id.hex}{self.digest.hex()}"

    @classmethod
    def decode(cls, value: str) -> Cursor:
        """Lit un curseur dont la forme est déjà validée (``CURSOR_PATTERN``)."""
        micros, identifier = value.split(".")
        try:
            produced_at = _ORIGIN + int(micros) * _MICROSECOND
        except OverflowError:
            raise CursorError from None
        return cls(produced_at, UUID(hex=identifier[:32]), bytes.fromhex(identifier[32:]))

    @classmethod
    def after(cls, row: Any) -> Cursor:
        """Le curseur qui suit cette réponse (colonnes ``event_time``, ``run_id``, ``digest``)."""
        return cls(row.event_time, row.run_id, bytes(row.digest))

    def parameters(self) -> dict[str, Any]:
        return {
            "cursor_time": self.produced_at,
            "cursor_run": self.run_id,
            "cursor_digest": self.digest,
        }
