"""File de traitement tenue dans PostgreSQL.

Un lot est réclamé, traité et retiré de la file dans une seule transaction :

- deux workers ne traitent jamais le même événement : les lignes réclamées par
  l'un sont verrouillées et l'autre les saute (``FOR UPDATE SKIP LOCKED``) ;
- un worker arrêté en plein lot ne perd rien : sa transaction est annulée, les
  lignes redeviennent visibles et les écritures partielles disparaissent ;
- un événement fautif ne bloque pas le lot : il est remis à plus tard, avec un
  délai qui double à chaque essai ;
- un événement que la projection ne pourra pas intégrer ne tourne pas sans fin :
  il est mis au rebut (``dead_letter``) au premier échec quand l'erreur ne se
  corrigera pas en réessayant, après ``max_attempts`` essais sinon. Le passage
  au rebut et le retrait de la file se font dans la transaction du lot.

Le motif d'un échec est un code d'une liste fermée, tiré de la classe de
l'erreur (``failure_reason``) : jamais le message d'une exception, qui peut
citer l'événement.

Le graphe reçoit, avec chaque événement, l'heure à laquelle AnswerLineage l'a
reçu : c'est son heure d'enregistrement, que le passage du worker ne change pas.

Entre deux lots, la boucle du worker analyse aussi un incident déclaré, quand
une demande attend (``incidents.py``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.exc import DataError, DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from answerlineage_api.identifiers import InvalidIdentifierError
from answerlineage_api.ingest.dead_letters import bury
from answerlineage_api.store.graph import record_run
from answerlineage_api.store.schema import DeadLetterReason
from answerlineage_workers.incidents import DEFAULT_ANALYSIS_TIMEOUT, process_analysis
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
# Dix essais, espacés de 5 s à 21 min : 42 minutes pour qu'une panne passe.
DEFAULT_MAX_ATTEMPTS: Final = 10
MAX_MAX_ATTEMPTS: Final = 100
# Ces motifs ne se corrigent pas en réessayant : l'événement va au rebut au premier échec.
PERMANENT_REASONS: Final[frozenset[DeadLetterReason]] = frozenset(
    {"invalid_identifier", "invalid_event", "constraint_violation", "invalid_value"}
)

_CLAIM = text(
    """
    SELECT q.id, q.event_id, q.attempts, e.payload, e.recorded_at, e.emitter
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
        first_failed_at = COALESCE(first_failed_at, now()),
        available_at = now() + make_interval(secs => LEAST(:cap, :base * power(2, attempts)))
    WHERE id = :id
    """
)


@dataclass(frozen=True)
class BatchResult:
    processed: int = 0
    postponed: int = 0
    dead_lettered: int = 0

    @property
    def empty(self) -> bool:
        return self.processed == 0 and self.postponed == 0 and self.dead_lettered == 0


def failure_reason(error: Exception) -> DeadLetterReason:
    """Le motif d'un échec de projection, d'après la classe de l'erreur.

    Un identifiant refusé, un événement que la projection ne sait pas lire, une
    contrainte de la base violée ou une valeur qu'elle refuse ne se corrigent pas
    en réessayant (``PERMANENT_REASONS``). Une erreur de la base (connexion,
    interblocage, schéma pas encore migré) ou toute autre erreur peut passer.
    """
    if isinstance(error, InvalidIdentifierError):
        return "invalid_identifier"
    if isinstance(error, IntegrityError):
        return "constraint_violation"
    if isinstance(error, DataError):
        return "invalid_value"
    if isinstance(error, DBAPIError):
        return "database_error"
    if isinstance(error, KeyError | TypeError | ValueError):
        return "invalid_event"
    return "projection_error"


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
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> BatchResult:
    """Réclame un lot, le traite et le retire de la file, dans une transaction.

    Un événement en échec est remis à plus tard, ou mis au rebut quand son motif
    ne se corrigera pas en réessayant ou qu'il atteint ``max_attempts`` essais.
    """
    async with engine.begin() as conn:
        rows = (await conn.execute(_CLAIM, {"limit": batch_size})).all()
        done: list[int] = []
        postponed = buried = 0
        for row in rows:
            try:
                # Point de reprise : l'échec d'un événement n'annule pas le lot.
                async with conn.begin_nested():
                    await handler(conn, row.payload, row.recorded_at)
            except Exception as error:
                # Seuls le motif et la classe de l'erreur sont gardés : son message peut
                # citer l'événement. L'identifiant de l'événement est une empreinte.
                reason, kind = failure_reason(error), type(error).__name__
                attempt = row.attempts + 1
                if reason in PERMANENT_REASONS or attempt >= max_attempts:
                    buried += 1
                    await bury(conn, queue_id=row.id, emitter=row.emitter, reason=reason)
                    logger.error(
                        "Événement %s mis au rebut : %s (%s), après %d essai(s).",
                        row.event_id,
                        reason,
                        kind,
                        attempt,
                    )
                    continue
                postponed += 1
                logger.warning(
                    "Événement %s remis à plus tard : %s (%s), essai %d sur %d.",
                    row.event_id,
                    reason,
                    kind,
                    attempt,
                    max_attempts,
                )
                await conn.execute(
                    _POSTPONE,
                    {
                        "id": row.id,
                        "error": kind,
                        "base": RETRY_BASE_SECONDS,
                        "cap": RETRY_CAP_SECONDS,
                    },
                )
            else:
                done.append(row.id)
        if done:
            await conn.execute(_DONE, {"ids": done})
    return BatchResult(processed=len(done), postponed=postponed, dead_lettered=buried)


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
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    silence_threshold: timedelta = DEFAULT_SILENCE,
    silence_every: timedelta = DEFAULT_SILENCE_CHECK_EVERY,
    retention: Retention = DEFAULT_RETENTION,
    maintenance_every: timedelta = DEFAULT_MAINTENANCE_EVERY,
    analysis_timeout: timedelta = DEFAULT_ANALYSIS_TIMEOUT,
    clock: Callable[[], datetime] = _now,
) -> None:
    """Traite la file jusqu'à ce que ``stop`` soit levé.

    Un événement en échec va au rebut après ``max_attempts`` essais, ou au
    premier quand son motif ne se corrigera pas en réessayant. Après chaque lot,
    le worker analyse un incident déclaré si une demande attend, sous le délai
    ``analysis_timeout``.

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
            result = await process_batch(
                engine, batch_size=batch_size, handler=handler, max_attempts=max_attempts
            )
            analysed = await process_analysis(engine, timeout=analysis_timeout)
        except Exception as error:
            # Base injoignable, interblocage : le lot ou la demande est resté dans la file.
            logger.error("Lot non traité (%s), nouvel essai à suivre.", type(error).__name__)
            delay = error_delay
        else:
            if result.empty and analysed is None:
                delay = idle_delay
        if delay:
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                continue
