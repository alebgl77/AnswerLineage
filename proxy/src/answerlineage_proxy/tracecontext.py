"""Contexte de trace W3C d'un appel d'outil relayé, et identifiant de run qui en dérive.

Le contexte se lit dans ``params._meta.traceparent`` du corps, sinon dans
l'en-tête HTTP ``traceparent``. Faute de l'un et de l'autre, le proxy en crée un.

La lecture d'un ``traceparent`` et la dérivation de l'identifiant de run sont
celles de tout émetteur (``answerlineage_emitter.tracecontext``) : tout
émetteur qui voit le même contexte émet pour le même run. Le calcul est décrit
à l'octet près dans ``docs/proxy.md`` et vérifié par les vecteurs de
``spec/run-id/vectors-v1.json``.

Une valeur mal formée est une entrée hostile : elle est ignorée comme si elle
manquait, sans être conservée, recopiée ni journalisée.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from answerlineage_emitter.tracecontext import (
    MAX_TRACEPARENT_LENGTH,
    RUN_ID_LABEL,
    TRACEPARENT,
    TraceContext,
    derive_run_id,
    new_trace_context,
    parse_traceparent,
)

__all__ = [
    "MAX_TRACEPARENT_LENGTH",
    "RUN_ID_LABEL",
    "TRACEPARENT",
    "CallTrace",
    "TraceContext",
    "TraceOrigin",
    "derive_run_id",
    "new_trace_context",
    "parse_traceparent",
    "resolve_trace",
]

TraceOrigin = Literal["meta", "header", "proxy"]


@dataclass(frozen=True)
class CallTrace:
    """Contexte de trace d'un appel d'outil, et d'où il vient."""

    context: TraceContext
    origin: TraceOrigin
    # En-tête traceparent que le proxy ajoute à la requête relayée, s'il en ajoute un.
    forwarded: str | None = None

    @property
    def run_id(self) -> UUID:
        return derive_run_id(self.context)


def resolve_trace(meta: TraceContext | None, headers: Sequence[str]) -> CallTrace:
    """Choisit le contexte d'un appel d'outil.

    ``meta`` est le contexte lu dans ``params._meta.traceparent``, ``headers``
    les valeurs des en-têtes ``traceparent`` reçus. ``_meta`` l'emporte : il
    désigne l'appel d'outil lui-même et survit aux reprises HTTP. Un en-tête en
    double ou mal formé n'est pas lu ; il n'est ni remplacé ni doublé, et le
    contexte créé ne sert alors qu'à l'identifiant de run.
    """
    if meta is not None:
        return CallTrace(meta, "meta")
    if len(headers) == 1:
        received = parse_traceparent(headers[0])
        if received is not None:
            return CallTrace(received, "header")
    created = new_trace_context()
    return CallTrace(created, "proxy", None if headers else created.traceparent())
