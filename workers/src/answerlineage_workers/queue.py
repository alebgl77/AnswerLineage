"""File de traitement tenue dans PostgreSQL.

Un lot est réclamé, traité et retiré de la file dans une seule transaction :

- deux workers ne traitent jamais le même événement : les lignes réclamées par
  l'un sont verrouillées et l'autre les saute (``FOR UPDATE SKIP LOCKED``) ;
- un worker arrêté en plein lot ne perd rien : sa transaction est annulée, les
  lignes redeviennent visibles et les écritures partielles disparaissent ;
- un événement fautif ne bloque pas le lot : il est remis à plus tard, avec un
  délai qui double à chaque essai.

Le graphe reçoit, avec chaque événement, l'heure à laquelle AnswerLineage l'a
reçu : c'est son heure d'enregistrement, que le passage du worker ne change pas.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from answerlineage_api.store.graph import record_run
from answerlineage_workers.partitions import (
    DEFAULT_MAINTENANCE_EVERY,
    DEFAULT_RETENTION,
    Retention,
    maintain_partitions,
)
from answerlineage_workers.projection import project
from answerlineage_workers.silence import DEFAULT_SILENCE, check_silent_emitters

logger = logging.getLogger("answerlineage_workers")

# Traite un événement : sa forme reçue, et l'heure de sa réception.
Handler = Callable[[AsyncConnection, Mapping[str, Any], datetime], Awaitable[None]]

DEFAULT_BATCH_SIZE = 100
# Le contrôle des émetteurs muets revient chaque minute : un silence est
# signalé au plus une minute après avoir passé le seuil.
DEFAULT_SILENCE_CHECK_EVERY = timedelta(minutes=1)
RETRY_BASE_SECONDS = 5.0
RETRY_CAP_SECONDS = 3600.0

_CLAIM = text(
    """
    SELECT q.id, e.payload, e.recorded_at
    FROM ingest_queue AS q
    JOIN event_raw AS e ON e.event_id = q.event_id AND e.recorded_month = q.recorded_month
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


async def record_event(
    conn: AsyncConnection, payload: Mapping[str, Any], received_at: datetime
) -> None:
    """Projette un événement dans le graphe, daté de sa réception."""
    await record_run(conn, replace(project(payload), recorded_at=received_at))


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
                    await handler(conn, row.payload, row.recorded_at)
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


def _now() -> datetime:
    return datetime.now(UTC)


async def run_worker(
    engine: AsyncEngine,
    stop: asyncio.Event,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    idle_delay: float = 0.5,
    error_delay: float = 5.0,
    handler: Handler = record_event,
    silence_threshold: timedelta = DEFAULT_SILENCE,
    silence_every: timedelta = DEFAULT_SILENCE_CHECK_EVERY,
    retention: Retention = DEFAULT_RETENTION,
    maintenance_every: timedelta = DEFAULT_MAINTENANCE_EVERY,
    clock: Callable[[], datetime] = _now,
) -> None:
    """Traite la file jusqu'à ce que ``stop`` soit levé.

    Au démarrage, puis toutes les ``silence_every`` sur l'horloge ``clock``, le
    worker contrôle les émetteurs muets depuis plus que ``silence_threshold``.
    Au démarrage, puis toutes les ``maintenance_every``, il entretient les
    partitions mensuelles selon ``retention`` : création à l'avance, purge,
    contrôle.
    """
    next_check: datetime | None = None
    next_maintenance: datetime | None = None
    while not stop.is_set():
        now = clock()
        if next_check is None or now >= next_check:
            next_check = now + silence_every
            try:
                await check_silent_emitters(engine, now=now, threshold=silence_threshold)
            except Exception as error:
                # Le contrôle reprendra au prochain passage ; la file n'attend pas.
                logger.error("Contrôle des émetteurs muets en échec (%s).", type(error).__name__)
        if next_maintenance is None or now >= next_maintenance:
            next_maintenance = now + maintenance_every
            try:
                await maintain_partitions(engine, now=now, retention=retention)
            except Exception as error:
                # Reprise au prochain passage ; les partitions sont créées des mois à l'avance.
                logger.error("Maintenance des partitions en échec (%s).", type(error).__name__)
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
