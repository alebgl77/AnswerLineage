"""Reconstruction du graphe depuis les événements bruts.

Le graphe se déduit tout entier des événements que garde ``event_raw`` : quand il
est corrompu, on le vide et on rejoue les événements, sans restaurer la base.

Pas à pas (``rebuild_graph``) :

1. **Une seule à la fois.** La reconstruction prend un verrou consultatif de
   session, sans l'attendre : une seconde reconstruction refuse de démarrer.
2. **Les workers s'écartent.** Elle prend la barrière du graphe (``gate.py``),
   en attendant la fin des lots et des analyses en cours ; les workers passent
   ensuite leur tour jusqu'à ce qu'elle la rende.
3. **Le départ, en une transaction.** Les événements bruts conservés sont remis
   en file dans l'ordre de leur réception, sauf ceux au rebut, qui y restent avec
   leur motif ; ceux qui attendaient déjà dans la file y gardent leur place. La
   reconstruction est inscrite dans ``graph_rebuild``, puis les tables du graphe
   sont vidées, elles seules. Une panne avant la fin de cette transaction ne
   change rien. Après elle, un événement est dans la file ou dans le graphe,
   jamais nulle part : la couverture du lignage dit ce qui manque encore.
4. **La projection, par lots.** La reconstruction projette la file elle-même,
   lot par lot, par la projection des workers (``project_batch``), dans l'ordre
   de la file : les événements remis en file, puis ceux reçus depuis, que
   l'ingestion continue d'y mettre. Un événement que la projection refuse va au
   rebut, comme avec un worker. Quand les workers ont une destination de
   réémission, un événement reçu depuis, ou qui attendait dans la file, y part
   une fois projeté ; un événement remis en file par la reconstruction est
   déjà parti : il ne repart pas.
5. **La fin.** Quand la file a rendu au graphe tout ce qui y était au départ, la
   reconstruction est close, et la barrière rendue.

Le point de reprise est la file : chaque lot projeté la quitte dans sa
transaction. Une reconstruction arrêtée, même brutalement, rend la barrière ;
les workers projettent ce qui reste, et une nouvelle commande reprend la
reconstruction inachevée sans vider le graphe de nouveau. Les analyses
d'incident attendent que la file ait rendu ce qu'elle y a mis.

Ce qu'elle ne rend pas : ce que la conservation a purgé n'est plus dans les
événements bruts. Une ligne du graphe enregistrée avant le premier événement
conservé revient autrement, ou pas du tout : la reconstruction les compte, et
refuse de démarrer sans qu'on accepte de les perdre (``accept_loss``).

Le journal ne dit que des nombres et des heures : aucun contenu.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from answerlineage_api.store.schema import GRAPH, graph_rebuild
from answerlineage_workers.gate import GATE_KEY
from answerlineage_workers.queue import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_MAX_ATTEMPTS,
    Handler,
    checked_batch_size,
    project_batch,
    record_event,
)

logger = logging.getLogger("answerlineage_workers")

# Clé du verrou consultatif d'une reconstruction, propre à AnswerLineage (« ALREBUIL »).
REBUILD_LOCK_KEY: Final = 0x414C_5245_4255_494C
DEFAULT_PROGRESS_EVERY: Final = timedelta(seconds=10)
# Attente quand la file ne rend rien pour l'instant : ce qui reste est remis à plus tard.
IDLE_DELAY: Final = 1.0
# Attente maximale des lectures en cours pour vider le graphe ; au-delà, rien ne change.
LOCK_TIMEOUT: Final = "10s"
RESUME_COMMAND: Final = "answerlineage-worker rebuild --yes"

_TRY_LOCK = text("SELECT pg_try_advisory_lock(:key)")
_HOLD = text("SELECT pg_advisory_lock(:key)")
_LOCK_TIMEOUT = text("SELECT set_config('lock_timeout', :timeout, true)")
_UNFINISHED = text("SELECT * FROM graph_rebuild WHERE finished_at IS NULL ORDER BY id LIMIT 1")
_EVENTS = text(
    "SELECT count(*) AS events, min(recorded_at) AS first, max(recorded_at) AS last FROM event_raw"
)
_QUEUED = text("SELECT count(*) FROM ingest_queue")
_DEAD_LETTERS = text("SELECT count(*) FROM dead_letter")
# Dans l'ordre de réception ; un événement déjà en file y garde sa place et ses essais.
# Un événement remis en file a déjà été projeté, et réémis : il ne repart pas.
_REQUEUE = text(
    """
    INSERT INTO ingest_queue (event_id, recorded_month, reemit)
    SELECT e.event_id, e.recorded_month, false FROM event_raw e
    WHERE NOT EXISTS (SELECT 1 FROM dead_letter d WHERE d.event_id = e.event_id)
    ORDER BY e.recorded_at, e.event_id
    ON CONFLICT (event_id) DO NOTHING
    """
)
_BOUNDARY = text("SELECT coalesce(max(id), 0) FROM ingest_queue")
_LEFT = text("SELECT count(*) FROM ingest_queue WHERE id <= :boundary")
_PENDING = text("SELECT EXISTS (SELECT 1 FROM ingest_queue WHERE id <= :boundary)")
_BURIED = text("SELECT count(*) FROM dead_letter WHERE last_failed_at >= :since")
# Les tables du graphe, et elles seules ; leurs identifiants repartent de un.
_EMPTY_GRAPH = text(f"TRUNCATE {', '.join(table.name for table in GRAPH)} RESTART IDENTITY")
_FINISH = text(
    "UPDATE graph_rebuild SET finished_at = clock_timestamp() WHERE id = :id RETURNING *"
)


class RebuildRefusedError(Exception):
    """La reconstruction ne démarre pas ; rien n'a changé. Le message est un gabarit fixe."""


def _count(value: int, singular: str, plural: str) -> str:
    return f"{value} {singular if value <= 1 else plural}"


def _at(moment: datetime) -> str:
    return moment.isoformat()


@dataclass(frozen=True)
class Assessment:
    """Ce qu'une reconstruction trouverait : l'aperçu, sans rien changer."""

    graph_rows: int
    events: int
    queued: int
    dead_letters: int
    first_received: datetime | None
    last_received: datetime | None
    # Lignes du graphe enregistrées avant le premier événement conservé.
    unrecoverable: int
    # Début d'une reconstruction inachevée, que la prochaine reprendrait.
    unfinished_since: datetime | None = None

    def lines(self) -> list[str]:
        """L'aperçu, en phrases d'un gabarit fixe : des nombres et des heures."""
        found = [f"Le graphe compte {_count(self.graph_rows, 'ligne', 'lignes')}."]
        if self.first_received is None or self.last_received is None:
            found.append("Aucun événement brut conservé : le graphe reconstruit serait vide.")
        else:
            found.append(
                f"{_count(self.events, 'événement brut conservé', 'événements bruts conservés')},"
                f" reçus du {_at(self.first_received)} au {_at(self.last_received)}. La"
                f" reconstruction les remet en file, sauf {self.dead_letters} au rebut, laissés"
                f" tels quels ; {self.queued} y attendent déjà."
            )
        if self.unrecoverable:
            found.append(
                f"{_count(self.unrecoverable, 'ligne', 'lignes')} du graphe, enregistrées avant"
                " le premier événement conservé, ne seraient pas rendues telles quelles :"
                " --accept-loss l'accepte."
            )
        if self.unfinished_since is not None:
            found.append(
                f"La reconstruction commencée le {_at(self.unfinished_since)} n'est pas finie :"
                " --yes la reprend, sans vider le graphe de nouveau."
            )
        found.append("Rien n'est fait sans --yes.")
        return found


@dataclass(frozen=True)
class RebuildReport:
    """Le résultat d'une reconstruction, finie ou interrompue."""

    started_at: datetime
    finished_at: datetime | None
    replayed: int
    dead_letters: int
    # Événements mis au rebut depuis le début de la reconstruction.
    buried: int
    # Événements de la reconstruction encore à projeter.
    remaining: int
    first_received: datetime | None
    last_received: datetime | None
    unrecoverable: int
    resumed: bool = False

    @property
    def finished(self) -> bool:
        return self.finished_at is not None

    @property
    def duration(self) -> timedelta | None:
        return None if self.finished_at is None else self.finished_at - self.started_at

    def lines(self) -> list[str]:
        """Le résultat, en phrases d'un gabarit fixe : des nombres et des heures."""
        if self.duration is None:
            return [
                f"Reconstruction interrompue : {self.remaining} à projeter dans la file. Les"
                f" workers les projettent ; « {RESUME_COMMAND} » reprend la reconstruction."
            ]
        seconds = f"{self.duration.total_seconds():.1f}".replace(".", ",")
        found = [
            f"Graphe reconstruit en {seconds} s :"
            f" {_count(self.replayed, 'événement rejoué', 'événements rejoués')},"
            f" {self.dead_letters} au rebut laissés tels quels,"
            f" {self.buried} mis au rebut pendant la reconstruction."
        ]
        if self.first_received is None or self.last_received is None:
            found.append("Aucun événement brut n'était conservé : le graphe reconstruit est vide.")
        else:
            found.append(
                f"Événements bruts conservés reçus du {_at(self.first_received)}"
                f" au {_at(self.last_received)} : ce que la conservation a purgé avant n'est"
                " pas rendu."
            )
        if self.unrecoverable:
            found.append(
                f"{_count(self.unrecoverable, 'ligne', 'lignes')} du graphe, enregistrées avant"
                " le premier événement conservé, ne sont pas rendues telles quelles."
            )
        return found


async def _scalar(conn: AsyncConnection, statement: Any, **params: Any) -> Any:
    return (await conn.execute(statement, params)).scalar_one()


async def _assess(conn: AsyncConnection) -> Assessment:
    events = (await conn.execute(_EVENTS)).one()
    graph_rows = unrecoverable = 0
    for table in GRAPH:
        older = (
            func.count()
            if events.first is None
            else func.count().filter(table.c.recorded_at < events.first)
        )
        rows, lost = (await conn.execute(select(func.count(), older).select_from(table))).one()
        graph_rows += rows
        unrecoverable += lost
    unfinished = (await conn.execute(_UNFINISHED)).first()
    return Assessment(
        graph_rows=graph_rows,
        events=events.events,
        queued=await _scalar(conn, _QUEUED),
        dead_letters=await _scalar(conn, _DEAD_LETTERS),
        first_received=events.first,
        last_received=events.last,
        unrecoverable=unrecoverable,
        unfinished_since=None if unfinished is None else unfinished.started_at,
    )


async def assess(engine: AsyncEngine) -> Assessment:
    """L'aperçu d'une reconstruction : ce que contiennent le graphe et les événements bruts."""
    async with engine.connect() as conn:
        return await _assess(conn)


async def _start(conn: AsyncConnection, *, accept_loss: bool) -> tuple[Any, bool]:
    """Reprend la reconstruction inachevée, ou en commence une : remise en file, puis graphe vidé.

    Rend sa ligne de ``graph_rebuild``, et vrai pour une reprise.
    """
    async with conn.begin():
        unfinished = (await conn.execute(_UNFINISHED)).first()
        if unfinished is not None:
            logger.info(
                "Reconstruction du graphe commencée le %s reprise : le graphe n'est pas vidé de"
                " nouveau.",
                _at(unfinished.started_at),
            )
            return unfinished, True
        found = await _assess(conn)
        if found.unrecoverable and not accept_loss:
            raise RebuildRefusedError(
                f"{_count(found.unrecoverable, 'ligne', 'lignes')} du graphe, enregistrées avant"
                " le premier événement conservé, ne seraient pas rendues telles quelles : la"
                " reconstruction ne démarre pas sans --accept-loss. Rien n'a changé."
            )
        await conn.execute(_REQUEUE)
        boundary = await _scalar(conn, _BOUNDARY)
        started = (
            await conn.execute(
                graph_rebuild.insert()
                .values(
                    queue_boundary=boundary,
                    replayed=await _scalar(conn, _LEFT, boundary=boundary),
                    dead_letters=found.dead_letters,
                    first_received=found.first_received,
                    last_received=found.last_received,
                    unrecoverable=found.unrecoverable,
                )
                .returning(*graph_rebuild.c)
            )
        ).one()
        # Le graphe en dernier : ses lectures n'attendent que le temps de la validation.
        await conn.execute(_LOCK_TIMEOUT, {"timeout": LOCK_TIMEOUT})
        await conn.execute(_EMPTY_GRAPH)
    logger.info(
        "Reconstruction du graphe commencée : %d événement(s) à rejouer, %d laissé(s) au rebut.",
        started.replayed,
        started.dead_letters,
    )
    return started, False


async def _report(conn: AsyncConnection, rebuild: Any, *, resumed: bool) -> RebuildReport:
    async with conn.begin():
        buried = await _scalar(conn, _BURIED, since=rebuild.started_at)
        remaining = await _scalar(conn, _LEFT, boundary=rebuild.queue_boundary)
    return RebuildReport(
        started_at=rebuild.started_at,
        finished_at=rebuild.finished_at,
        replayed=rebuild.replayed,
        dead_letters=rebuild.dead_letters,
        buried=buried,
        remaining=remaining,
        first_received=rebuild.first_received,
        last_received=rebuild.last_received,
        unrecoverable=rebuild.unrecoverable,
        resumed=resumed,
    )


async def _progress(conn: AsyncConnection, rebuild: Any, projected: int, buried: int) -> None:
    async with conn.begin():
        remaining = await _scalar(conn, _LEFT, boundary=rebuild.queue_boundary)
    logger.info(
        "Reconstruction du graphe : %d événement(s) projeté(s), %d à projeter dans la file,"
        " %d mis au rebut.",
        projected,
        remaining,
        buried,
    )


async def _pending(conn: AsyncConnection, boundary: int) -> bool:
    async with conn.begin():
        pending: bool = await _scalar(conn, _PENDING, boundary=boundary)
    return pending


async def _project(
    conn: AsyncConnection,
    rebuild: Any,
    *,
    resumed: bool,
    stop: asyncio.Event,
    batch_size: int,
    max_attempts: int,
    handler: Handler,
    progress_every: timedelta,
    clock: Callable[[], float],
    reemit: bool,
) -> RebuildReport:
    """Projette la file, lot par lot, jusqu'à ce qu'elle ait rendu ce qui y était au départ."""
    projected = buried = 0
    logged = clock()
    while await _pending(conn, rebuild.queue_boundary):
        if stop.is_set():
            await _progress(conn, rebuild, projected, buried)
            return await _report(conn, rebuild, resumed=resumed)
        result = await project_batch(
            conn,
            batch_size=batch_size,
            handler=handler,
            max_attempts=max_attempts,
            reemit=reemit,
        )
        projected += result.processed
        buried += result.dead_lettered
        if clock() - logged >= progress_every.total_seconds():
            logged = clock()
            await _progress(conn, rebuild, projected, buried)
        if result.empty:
            # Ce qui reste est remis à plus tard : la file le rendra à son heure.
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=IDLE_DELAY)
    async with conn.begin():
        finished = (await conn.execute(_FINISH, {"id": rebuild.id})).one()
    await _progress(conn, rebuild, projected, buried)
    return await _report(conn, finished, resumed=resumed)


async def rebuild_graph(
    engine: AsyncEngine,
    *,
    accept_loss: bool = False,
    stop: asyncio.Event | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    handler: Handler = record_event,
    progress_every: timedelta = DEFAULT_PROGRESS_EVERY,
    clock: Callable[[], float] = time.monotonic,
    reemit: bool = False,
) -> RebuildReport:
    """Vide le graphe et le rejoue depuis les événements bruts, ou reprend une reconstruction.

    Avec ``reemit``, quand les workers ont une destination de réémission, les
    événements qui n'étaient jamais entrés au graphe y partent une fois projetés.

    Lève ``RebuildRefusedError`` quand une autre reconstruction tourne, ou quand des
    lignes du graphe ne seraient pas rendues et que ``accept_loss`` est faux : rien
    n'a alors changé. Rend un résultat interrompu quand ``stop`` est levé avant la
    fin : la file garde ce qui reste à projeter.
    """
    checked_batch_size(batch_size)
    stop = stop or asyncio.Event()
    async with engine.connect() as conn:
        try:
            async with conn.begin():
                if not await _scalar(conn, _TRY_LOCK, key=REBUILD_LOCK_KEY):
                    raise RebuildRefusedError(
                        "Une autre reconstruction du graphe tourne : celle-ci ne démarre pas."
                        " Rien n'a changé."
                    )
            # Les lots et les analyses en cours finissent ; les suivants attendent la fin.
            async with conn.begin():
                await conn.execute(_HOLD, {"key": GATE_KEY})
            rebuild, resumed = await _start(conn, accept_loss=accept_loss)
            return await _project(
                conn,
                rebuild,
                resumed=resumed,
                stop=stop,
                batch_size=batch_size,
                max_attempts=max_attempts,
                handler=handler,
                progress_every=progress_every,
                clock=clock,
                reemit=reemit,
            )
        finally:
            # La session se ferme avec ses verrous : la barrière est rendue aux workers,
            # même quand la connexion ne répond plus.
            await conn.invalidate()
