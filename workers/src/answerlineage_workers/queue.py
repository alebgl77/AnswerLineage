"""File de traitement tenue dans PostgreSQL.

Un lot est réclamé, traité et retiré de la file dans une seule transaction :

- deux workers ne traitent jamais le même événement : les lignes réclamées par
  l'un sont verrouillées et l'autre les saute (``FOR UPDATE SKIP LOCKED``) ;
- un worker arrêté en plein lot ne perd rien : sa transaction est annulée, les
  lignes redeviennent visibles et les écritures partielles disparaissent ;
- un événement fautif ne bloque pas le lot : il est remis à plus tard, avec un
  délai qui double à chaque essai.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from answerlineage_api.store.graph import record_run
from answerlineage_workers.projection import project

logger = logging.getLogger("answerlineage_workers")

Handler = Callable[[AsyncConnection, Mapping[str, Any]], Awaitable[None]]

DEFAULT_BATCH_SIZE = 100
RETRY_BASE_SECONDS = 5.0
RETRY_CAP_SECONDS = 3600.0

_CLAIM = text(
    """
    SELECT q.id, e.payload
    FROM ingest_queue AS q
    JOIN event_raw AS e ON e.event_id = q.event_id
    WHERE q.available_at <= now()
    ORDER BY q.id
    LIMIT :limit
    FOR UPDATE OF q SKIP LOCKED
    """
)
_DONE = text("DELETE FROM ingest_queue WHERE id = ANY(:ids)")
_POSTPONE = text(
    """
    UPDATE ingest_queue
    SET attempts = attempts + 1,
        last_error = :error,
        available_at = now() + make_interval(secs => LEAST(:cap, :base * power(2, attempts)))
    WHERE id = :id
    """
)


@dataclass(frozen=True)
class BatchResult:
    processed: int = 0
    postponed: int = 0

    @property
    def empty(self) -> bool:
        return self.processed == 0 and self.postponed == 0


async def record_event(conn: AsyncConnection, payload: Mapping[str, Any]) -> None:
    """Projette un événement dans le graphe."""
    await record_run(conn, project(payload))


async def process_batch(
    engine: AsyncEngine,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    handler: Handler = record_event,
) -> BatchResult:
    """Réclame un lot, le traite et le retire de la file, dans une transaction."""
    async with engine.begin() as conn:
        rows = (await conn.execute(_CLAIM, {"limit": batch_size})).all()
        done: list[int] = []
        postponed = 0
        for row in rows:
            try:
                # Point de reprise : l'échec d'un événement n'annule pas le lot.
                async with conn.begin_nested():
                    await handler(conn, row.payload)
            except Exception as error:
                postponed += 1
                # Seule la classe de l'erreur est gardée : son message peut citer l'événement.
                logger.warning("Événement remis à plus tard (%s).", type(error).__name__)
                await conn.execute(
                    _POSTPONE,
                    {
                        "id": row.id,
                        "error": type(error).__name__,
                        "base": RETRY_BASE_SECONDS,
                        "cap": RETRY_CAP_SECONDS,
                    },
                )
            else:
                done.append(row.id)
        if done:
            await conn.execute(_DONE, {"ids": done})
    return BatchResult(processed=len(done), postponed=postponed)


async def run_worker(
    engine: AsyncEngine,
    stop: asyncio.Event,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    idle_delay: float = 0.5,
    error_delay: float = 5.0,
    handler: Handler = record_event,
) -> None:
    """Traite la file jusqu'à ce que ``stop`` soit levé."""
    while not stop.is_set():
        delay = 0.0
        try:
            result = await process_batch(engine, batch_size=batch_size, handler=handler)
        except Exception as error:
            # Base injoignable, interblocage : le lot est resté dans la file.
            logger.error("Lot non traité (%s), nouvel essai à suivre.", type(error).__name__)
            delay = error_delay
        else:
            if result.empty:
                delay = idle_delay
        if delay:
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                continue
