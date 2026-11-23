"""Réémission des événements vers un consommateur OpenLineage externe.

Un événement que la projection intègre au graphe entre dans la file de
réémission (``reemission_queue``) dans la transaction qui le projette et le
retire de la file de traitement, quand les workers ont une destination
(``queue.py``). Un événement refusé à l'ingestion n'a jamais été enregistré ;
un événement au rebut n'est pas dans le graphe : ni l'un ni l'autre ne part.
Un événement que la reconstruction du graphe projette de nouveau est déjà parti.

La boucle de réémission tourne à côté de la projection, dans chaque worker qui a
une destination, sans jamais la retarder :

- **un lot à la fois.** Elle réclame un lot (``FOR UPDATE SKIP LOCKED`` : deux
  workers ne réclament jamais le même événement), l'envoie événement par
  événement, puis le retire de la file avec ses compteurs, dans une seule
  transaction. L'envoi d'un lot est borné dans le temps ;
- **au moins une fois.** Un envoi dont l'issue n'est pas sûre (délai dépassé,
  connexion coupée, worker arrêté avant la validation) est refait : le
  protocole d'OpenLineage tolère qu'un même événement arrive deux fois ;
- **ce qui est envoyé** est l'événement tel qu'il a été reçu et accepté, sous sa
  forme canonique (clés triées, sans espace), sans rien y ajouter ;
- **une destination qui refuse un événement** (``400``, ``413``, ``422``) ne
  l'acceptera pas mieux plus tard : il est abandonné, et compté perdu ;
- **une destination en panne** arrête le lot au premier échec ; la reprise
  attend de plus en plus, du double à chaque échec jusqu'à un plafond, chaque
  attente tirée au hasard entre sa moitié et son tout, pour que des workers ne
  reviennent pas ensemble ;
- **une file bornée**, en âge et en nombre : ce qui déborde, les plus anciens
  d'abord, est retiré, compté perdu, et dit au journal.

Le journal ne dit que des nombres, des statuts et des classes d'erreur : ni
l'adresse de la destination, ni son en-tête d'authentification, ni rien d'un
événement ou d'une réponse.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from random import Random
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_workers.destination import Destination, Sender

logger = logging.getLogger("answerlineage_workers")

DEFAULT_MAX_EVENTS: Final = 1_000_000
MAX_MAX_EVENTS: Final = 100_000_000
DEFAULT_MAX_AGE: Final = timedelta(days=7)
MAX_MAX_AGE: Final = timedelta(days=90)
DEFAULT_BATCH_SIZE: Final = 100
MAX_BATCH_SIZE: Final = 1_000
# Envoi d'un lot : passé ce temps, ce qui reste attend le lot suivant. La transaction
# qui tient le lot dure au plus ce temps et le délai d'un envoi.
BATCH_SECONDS: Final = 30.0
# Reprises : d'une seconde, doublées à chaque échec, jusqu'à cinq minutes.
BACKOFF_BASE: Final = 1.0
BACKOFF_CAP: Final = 300.0
# La borne de la file est tenue à ce délai près.
TRIM_EVERY: Final = timedelta(seconds=10)
# Événements retirés par transaction, et transactions par passage, au plus.
TRIM_CHUNK: Final = 10_000
TRIM_CHUNKS: Final = 10

# Le plus ancien d'abord ; un événement purgé n'a plus d'événement brut.
_CLAIM = text(
    """
    SELECT q.id, e.payload
    FROM reemission_queue AS q
    LEFT JOIN event_raw AS e ON e.event_id = q.event_id AND e.recorded_month = q.recorded_month
    ORDER BY q.id
    LIMIT :limit
    FOR UPDATE OF q SKIP LOCKED
    """
)
_SETTLED = text("DELETE FROM reemission_queue WHERE id = ANY(:ids)")
_FAILED = text(
    "UPDATE reemission_queue SET attempts = attempts + 1, last_error = :error WHERE id = :id"
)
# Les compteurs grandissent dans la transaction qui retire les événements de la file.
COUNT = text(
    """
    INSERT INTO reemission_stats (id, sent, rejected, dropped)
    VALUES (true, :sent, :rejected, :dropped)
    ON CONFLICT (id) DO UPDATE SET
        sent = reemission_stats.sent + EXCLUDED.sent,
        rejected = reemission_stats.rejected + EXCLUDED.rejected,
        dropped = reemission_stats.dropped + EXCLUDED.dropped
    """
)
# Un événement en cours d'envoi est verrouillé : la borne le saute, il part ou reste.
_TOO_OLD = text(
    """
    WITH doomed AS (
        SELECT id FROM reemission_queue
        WHERE enqueued_at < now() - make_interval(secs => :max_age)
        ORDER BY enqueued_at
        LIMIT :chunk
        FOR UPDATE SKIP LOCKED
    )
    DELETE FROM reemission_queue AS q USING doomed WHERE q.id = doomed.id
    """
)
# La plus récente des lignes au-delà de la borne ; sans verrou : un décalage verrouillerait
# toutes les lignes qu'il passe.
_BEYOND = text("SELECT id FROM reemission_queue ORDER BY id DESC OFFSET :max_events LIMIT 1")
_TOO_MANY = text(
    """
    WITH doomed AS (
        SELECT id FROM reemission_queue WHERE id <= :beyond
        ORDER BY id
        LIMIT :chunk
        FOR UPDATE SKIP LOCKED
    )
    DELETE FROM reemission_queue AS q USING doomed WHERE q.id = doomed.id
    """
)


@dataclass(frozen=True)
class Reemission:
    """La destination de la réémission et les bornes de sa file."""

    destination: Destination
    max_events: int = DEFAULT_MAX_EVENTS
    max_age: timedelta = DEFAULT_MAX_AGE
    batch_size: int = DEFAULT_BATCH_SIZE

    def __post_init__(self) -> None:
        if not 1 <= self.max_events <= MAX_MAX_EVENTS:
            raise ValueError(f"La file de réémission garde de 1 à {MAX_MAX_EVENTS} événements.")
        if not timedelta(seconds=1) <= self.max_age <= MAX_MAX_AGE:
            raise ValueError(
                f"La file de réémission garde un événement d'une seconde à {MAX_MAX_AGE.days}"
                " jours."
            )
        if not 1 <= self.batch_size <= MAX_BATCH_SIZE:
            raise ValueError(f"Un lot de réémission compte de 1 à {MAX_BATCH_SIZE} événements.")


@dataclass(frozen=True)
class ReemitResult:
    """Ce qu'un lot a fait : événements remis, refusés, abandonnés, et l'échec qui l'a arrêté."""

    sent: int = 0
    rejected: int = 0
    # Événements dont l'événement brut a été purgé avant leur envoi.
    dropped: int = 0
    # Statut ou classe de l'échec qui a arrêté le lot : jamais un message.
    failure: str | None = None

    @property
    def empty(self) -> bool:
        return not (self.sent or self.rejected or self.dropped) and self.failure is None


def body(payload: Mapping[str, Any]) -> bytes:
    """L'événement tel qu'il a été reçu et accepté, sous sa forme canonique : clés triées,
    sans espace. Son empreinte est l'identifiant de l'événement."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


async def reemit_batch(
    engine: AsyncEngine,
    sender: Sender,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    stop: asyncio.Event | None = None,
    budget: float = BATCH_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> ReemitResult:
    """Réclame un lot, l'envoie et le retire de la file, dans une transaction.

    Le lot s'arrête au premier échec : l'événement en cause garde son essai et la
    classe de l'échec, les suivants attendent sans essai compté. Il s'arrête aussi
    à ``stop`` et passé ``budget`` secondes : ce qui n'est pas parti reste en file.
    """
    sent = rejected = dropped = 0
    failure: str | None = None
    refusals: Counter[str] = Counter()
    async with engine.begin() as conn:
        rows = (await conn.execute(_CLAIM, {"limit": batch_size})).all()
        settled: list[int] = []
        ends = clock() + budget
        for row in rows:
            if row.payload is None:
                # La purge a retiré l'événement brut : il n'y a plus rien à envoyer.
                settled.append(row.id)
                dropped += 1
                continue
            if (stop is not None and stop.is_set()) or clock() >= ends:
                break
            delivery = await sender.send(body(row.payload), stop)
            if delivery.verdict == "interrupted":
                break
            if delivery.verdict == "failed":
                failure = delivery.reason
                await conn.execute(_FAILED, {"id": row.id, "error": delivery.reason})
                break
            settled.append(row.id)
            if delivery.verdict == "sent":
                sent += 1
            else:
                rejected += 1
                refusals[delivery.reason] += 1
        if settled:
            await conn.execute(_SETTLED, {"ids": settled})
        if sent or rejected or dropped:
            await conn.execute(COUNT, {"sent": sent, "rejected": rejected, "dropped": dropped})
    for reason, count in sorted(refusals.items()):
        logger.warning(
            "Réémission : %d événement(s) refusé(s) par la destination (%s) : abandonnés, comptés"
            " perdus.",
            count,
            reason,
        )
    if dropped:
        logger.warning(
            "Réémission : %d événement(s) purgé(s) avant leur envoi : comptés perdus.", dropped
        )
    return ReemitResult(sent=sent, rejected=rejected, dropped=dropped, failure=failure)


async def trim(engine: AsyncEngine, settings: Reemission) -> int:
    """Retire de la file ce qui dépasse ses bornes, les plus anciens d'abord ; rend leur nombre.

    Les événements retirés sont comptés perdus dans la transaction qui les retire.
    """
    dropped = 0
    for statement in (_TOO_OLD, _TOO_MANY):
        for _ in range(TRIM_CHUNKS):
            async with engine.begin() as conn:
                params: dict[str, Any] = {"chunk": TRIM_CHUNK}
                if statement is _TOO_OLD:
                    params["max_age"] = settings.max_age.total_seconds()
                else:
                    beyond = (
                        await conn.execute(_BEYOND, {"max_events": settings.max_events})
                    ).scalar_one_or_none()
                    if beyond is None:
                        break
                    params["beyond"] = beyond
                removed = (await conn.execute(statement, params)).rowcount
                if removed:
                    await conn.execute(COUNT, {"sent": 0, "rejected": 0, "dropped": removed})
            dropped += removed
            if removed < TRIM_CHUNK:
                break
    if dropped:
        logger.warning(
            "Réémission : %d événement(s) abandonné(s), les plus anciens, au-delà des bornes de la"
            " file (%d événements, %s) : comptés perdus.",
            dropped,
            settings.max_events,
            duration(settings.max_age),
        )
    return dropped


def duration(age: timedelta) -> str:
    """« 168 h », « 30 min », « 45 s » : une borne d'âge, pour le journal."""
    seconds = int(age.total_seconds())
    if seconds % 3600 == 0:
        return f"{seconds // 3600} h"
    if seconds % 60 == 0:
        return f"{seconds // 60} min"
    return f"{seconds} s"


def retry_delay(failures: int, backoff: tuple[float, float], random: Random) -> float:
    """L'attente avant la reprise : doublée à chaque échec jusqu'au plafond, tirée entre sa
    moitié et son tout."""
    base, cap = backoff
    nominal = min(cap, base * float(2 ** min(failures - 1, 32)))
    return nominal * random.uniform(0.5, 1.0)


async def run_reemission(
    engine: AsyncEngine,
    stop: asyncio.Event,
    settings: Reemission,
    *,
    sender: Sender | None = None,
    idle_delay: float = 1.0,
    error_delay: float = 5.0,
    backoff: tuple[float, float] = (BACKOFF_BASE, BACKOFF_CAP),
    random: Random | None = None,
    trim_every: timedelta = TRIM_EVERY,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Vide la file de réémission vers la destination jusqu'à ce que ``stop`` soit levé.

    Rien n'y arrête la boucle : une erreur de la base ou de la destination est
    journalisée par sa classe, et reprise plus tard.
    """
    sender = sender or Sender(settings.destination)
    random = random or Random()  # noqa: S311 — une gigue, pas un secret.
    failures = 0
    next_trim = clock()
    logger.info(
        "Réémission en marche : destination en %s%s, file bornée à %d événements et %s.",
        settings.destination.scheme,
        " avec en-tête d'authentification" if settings.destination.authorization else "",
        settings.max_events,
        duration(settings.max_age),
    )
    try:
        while not stop.is_set():
            if clock() >= next_trim:
                next_trim = clock() + trim_every.total_seconds()
                try:
                    await trim(engine, settings)
                except Exception as error:
                    logger.error(
                        "Bornes de la file de réémission non tenues (%s), nouvel essai à suivre.",
                        type(error).__name__,
                    )
            delay = 0.0
            try:
                result = await reemit_batch(
                    engine, sender, batch_size=settings.batch_size, stop=stop
                )
            except Exception as error:
                # Base injoignable : le lot est resté dans la file, sans essai compté.
                logger.error(
                    "Lot de réémission non traité (%s), nouvel essai à suivre.",
                    type(error).__name__,
                )
                delay = error_delay
            else:
                if result.failure is not None:
                    failures += 1
                    delay = retry_delay(failures, backoff, random)
                    if failures == 1:
                        logger.warning(
                            "Réémission en échec (%s) : les événements attendent dans la file ;"
                            " reprise dans %.1f s, puis de plus en plus espacée.",
                            result.failure,
                            delay,
                        )
                else:
                    if failures:
                        logger.info(
                            "Réémission rétablie après %d échec(s) : la destination accepte de"
                            " nouveau les événements.",
                            failures,
                        )
                        failures = 0
                    if result.empty:
                        delay = idle_delay
            if delay:
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=delay)
    finally:
        sender.close()
        logger.info("Réémission arrêtée.")
