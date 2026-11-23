"""Contexte de trace W3C, et identifiant de run qui en dérive.

L'identifiant de run est une fonction pure du ``trace-id`` et du ``parent-id`` :
tout émetteur qui voit le même contexte, quel que soit son langage, son
processus ou sa machine, émet pour le même run. Le calcul est décrit à l'octet
près dans ``docs/proxy.md`` et vérifié par les vecteurs de
``spec/run-id/vectors-v1.json``.

Une valeur mal formée est une entrée hostile : elle est ignorée comme si elle
manquait, sans être conservée, recopiée ni journalisée.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

TRACEPARENT = "traceparent"
# Étiquette de domaine : elle distingue ce calcul de tout autre SHA-256 des mêmes octets.
RUN_ID_LABEL = b"answerlineage/run-id/v1"
# Au-delà, la valeur n'est pas lue : un traceparent de version 00 compte 55 caractères.
MAX_TRACEPARENT_LENGTH = 512

# Version, trace-id, parent-id, drapeaux, puis, pour une version future, des
# champs qui suivent un tiret (W3C Trace Context, niveau 1, section 3.2).
_TRACEPARENT = re.compile(
    r"(?P<version>[0-9a-f]{2})-(?P<trace>[0-9a-f]{32})-(?P<parent>[0-9a-f]{16})"
    r"-(?P<flags>[0-9a-f]{2})(?P<rest>-.*)?",
    re.DOTALL,
)
_INVALID_VERSION = "ff"
_CURRENT_VERSION = "00"
# Drapeaux d'un contexte créé : « non échantillonné », l'émetteur ne décide pas
# de l'échantillonnage des traces d'autrui.
_NOT_SAMPLED = "00"


@dataclass(frozen=True)
class TraceContext:
    """Un contexte de trace bien formé."""

    trace_id: str
    parent_id: str
    version: str = _CURRENT_VERSION
    flags: str = _NOT_SAMPLED

    def traceparent(self) -> str:
        """Valeur de l'en-tête ``traceparent`` qui porte ce contexte, en version 00."""
        return f"{_CURRENT_VERSION}-{self.trace_id}-{self.parent_id}-{self.flags}"


def parse_traceparent(value: object) -> TraceContext | None:
    """Lit un ``traceparent``. Renvoie ``None`` pour toute valeur mal formée."""
    if not isinstance(value, str) or len(value) > MAX_TRACEPARENT_LENGTH:
        return None
    found = _TRACEPARENT.fullmatch(value)
    if found is None:
        return None
    version, trace_id, parent_id = found["version"], found["trace"], found["parent"]
    if version == _INVALID_VERSION or (version == _CURRENT_VERSION and found["rest"] is not None):
        return None
    if not (_nonzero(trace_id) and _nonzero(parent_id)):
        return None
    return TraceContext(trace_id, parent_id, version, found["flags"])


def derive_run_id(context: TraceContext) -> UUID:
    """UUID version 8 (RFC 9562) pris sur SHA-256(étiquette ‖ trace-id ‖ parent-id).

    La version et les drapeaux du contexte n'entrent pas dans le calcul.
    """
    digest = hashlib.sha256(
        RUN_ID_LABEL + bytes.fromhex(context.trace_id) + bytes.fromhex(context.parent_id)
    ).digest()
    octets = bytearray(digest[:16])
    octets[6] = (octets[6] & 0x0F) | 0x80
    octets[8] = (octets[8] & 0x3F) | 0x80
    return UUID(bytes=bytes(octets))


def new_trace_context(token: Callable[[int], str] = secrets.token_hex) -> TraceContext:
    """Crée un contexte d'identifiants aléatoires, de source cryptographique, jamais nuls."""
    while True:
        trace_id, parent_id = token(16), token(8)
        if _nonzero(trace_id) and _nonzero(parent_id):
            return TraceContext(trace_id, parent_id)


def new_span(
    context: TraceContext, token: Callable[[int], str] = secrets.token_hex
) -> TraceContext:
    """Un span de plus dans la trace de ``context`` : même ``trace-id``, ``parent-id`` tiré.

    Le span d'un appel fait sous un run, l'appel d'outil d'un agent sous la trace
    de l'agent par exemple : son identifiant de run en dérive, distinct de celui du run.
    """
    while True:
        parent_id = token(8)
        if _nonzero(parent_id):
            return TraceContext(context.trace_id, parent_id, context.version, context.flags)


def _nonzero(identifier: str) -> bool:
    return identifier.strip("0") != ""
