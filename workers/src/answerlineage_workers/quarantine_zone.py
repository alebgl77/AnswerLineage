"""La zone de chaque quarantaine active, calculée puis rafraîchie par les workers.

La zone d'une quarantaine, ce sont les vues et les tables dérivées de son
périmètre que le graphe connaît (``answerlineage_api.lineage.zone``). L'API la
diffuse aux proxys avec le périmètre ; les workers la calculent et la gardent à
côté de la quarantaine (``incident_quarantine_zone``,
``incident_quarantine_derived``), par des noms, jamais par les identifiants du
graphe, qu'une reconstruction changerait.

- **Dès qu'une quarantaine active n'en a pas**, puis toutes les cinq minutes
  (``DEFAULT_ZONE_EVERY``) : une vue que le graphe apprend entre dans la zone au
  calcul suivant. Une boucle à part la tient, à côté de la projection, qu'elle
  ne retarde pas ; elle regarde toutes les dix secondes ce qui est à calculer.
- **Le graphe tel qu'il était connu.** Le calcul lit le graphe tel qu'il était
  connu à son heure, posée par la base ; c'est l'heure de la zone.
- **Inchangée, elle garde son heure.** Un calcul qui retrouve la même zone ne
  change que l'heure du dernier calcul : la version de la diffusion, et donc son
  ETag, ne bougent pas sans raison.
- **Une quarantaine, un worker.** Le calcul prend un verrou consultatif de
  transaction propre à la quarantaine, sans l'attendre : un autre worker passe
  à la suivante. Après le verrou, il relit que la quarantaine est encore active
  et à calculer : un calcul qui vient de finir ailleurs n'est pas refait.
- **Sur un graphe entier.** Aucun calcul pendant une reconstruction du graphe,
  ni tant que la file n'a pas rendu au graphe ce qu'elle y a remis
  (``gate.py``) : sur un graphe partiel, la zone perdrait des jeux.
- **Un échec garde la zone précédente.** Un calcul au-delà de son délai, ou en
  erreur, n'écrit rien : la zone d'avant reste diffusée. Seule la classe de
  l'erreur est journalisée ; la quarantaine est reprise au passage suivant.
- **Une quarantaine levée n'est plus rafraîchie** : sa dernière zone reste avec
  elle, pour mémoire, et n'est plus diffusée.

Le journal ne nomme qu'un incident, par l'identifiant que l'API a tiré, et des
nombres : aucun nom de jeu de données.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal, Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from answerlineage_api.incidents.models import MAX_DATASETS
from answerlineage_api.lineage.reading import Reader
from answerlineage_api.lineage.zone import Zone, derive
from answerlineage_api.store.schema import MAX_ACTIVE_QUARANTINES
from answerlineage_workers import gate

logger = logging.getLogger("answerlineage_workers")

# Une zone se rafraîchit toutes les cinq minutes ; la boucle regarde toutes les dix
# secondes si une quarantaine en attend une, et un calcul dispose de trente secondes.
DEFAULT_ZONE_EVERY: Final = timedelta(minutes=5)
DEFAULT_ZONE_CHECK_EVERY: Final = timedelta(seconds=10)
DEFAULT_ZONE_TIMEOUT: Final = timedelta(seconds=30)
# Espace des verrous consultatifs des zones : « ZONE » en ASCII. La forme à deux entiers
# ne rencontre pas les verrous à une clé des workers ; le second est la quarantaine.
LOCK_SPACE: Final = 0x5A4F4E45
_LOCK_KEYS: Final = 2**31

Outcome = Literal["computed", "unchanged", "failed"]


class Deriver(Protocol):
    """Calcule la zone d'un périmètre sur le graphe connu à ``known_at``."""

    def __call__(
        self, reader: Reader, perimeter: Sequence[tuple[str, str]], *, known_at: datetime | None
    ) -> Awaitable[Zone]: ...


_NO_STATEMENT_TIMEOUT = text("SET LOCAL statement_timeout TO DEFAULT")
_TRY_LOCK = text("SELECT pg_try_advisory_xact_lock(:space, :key)")

# Les quarantaines à calculer : sans zone d'abord, puis la zone la plus ancienne. Rien
# tant qu'une reconstruction n'a pas rendu au graphe ce qu'elle a remis en file.
_DUE = f"""
    FROM incident_quarantine q
    LEFT JOIN incident_quarantine_zone z ON z.quarantine_id = q.id
    WHERE q.state = 'active'
      AND (z.quarantine_id IS NULL OR z.checked_at <= now() - make_interval(secs => :every))
      AND NOT {gate.REBUILD_PENDING}
"""
_CANDIDATES = text(
    f"""
    SELECT q.id {_DUE} AND q.id <> ALL(CAST(:skip AS bigint[]))
    ORDER BY z.checked_at NULLS FIRST, q.id
    LIMIT :limit
    """
)
# Relu sous le verrou : un calcul qui vient de finir ailleurs n'est pas refait. L'heure du
# graphe lu est prise ici, après que l'activation est visible : elle la suit.
_CLAIM = text(
    f"""
    SELECT q.incident_id, clock_timestamp() AS known_at {_DUE} AND q.id = :id
    """
)
_PERIMETER = text(
    """
    SELECT namespace, name FROM incident_dataset WHERE incident_id = :incident_id
    ORDER BY namespace COLLATE "C", name COLLATE "C"
    LIMIT :limit
    """
)
_STORED = text(
    """
    SELECT z.truncated, d.namespace, d.name, d.depth
    FROM incident_quarantine_zone z
    LEFT JOIN incident_quarantine_derived d ON d.quarantine_id = z.quarantine_id
    WHERE z.quarantine_id = :id
    LIMIT :limit
    """
)
# L'heure du dernier calcul suit celle de la zone, même si l'horloge a reculé.
_CHECKED = text(
    """
    UPDATE incident_quarantine_zone SET checked_at = GREATEST(:known_at, computed_at)
    WHERE quarantine_id = :id
    """
)
_FORGET = text("DELETE FROM incident_quarantine_derived WHERE quarantine_id = :id")
_KEEP = text(
    """
    INSERT INTO incident_quarantine_zone
        (quarantine_id, computed_at, checked_at, truncated, derived)
    VALUES (:id, :known_at, :known_at, :truncated, :derived)
    ON CONFLICT (quarantine_id) DO UPDATE SET
        computed_at = EXCLUDED.computed_at,
        checked_at = EXCLUDED.checked_at,
        truncated = EXCLUDED.truncated,
        derived = EXCLUDED.derived
    """
)
_KEEP_DERIVED = text(
    """
    INSERT INTO incident_quarantine_derived (quarantine_id, namespace, name, depth)
    SELECT :id, namespace, name, depth
    FROM unnest(CAST(:namespaces AS text[]), CAST(:names AS text[]), CAST(:depths AS integer[]))
        AS derived(namespace, name, depth)
    """
)


@dataclass(frozen=True)
class Processed:
    """Une quarantaine dont la zone a été calculée, et ce qu'il en est advenu."""

    quarantine_id: int
    incident_id: UUID
    outcome: Outcome
    derived: int = 0
    truncated: bool = False


async def process_zone(
    engine: AsyncEngine,
    *,
    every: timedelta = DEFAULT_ZONE_EVERY,
    timeout: timedelta = DEFAULT_ZONE_TIMEOUT,
    skip: Sequence[int] = (),
    derive_zone: Deriver = derive,
) -> Processed | None:
    """Calcule la zone d'une quarantaine active qui en attend une, dans une transaction.

    Rend ``None`` quand aucune n'attend, hors de ``skip`` et de celles qu'un autre
    worker calcule, ou qu'une reconstruction du graphe tourne ou n'a pas encore rendu
    au graphe ce qu'elle a remis en file.
    """
    async with engine.begin() as conn:
        if not await gate.share(conn):
            return None
        params = {"every": every.total_seconds()}
        candidates = await conn.execute(
            _CANDIDATES, {**params, "skip": list(skip), "limit": MAX_ACTIVE_QUARANTINES}
        )
        for (quarantine_id,) in candidates.all():
            locked = await conn.execute(
                _TRY_LOCK, {"space": LOCK_SPACE, "key": quarantine_id % _LOCK_KEYS}
            )
            if not locked.scalar_one():
                continue
            claimed = (await conn.execute(_CLAIM, {**params, "id": quarantine_id})).first()
            if claimed is None:
                continue
            return await _compute(
                conn, quarantine_id, claimed.incident_id, claimed.known_at, timeout, derive_zone
            )
    return None


async def _compute(
    conn: AsyncConnection,
    quarantine_id: int,
    incident_id: UUID,
    known_at: datetime,
    timeout: timedelta,
    derive_zone: Deriver,
) -> Processed:
    try:
        # Point de reprise : un échec n'écrit rien, la zone précédente reste.
        async with conn.begin_nested():
            reader = Reader(conn, deadline=time.monotonic() + timeout.total_seconds())
            perimeter = await reader.fetch(
                _PERIMETER, {"incident_id": incident_id}, limit=MAX_DATASETS
            )
            zone = await derive_zone(
                reader,
                [(row.namespace, row.name) for row in perimeter.rows],
                known_at=known_at,
            )
            changed = await _keep(reader, quarantine_id, zone, known_at)
    except Exception as error:
        # L'identifiant est tiré par l'API ; seule la classe de l'erreur est gardée.
        logger.warning(
            "Zone de la quarantaine de l'incident %s non calculée (%s) : la zone précédente"
            " est gardée.",
            incident_id,
            type(error).__name__,
        )
        return Processed(quarantine_id, incident_id, "failed")
    # Le délai posé pour les lectures du calcul ne vaut pas pour sa validation.
    await conn.execute(_NO_STATEMENT_TIMEOUT)
    if changed:
        logger.info(
            "Zone de la quarantaine de l'incident %s : %d jeu(x) dérivé(s)%s.",
            incident_id,
            len(zone.derived),
            ", tronquée" if zone.truncated else "",
        )
    return Processed(
        quarantine_id,
        incident_id,
        "computed" if changed else "unchanged",
        len(zone.derived),
        zone.truncated,
    )


async def _keep(reader: Reader, quarantine_id: int, zone: Zone, known_at: datetime) -> bool:
    """Garde la zone ; rend faux quand elle était déjà celle-ci, dont seule l'heure du
    dernier calcul change alors."""
    stored = await reader.fetch(_STORED, {"id": quarantine_id}, limit=len(zone.derived) + 1)
    if stored.rows and not stored.truncated:
        derived = {
            (row.namespace, row.name, row.depth) for row in stored.rows if row.name is not None
        }
        same = {(item.namespace, item.name, item.depth) for item in zone.derived}
        if stored.rows[0].truncated == zone.truncated and derived == same:
            await reader.execute(_CHECKED, {"id": quarantine_id, "known_at": known_at})
            return False
    as_of = {"id": quarantine_id, "known_at": known_at}
    await reader.execute(_FORGET, as_of)
    await reader.execute(
        _KEEP, {**as_of, "truncated": zone.truncated, "derived": len(zone.derived)}
    )
    if zone.derived:
        await reader.execute(
            _KEEP_DERIVED,
            {
                "id": quarantine_id,
                "namespaces": [item.namespace for item in zone.derived],
                "names": [item.name for item in zone.derived],
                "depths": [item.depth for item in zone.derived],
            },
        )
    return True


async def run_zones(
    engine: AsyncEngine,
    stop: asyncio.Event,
    *,
    every: timedelta = DEFAULT_ZONE_EVERY,
    check_every: timedelta = DEFAULT_ZONE_CHECK_EVERY,
    timeout: timedelta = DEFAULT_ZONE_TIMEOUT,
    error_delay: float = 5.0,
    derive_zone: Deriver = derive,
) -> None:
    """Calcule les zones à calculer, puis attend ``check_every``, jusqu'à ``stop``.

    Une quarantaine dont le calcul échoue attend le passage suivant ; une erreur de
    la base est journalisée par sa classe, et le passage repris après ``error_delay``.
    """
    while not stop.is_set():
        delay = check_every.total_seconds()
        failed: list[int] = []
        try:
            while not stop.is_set():
                done = await process_zone(
                    engine, every=every, timeout=timeout, skip=failed, derive_zone=derive_zone
                )
                if done is None:
                    break
                if done.outcome == "failed":
                    failed.append(done.quarantine_id)
        except Exception as error:
            logger.error(
                "Zones des quarantaines non calculées (%s), nouvel essai à suivre.",
                type(error).__name__,
            )
            delay = error_delay
        await _until_stopped(stop, delay)


async def _until_stopped(stop: asyncio.Event, delay: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=delay)
    except TimeoutError:
        return
