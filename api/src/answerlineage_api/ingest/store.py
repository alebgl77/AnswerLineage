"""Enregistrement des événements bruts.

L'identifiant d'un événement est l'empreinte de sa forme canonique : le même
événement reçu deux fois n'est enregistré qu'une fois, et chaque réception en
trop est comptée comme un doublon.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from answerlineage_api.ingest.duplicates import count_duplicates, emitter_of
from answerlineage_api.ingest.models import TOOL_CALL_FACET, RunEvent
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
    """Enregistre les événements et renvoie le nombre de ceux qui étaient nouveaux.

    Un événement présent plusieurs fois dans la liste n'est enregistré qu'une
    fois. Les réceptions qui ne sont pas nouvelles (n - 1 pour un événement
    inconnu présent n fois, n pour un événement déjà connu) sont comptées dans
    la même transaction, par émetteur. Sans doublon, le compteur n'est pas touché.

    Chaque événement nouveau entre dans la file de traitement dans la même
    transaction : il sera projeté dans le graphe, une fois et une seule.
    """
    if not events:
        return 0
    distinct = {accepted.event_id: accepted for accepted in events}
    rows = [
        {
            "event_id": accepted.event_id,
            "run_id": accepted.event.run.run_id,
            "event_type": accepted.event.event_type,
            "event_time": accepted.event.event_time,
            "producer": str(accepted.event.producer),
            "payload": accepted.payload,
            "emitter": emitter_of(accepted.event),
            "tool_call": TOOL_CALL_FACET in accepted.event.run.facets,
        }
        for accepted in distinct.values()
    ]
    statement = (
        insert(schema.event_raw)
        .values(rows)
        .on_conflict_do_nothing(index_elements=[schema.event_raw.c.event_id])
        .returning(schema.event_raw.c.event_id)
    )
    stored = {row.event_id for row in (await conn.execute(statement)).all()}
    if stored:
        await conn.execute(
            # Dans l'ordre de réception : la file se traite dans cet ordre.
            insert(schema.ingest_queue).values(
                [{"event_id": event_id} for event_id in distinct if event_id in stored]
            )
        )
    # La première réception d'un événement nouveau n'est pas un doublon.
    unseen = set(stored)
    repeated: list[str] = []
    for accepted in events:
        if accepted.event_id in unseen:
            unseen.discard(accepted.event_id)
        else:
            repeated.append(emitter_of(accepted.event))
    await count_duplicates(conn, repeated)
    return len(stored)
