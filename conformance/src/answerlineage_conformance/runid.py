"""Identifiant de run d'un appel d'outil, refait d'après la dérivation publiée.

Le calcul est celui que ``docs/proxy.md`` décrit à l'octet près : il est écrit
ici une seconde fois, sans rien importer du proxy, comme l'écrirait un émetteur
tiers. Les vecteurs de ``spec/run-id/vectors-v1.json`` tiennent les deux
écritures d'accord.
"""

from __future__ import annotations

import hashlib
import re
from typing import Final
from uuid import UUID

# Étiquette de la dérivation, première version.
LABEL: Final = b"answerlineage/run-id/v1"
# Identifiants d'un contexte de trace W3C : hexadécimal en minuscules, jamais nul.
_TRACE_ID: Final = re.compile(r"(?!0{32})[0-9a-f]{32}")
_SPAN_ID: Final = re.compile(r"(?!0{16})[0-9a-f]{16}")


def derived_from(trace_id: object, span_id: object) -> UUID | None:
    """L'identifiant de run d'un contexte de trace cité, ou ``None`` s'il est mal formé."""
    if (
        isinstance(trace_id, str)
        and isinstance(span_id, str)
        and _TRACE_ID.fullmatch(trace_id)
        and _SPAN_ID.fullmatch(span_id)
    ):
        return derive_run_id(trace_id, span_id)
    return None


def derive_run_id(trace_id: str, span_id: str) -> UUID:
    """UUID version 8 (RFC 9562) pris sur SHA-256(étiquette ‖ trace-id ‖ parent-id)."""
    digest = hashlib.sha256(LABEL + bytes.fromhex(trace_id) + bytes.fromhex(span_id)).digest()
    octets = bytearray(digest[:16])
    octets[6] = (octets[6] & 0x0F) | 0x80
    octets[8] = (octets[8] & 0x3F) | 0x80
    return UUID(bytes=bytes(octets))
