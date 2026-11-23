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
  au rebut et le retrait de la file se font dans la transaction du lot ;
- des lots concurrents ne s'interbloquent pas : chacun pose d'avance, dans un
  ordre global, les lignes du graphe qu'il partage avec d'autres
  (``prepare_runs``). Un interblocage ou un échec de sérialisation qui survient
  malgré tout ne dit rien des événements : la transaction est annulée et le lot
  repris, sans compter d'essai, au plus ``MAX_BATCH_RETRIES`` fois.

Le motif d'un échec est un code d'une liste fermée, tiré de la classe de
l'erreur (``failure_reason``) : jamais le message d'une exception, qui peut
citer l'événement.

Le graphe reçoit, avec chaque événement, l'heure à laquelle AnswerLineage l'a
reçu : c'est son heure d'enregistrement, que le passage du worker ne change pas.

Quand les workers ont une destination de réémission (``reemission.py``), un
événement projeté entre dans la file de réémission dans la transaction qui le
projette et le retire de la file : il part une fois intégré au graphe, jamais
avant, et pas s'il va au rebut. Un événement que la reconstruction remet en file
est déjà parti : il n'y entre pas. La boucle de réémission tourne à côté de
celle du worker, sans la retarder.

Entre deux lots, la boucle du worker analyse aussi un incident déclaré, quand
une demande attend (``incidents.py``). Quand le worker a un webhook, une autre
boucle annonce les changements d'état des incidents au récepteur
(``webhooks.py``), sans retarder la projection.

Pendant une reconstruction du graphe (``rebuild.py``), le worker ne réclame ni
lot ni analyse : la reconstruction tient la barrière du graphe et projette la
file seule (``gate.py``). Il les reprend dès qu'elle la rend.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import Row, text
from sqlalchemy.exc import DataError, DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from answerlineage_api.identifiers import InvalidIdentifierError
from answerlineage_api.ingest.dead_letters import bury
from answerlineage_api.store.graph import PreparedRuns, RunRecord, prepare_runs, record_run
from answerlineage_api.store.schema import DeadLetterReason
from answerlineage_workers import gate
from answerlineage_workers.incidents import DEFAULT_ANALYSIS_TIMEOUT, process_analysis
from answerlineage_workers.partitions import (
    DEFAULT_MAINTENANCE_EVERY,
    DEFAULT_RETENTION,
    Retention,
    maintain_partitions,
)
from answerlineage_workers.projection import project
from answerlineage_workers.reemission import Reemission, run_reemission
from answerlineage_workers.silence import DEFAULT_SILENCE, check_silent_emitters
from answerlineage_workers.webhooks import Webhook, run_webhooks

logger = logging.getLogger("answerlineage_workers")

# Traite un événement : sa forme reçue, et l'heure de sa réception.
Handler = Callable[[AsyncConnection, Mapping[str, Any], datetime], Awaitable[None]]

# Ce que le lot en cours a posé d'avance (``prepare_runs``) : la projection de ses
# événements le reprend au lieu de le relire et de le réécrire.
_prepared: ContextVar[PreparedRuns | None] = ContextVar("prepared", default=None)

DEFAULT_BATCH_SIZE = 100
# Un lot est une transaction : au-delà, elle tiendrait trop longtemps les lignes qu'elle écrit.
MAX_BATCH_SIZE: Final = 10_000
# Interblocage et échec de sérialisation : PostgreSQL a annulé la transaction pour en
# laisser passer une autre. Rien n'y tient à l'événement.
TRANSIENT_SQLSTATES: Final = frozenset({"40P01", "40001"})
# Reprises d'un lot après une telle erreur, sans compter d'essai ; au-delà, elle en compte un.
MAX_BATCH_RETRIES: Final = 5
# Attente avant la n-ième reprise : n fois ce délai, le temps que l'autre transaction finisse.
RETRY_PAUSE_SECONDS: Final = 0.05
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
# Retirés de la file, et mis en file de réémission dans l'ordre de leur projection,
# sauf ceux que la reconstruction du graphe y avait remis (déjà partis).
_DONE_AND_REEMITTED = text(
    """
    WITH done AS (
        DELETE FROM ingest_queue WHERE id = ANY(:ids)
        RETURNING id, event_id, recorded_month, reemit
    )
    INSERT INTO reemission_queue (event_id, recorded_month)
    SELECT event_id, recorded_month FROM done WHERE reemit ORDER BY id
    ON CONFLICT (event_id) DO NOTHING
    """
)
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
    # Reprises du lot après un interblocage ou un échec de sérialisation, sans essai compté.
    retried: int = 0

    @property
    def empty(self) -> bool:
        return self.processed == 0 and self.postponed == 0 and self.dead_lettered == 0


def checked_batch_size(batch_size: int) -> int:
    """Le nombre d'événements d'un lot, refusé hors de ses bornes."""
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"Un lot compte de 1 à {MAX_BATCH_SIZE} événements.")
    return batch_size


def _sqlstate(error: BaseException) -> str | None:
    if not isinstance(error, DBAPIError):
        return None
    sqlstate = getattr(error.orig, "sqlstate", None)
    return sqlstate if isinstance(sqlstate, str) else None


def transient(error: BaseException) -> bool:
    """Vrai pour un interblocage ou un échec de sérialisation (``TRANSIENT_SQLSTATES``).

    PostgreSQL a annulé la transaction pour en laisser passer une autre : la même
    transaction, reprise, passe. Une connexion perdue, une base injoignable ou un
    schéma pas encore migré ne sont pas de ces erreurs.
    """
    return _sqlstate(error) in TRANSIENT_SQLSTATES


def failure_reason(error: Exception) -> DeadLetterReason:
    """Le motif d'un échec de projection, d'après la classe de l'erreur.

    Un identifiant refusé, un événement que la projection ne sait pas lire, une
    contrainte de la base violée ou une valeur qu'elle refuse ne se corrigent pas
    en réessayant (``PERMANENT_REASONS``). Une erreur de la base (connexion,
    schéma pas encore migré, interblocage au-delà des reprises du lot) ou toute
    autre erreur peut passer.
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
    """Projette un événement dans le graphe, daté de sa réception.

    Dans un lot, l'écriture reprend ce que le lot a posé d'avance (``_prepared``).
    """
    record = replace(project(payload), recorded_at=received_at)
    await record_run(conn, record, _prepared.get())


async def process_batch(
    engine: AsyncEngine,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    handler: Handler = record_event,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    reemit: bool = False,
) -> BatchResult:
    """Réclame un lot, le traite et le retire de la file, dans une transaction.

    Un événement en échec est remis à plus tard, ou mis au rebut quand son motif
    ne se corrigera pas en réessayant ou qu'il atteint ``max_attempts`` essais.
    Avec ``reemit``, un événement projeté entre dans la file de réémission, dans
    la même transaction. Pendant une reconstruction du graphe, rien n'est
    réclamé (``gate.py``).
    """
    async with engine.connect() as conn:
        return await project_batch(
            conn,
            batch_size=batch_size,
            handler=handler,
            max_attempts=max_attempts,
            reemit=reemit,
        )


async def project_batch(
    conn: AsyncConnection,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    handler: Handler = record_event,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    reemit: bool = False,
) -> BatchResult:
    """``process_batch`` sur cette connexion, dans une transaction qu'elle ouvre.

    Le lot n'est réclamé que si la barrière du graphe est libre, ou tenue par
    cette session : celle de la reconstruction projette, les autres attendent.

    Un interblocage ou un échec de sérialisation annule la transaction entière, et
    le lot est repris dans une nouvelle, sans essai compté, au plus
    ``MAX_BATCH_RETRIES`` fois. Pas dans la même : une transaction qui attendait
    une ligne de ce lot n'est libérée qu'à sa fin, et la reprise l'attendrait à
    son tour. Au-delà, le lot est projeté une dernière fois sans reprise, et
    l'erreur compte un essai à l'événement qu'elle touche, comme toute erreur de
    la base : un événement qui ne passe jamais finit au rebut au lieu de tourner
    sans fin.
    """
    retried = 0
    while True:
        claimed: list[Row[Any]] = []
        try:
            result = await _attempt(
                conn,
                claimed,
                batch_size=batch_size,
                handler=handler,
                max_attempts=max_attempts,
                retrying=retried < MAX_BATCH_RETRIES,
                reemit=reemit,
            )
        except DBAPIError as error:
            if retried >= MAX_BATCH_RETRIES or not transient(error):
                raise
            retried += 1
            # Des nombres et des codes : le message de l'erreur peut citer l'événement.
            logger.warning(
                "Lot de %d événement(s) repris sans compter d'essai : %s (%s), reprise %d sur %d.",
                len(claimed),
                _sqlstate(error),
                type(error).__name__,
                retried,
                MAX_BATCH_RETRIES,
            )
            await asyncio.sleep(RETRY_PAUSE_SECONDS * retried)
            continue
        return replace(result, retried=retried)


async def _attempt(
    conn: AsyncConnection,
    claimed: list[Row[Any]],
    *,
    batch_size: int,
    handler: Handler,
    max_attempts: int,
    retrying: bool,
    reemit: bool,
) -> BatchResult:
    """Réclame un lot, le projette et le retire de la file, dans une transaction."""
    async with conn.begin():
        if not await gate.share(conn):
            return BatchResult()
        rows = (await conn.execute(_CLAIM, {"limit": batch_size})).all()
        claimed.extend(rows)
        failures = await _project(conn, rows, handler, retrying=retrying)
        done: list[int] = []
        postponed = buried = 0
        for row in rows:
            error = failures.get(row.id)
            if error is None:
                done.append(row.id)
                continue
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
        if done:
            await conn.execute(_DONE_AND_REEMITTED if reemit else _DONE, {"ids": done})
    return BatchResult(processed=len(done), postponed=postponed, dead_lettered=buried)


class _RedoError(Exception):
    """Des événements ont échoué après que le lot a posé leurs lignes : il est refait sans eux."""

    def __init__(self, failures: dict[int, Exception]) -> None:
        super().__init__()
        self.failures = failures


async def _project(
    conn: AsyncConnection, rows: Sequence[Row[Any]], handler: Handler, *, retrying: bool
) -> dict[int, Exception]:
    """Projette le lot ; rend ses échecs, par ligne de la file.

    Les lignes que le lot partage avec d'autres sont posées d'avance (``_prepare``),
    sous un point de reprise qui couvre le lot entier : un événement qui échoue
    ensuite y laisserait ses lignes, alors le lot est refait sans lui, et son échec
    gardé. Rien d'un événement en échec ne reste dans le graphe.
    """
    failures: dict[int, Exception] = {}
    while True:
        pending = [row for row in rows if row.id not in failures]
        try:
            async with conn.begin_nested():
                prepared = await _prepare(conn, pending) if retrying else None
                token = _prepared.set(prepared)
                try:
                    failed = await _project_each(conn, pending, handler, retrying=retrying)
                finally:
                    _prepared.reset(token)
                if prepared is not None and failed:
                    raise _RedoError(failed)
        except _RedoError as redo:
            failures.update(redo.failures)
            continue
        return failures | failed


async def _prepare(conn: AsyncConnection, rows: Sequence[Row[Any]]) -> PreparedRuns | None:
    """Pose d'avance, dans un ordre global, les lignes partagées et les runs du lot.

    Rend nul, sans rien laisser, quand la base refuse l'une d'elles : le lot est
    alors projeté événement par événement, et l'échec revient à l'événement fautif.
    """
    records: list[RunRecord] = []
    for row in rows:
        try:
            records.append(replace(project(row.payload), recorded_at=row.recorded_at))
        except Exception:  # noqa: S112 — sa projection échouera de même, et le dira.
            continue
    try:
        async with conn.begin_nested():
            return await prepare_runs(conn, records)
    except Exception as error:
        if transient(error):
            raise
        return None


async def _project_each(
    conn: AsyncConnection, rows: Sequence[Row[Any]], handler: Handler, *, retrying: bool
) -> dict[int, Exception]:
    """Projette chaque événement sous son point de reprise ; rend les échecs.

    Tant que le lot peut être repris, un interblocage ou un échec de sérialisation
    annule la transaction entière (``project_batch``) ; sinon, il compte comme
    l'échec de l'événement qu'il touche.
    """
    failures: dict[int, Exception] = {}
    for row in rows:
        try:
            # Point de reprise : l'échec d'un événement n'annule pas le lot.
            async with conn.begin_nested():
                await handler(conn, row.payload, row.recorded_at)
        except Exception as error:
            if retrying and transient(error):
                raise
            failures[row.id] = error
    return failures


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
    reemission: Reemission | None = None,
    webhook: Webhook | None = None,
) -> None:
    """Traite la file jusqu'à ce que ``stop`` soit levé.

    Un lot compte au plus ``batch_size`` événements, de 1 à ``MAX_BATCH_SIZE`` :
    une valeur hors de ces bornes lève ``ValueError`` avant tout lot. Un événement
    en échec va au rebut après ``max_attempts`` essais, ou au premier quand son
    motif ne se corrigera pas en réessayant. Après chaque lot, le worker analyse un
    incident déclaré si une demande attend, sous le délai ``analysis_timeout``.

    Au démarrage, puis toutes les ``silence_every`` sur l'horloge ``clock``, le
    worker contrôle les émetteurs muets depuis plus que ``silence_threshold``.
    Au démarrage, puis toutes les ``maintenance_every``, il entretient les
    partitions mensuelles selon ``retention`` : création à l'avance, purge,
    contrôle.

    Avec ``reemission``, les événements projetés entrent dans la file de
    réémission, et une seconde boucle, à côté, les envoie à la destination
    (``run_reemission``) : une destination en panne ne retarde pas la projection.
    Avec ``webhook``, une autre boucle annonce les changements d'état des
    incidents au récepteur (``run_webhooks``), sans la retarder non plus.
    """
    checked_batch_size(batch_size)
    beside: list[asyncio.Task[None]] = []
    if reemission is not None:
        beside.append(asyncio.create_task(run_reemission(engine, stop, reemission)))
    if webhook is not None:
        beside.append(asyncio.create_task(run_webhooks(engine, stop, webhook)))
    try:
        await _project_loop(
            engine,
            stop,
            batch_size=batch_size,
            idle_delay=idle_delay,
            error_delay=error_delay,
            handler=handler,
            max_attempts=max_attempts,
            silence_threshold=silence_threshold,
            silence_every=silence_every,
            retention=retention,
            maintenance_every=maintenance_every,
            analysis_timeout=analysis_timeout,
            clock=clock,
            reemit=reemission is not None,
        )
    finally:
        if not stop.is_set():
            for task in beside:
                task.cancel()
        await asyncio.gather(*beside, return_exceptions=True)


async def _project_loop(
    engine: AsyncEngine,
    stop: asyncio.Event,
    *,
    batch_size: int,
    idle_delay: float,
    error_delay: float,
    handler: Handler,
    max_attempts: int,
    silence_threshold: timedelta,
    silence_every: timedelta,
    retention: Retention,
    maintenance_every: timedelta,
    analysis_timeout: timedelta,
    clock: Callable[[], datetime],
    reemit: bool,
) -> None:
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
                engine,
                batch_size=batch_size,
                handler=handler,
                max_attempts=max_attempts,
                reemit=reemit,
            )
            analysed = await process_analysis(engine, timeout=analysis_timeout)
        except Exception as error:
            # Base injoignable, interblocage au-delà des reprises : le lot ou la demande
            # est resté dans la file, sans essai compté.
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
