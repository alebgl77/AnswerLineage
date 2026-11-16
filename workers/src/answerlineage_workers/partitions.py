"""Maintenance des partitions mensuelles : création à l'avance, purge, contrôle.

Les événements bruts, les relevés de couverture et le compteur de doublons sont
partitionnés par mois de réception (``answerlineage_api.store.partitions``). La
boucle du worker les entretient au démarrage, puis toutes les heures :

- **création à l'avance** : le mois courant et les ``ahead`` suivants (deux par
  défaut) ont leur partition, pour qu'aucune écriture n'échoue faute de partition ;
- **purge** : une partition entièrement plus ancienne que la conservation (13 mois
  par défaut) est supprimée. Jamais le mois courant : la conservation compte au
  moins deux mois, et la limite est prise sur la plus prudente de deux horloges,
  celle du worker et celle de la base ;
- **contrôle** : une partition qui manque pour un mois qui commence dans les sept
  jours est signalée, une fois par épisode, par une ligne de journal qui nomme la
  table et le mois, et par une ligne de ``partition_alert`` tant qu'elle manque.

Plusieurs workers entretiennent en même temps sans double travail ni attente :
la maintenance prend un verrou consultatif de transaction sans l'attendre, et un
worker qui le trouve pris passe son tour. Créer ou supprimer une partition
verrouille sa table un instant ; la maintenance n'attend pas ce verrou plus d'une
seconde, pour ne jamais faire attendre l'ingestion derrière elle : elle renonce,
le journalise, et réessaie au passage suivant.
"""

from __future__ import annotations

import calendar
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Final

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from answerlineage_api.store.partitions import (
    DEFAULT_AHEAD_MONTHS,
    PARTITIONED,
    create_partition,
    month_of,
    partition_name,
    shift,
)

logger = logging.getLogger("answerlineage_workers")

DEFAULT_RETENTION_MONTHS: Final = 13
MAX_RETENTION_MONTHS: Final = 1200
MIN_AHEAD_MONTHS: Final = DEFAULT_AHEAD_MONTHS
MAX_AHEAD_MONTHS: Final = 24
DEFAULT_MAINTENANCE_EVERY: Final = timedelta(hours=1)
# Une partition qui manque pour un mois commençant dans ce délai est signalée.
ALERT_HORIZON: Final = timedelta(days=7)
# Clé du verrou consultatif de la maintenance, propre à AnswerLineage.
LOCK_KEY: Final = 0x414C_5041_5254_4954
# Attente maximale du verrou d'une table pour créer ou supprimer une partition.
LOCK_TIMEOUT: Final = "1s"

_TRY_LOCK = text("SELECT pg_try_advisory_xact_lock(:key)")
_LOCK_TIMEOUT = text("SELECT set_config('lock_timeout', :timeout, true)")
_DATABASE_NOW = text("SELECT now()")
_PARTITIONS = text(
    """
    SELECT p.relname AS parent, c.relname AS name, pg_get_expr(c.relpartbound, c.oid) AS bound
    FROM pg_inherits i
    JOIN pg_class c ON c.oid = i.inhrelid
    JOIN pg_class p ON p.oid = i.inhparent
    WHERE p.relnamespace = CAST(current_schema() AS regnamespace)
      AND p.relname = ANY(:tables)
    """
)
_BOUND = re.compile(
    r"FOR VALUES FROM \((?:MINVALUE|'(\d{4}-\d{2}-\d{2})')\)"
    r" TO \((?:MAXVALUE|'(\d{4}-\d{2}-\d{2})')\)"
)
_UNQUEUE = text(
    """
    DELETE FROM ingest_queue
    WHERE (CAST(:lower AS date) IS NULL OR recorded_month >= :lower) AND recorded_month < :upper
    """
)
# L'état du contrôle : les épisodes en cours. Un épisode clos s'efface.
_CLOSE = text(
    """
    DELETE FROM partition_alert
    WHERE (table_name, month) NOT IN (
        SELECT * FROM unnest(CAST(:tables AS text[]), CAST(:months AS date[]))
    )
    """
)
_SIGNAL = text(
    """
    INSERT INTO partition_alert (table_name, month, signalled_at)
    SELECT missing.table_name, missing.month, :now
    FROM unnest(CAST(:tables AS text[]), CAST(:months AS date[])) AS missing(table_name, month)
    ON CONFLICT (table_name, month) DO NOTHING
    RETURNING table_name, month
    """
)


@dataclass(frozen=True)
class Retention:
    """Durée de conservation et avance de création, en mois."""

    months: int = DEFAULT_RETENTION_MONTHS
    ahead: int = DEFAULT_AHEAD_MONTHS

    def __post_init__(self) -> None:
        if not MIN_AHEAD_MONTHS <= self.ahead <= MAX_AHEAD_MONTHS:
            raise ValueError(
                f"L'avance va de {MIN_AHEAD_MONTHS} à {MAX_AHEAD_MONTHS} mois"
                " après le mois courant."
            )
        if not 1 <= self.months <= MAX_RETENTION_MONTHS:
            raise ValueError(f"La conservation va de 1 à {MAX_RETENTION_MONTHS} mois.")
        if self.months < self.ahead:
            raise ValueError(
                f"La conservation ({self.months} mois) ne peut pas être plus courte"
                f" que l'avance ({self.ahead} mois)."
            )

    def cutoff(self, now: datetime) -> datetime:
        """L'instant ``months`` mois avant ``now`` : ce qui le précède sort de la conservation."""
        utc = now.astimezone(UTC)
        year, month = divmod(utc.year * 12 + utc.month - 1 - self.months, 12)
        day = min(utc.day, calendar.monthrange(year, month + 1)[1])
        return utc.replace(year=year, month=month + 1, day=day)


DEFAULT_RETENTION: Final = Retention()


@dataclass(frozen=True)
class MissingPartition:
    """Une partition qui manque : sa table et son mois."""

    table: str
    month: date


@dataclass(frozen=True)
class MaintenanceReport:
    """Ce qu'une maintenance a fait : partitions créées, supprimées, et celles qui manquent."""

    skipped: bool = False
    created: list[tuple[str, str]] = field(default_factory=list)
    dropped: list[tuple[str, str]] = field(default_factory=list)
    missing: list[MissingPartition] = field(default_factory=list)


@dataclass(frozen=True)
class _Partition:
    table: str
    name: str
    # Bornes du mois : nulles pour MINVALUE et MAXVALUE.
    lower: date | None
    upper: date | None

    def covers(self, month: date) -> bool:
        return (self.lower is None or self.lower <= month) and (
            self.upper is None or shift(month, 1) <= self.upper
        )


async def _partitions(conn: AsyncConnection) -> list[_Partition]:
    """Les partitions des tables partitionnées, d'après le catalogue de la base.

    Une partition par défaut, ou de bornes que la maintenance ne lit pas, ne
    couvre aucun mois et n'est jamais supprimée.
    """
    found = []
    for row in await conn.execute(_PARTITIONS, {"tables": list(PARTITIONED)}):
        bounds = _BOUND.fullmatch(row.bound or "")
        if bounds is None:
            continue
        lower, upper = (
            None if value is None else date.fromisoformat(value) for value in bounds.groups()
        )
        found.append(_Partition(row.parent, row.name, lower, upper))
    return found


def _covered(partitions: Iterable[_Partition], table: str, month: date) -> bool:
    return any(partition.table == table and partition.covers(month) for partition in partitions)


def _months(first: date, last: date) -> list[date]:
    months = []
    while first <= last:
        months.append(first)
        first = shift(first, 1)
    return months


def _failure(error: DBAPIError) -> str:
    """La classe de l'erreur de la base, jamais son message."""
    return type(error.orig).__name__ if error.orig is not None else type(error).__name__


async def _create_ahead(
    conn: AsyncConnection, clocks: Sequence[datetime], ahead: int
) -> list[tuple[str, str]]:
    existing = await _partitions(conn)
    months = sorted(
        {shift(month_of(clock), count) for clock in clocks for count in range(ahead + 1)}
    )
    created = []
    for table in PARTITIONED:
        for month in months:
            if _covered(existing, table, month):
                continue
            try:
                async with conn.begin_nested():
                    await conn.execute(text(create_partition(table, month)))
            except DBAPIError as error:
                # La table est occupée, ou la partition gênée : nouvel essai au prochain passage.
                logger.warning(
                    "Partition %s non créée (%s).", partition_name(table, month), _failure(error)
                )
                break
            logger.info("Partition créée : %s.", partition_name(table, month))
            created.append((table, f"{month:%Y_%m}"))
    return created


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time(), tzinfo=UTC)


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


async def _purge(
    conn: AsyncConnection, clocks: Sequence[datetime], retention: Retention
) -> list[tuple[str, str]]:
    # La limite la plus prudente : la conservation selon chaque horloge, et
    # jamais au-delà du début du mois courant selon l'une ou l'autre.
    limit = min(
        *(retention.cutoff(clock) for clock in clocks),
        *(_midnight(month_of(clock)) for clock in clocks),
    )
    dropped = []
    for partition in sorted(await _partitions(conn), key=lambda item: item.name):
        if partition.upper is None or _midnight(partition.upper) > limit:
            continue
        try:
            async with conn.begin_nested():
                if partition.table == "event_raw":
                    removed = await conn.execute(
                        _UNQUEUE, {"lower": partition.lower, "upper": partition.upper}
                    )
                    if removed.rowcount:
                        logger.warning(
                            "Partition %s purgée : %d événement(s) jamais projeté(s) retiré(s)"
                            " de la file.",
                            partition.name,
                            removed.rowcount,
                        )
                await conn.execute(text(f"DROP TABLE {_quoted(partition.name)}"))
        except DBAPIError as error:
            logger.warning("Partition %s non supprimée (%s).", partition.name, _failure(error))
            continue
        logger.info(
            "Partition supprimée : %s, hors de la conservation de %d mois.",
            partition.name,
            retention.months,
        )
        dropped.append((partition.table, partition.name))
    return dropped


async def _check(
    conn: AsyncConnection, clocks: Sequence[datetime], now: datetime
) -> tuple[list[MissingPartition], list[MissingPartition]]:
    """Les partitions qui manquent d'ici sept jours, et celles que ce contrôle signale."""
    existing = await _partitions(conn)
    months = sorted(
        {
            month
            for clock in clocks
            for month in _months(month_of(clock), month_of(clock + ALERT_HORIZON))
        }
    )
    missing = [
        MissingPartition(table, month)
        for table in PARTITIONED
        for month in months
        if not _covered(existing, table, month)
    ]
    episodes = {
        "tables": [item.table for item in missing],
        "months": [item.month for item in missing],
    }
    await conn.execute(_CLOSE, episodes)
    signalled = await conn.execute(_SIGNAL, {**episodes, "now": now})
    return missing, [MissingPartition(row.table_name, row.month) for row in signalled]


def _alert(signalled: Iterable[MissingPartition]) -> None:
    for item in sorted(signalled, key=lambda found: (found.month, found.table)):
        # La table et le mois sont des noms du produit : jamais un contenu reçu.
        logger.error(
            "Partition manquante : %s, mois %s, à créer avant le %s.",
            item.table,
            f"{item.month:%Y-%m}",
            item.month.isoformat(),
        )


async def _begin(conn: AsyncConnection, now: datetime) -> list[datetime] | None:
    """Prend le verrou de la maintenance sans l'attendre ; rend les deux horloges."""
    if not (await conn.execute(_TRY_LOCK, {"key": LOCK_KEY})).scalar_one():
        return None
    await conn.execute(_LOCK_TIMEOUT, {"timeout": LOCK_TIMEOUT})
    return [now, (await conn.execute(_DATABASE_NOW)).scalar_one()]


async def maintain_partitions(
    engine: AsyncEngine, *, now: datetime, retention: Retention = DEFAULT_RETENTION
) -> MaintenanceReport:
    """Crée les partitions à l'avance, purge celles hors conservation, puis contrôle.

    ``now`` est l'horloge du worker ; la base donne la sienne, et la plus
    prudente des deux l'emporte. Un worker qui trouve la maintenance en cours
    ailleurs passe son tour (``skipped``).
    """
    async with engine.begin() as conn:
        clocks = await _begin(conn, now)
        if clocks is None:
            return MaintenanceReport(skipped=True)
        created = await _create_ahead(conn, clocks, retention.ahead)
        dropped = await _purge(conn, clocks, retention)
        missing, signalled = await _check(conn, clocks, now)
    _alert(signalled)
    return MaintenanceReport(created=created, dropped=dropped, missing=missing)


async def check_partitions(engine: AsyncEngine, *, now: datetime) -> list[MissingPartition]:
    """Signale les partitions qui manquent d'ici sept jours ; rend celles signalées ici.

    Un épisode déjà signalé, ou un contrôle qui trouve la maintenance en cours
    ailleurs, ne rend rien.
    """
    async with engine.begin() as conn:
        clocks = await _begin(conn, now)
        if clocks is None:
            return []
        _, signalled = await _check(conn, clocks, now)
    _alert(signalled)
    return signalled
