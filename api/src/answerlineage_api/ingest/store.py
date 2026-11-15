"""Enregistrement des événements bruts.

L'identifiant d'un événement est l'empreinte de sa forme canonique : le même
événement reçu deux fois n'est enregistré qu'une fois.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from answerlineage_api.ingest.models import RunEvent
from answerlineage_api.store import schema


class UnstorableEventError(ValueError):
    """L'événement contient un texte que PostgreSQL ne peut pas conserver."""


@dataclass(frozen=True)
class AcceptedEvent:
    event_id: str
    event: RunEvent
    payload: Mapping[str, Any]


def event_identifier(payload: Mapping[str, Any]) -> str:
    """Empreinte SHA-256 de la forme canonique de l'événement."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if "\\u0000" in canonical:
        raise UnstorableEventError("caractère nul")
    try:
        encoded = canonical.encode("utf-8")
    except UnicodeEncodeError as error:
        raise UnstorableEventError("texte Unicode invalide") from error
    return hashlib.sha256(encoded).hexdigest()


async def store_events(conn: AsyncConnection, events: Sequence[AcceptedEvent]) -> int:
    """Enregistre les événements et renvoie le nombre de ceux qui étaient nouveaux."""
    if not events:
        return 0
    rows = [
        {
            "event_id": accepted.event_id,
            "run_id": accepted.event.run.run_id,
            "event_type": accepted.event.event_type,
            "event_time": accepted.event.event_time,
            "producer": str(accepted.event.producer),
            "payload": accepted.payload,
        }
        for accepted in events
    ]
    statement = (
        insert(schema.event_raw)
        .values(rows)
        .on_conflict_do_nothing(index_elements=[schema.event_raw.c.event_id])
        .returning(schema.event_raw.c.event_id)
    )
    return len((await conn.execute(statement)).all())
