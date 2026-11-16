"""État de l'installation, tel que la vérifie ``answerlineage doctor``.

La route dit si l'API lit sa base, si le schéma est à jour, si la file
d'ingestion avance, combien d'événements attendent au rebut, combien
d'émetteurs sont muets et combien de partitions manquent. Elle reste petite et
bornée : quelques requêtes indexées, des décomptes plafonnés, sous le délai
maximal des lectures. Elle ne dit aucun détail interne : ni révision du
schéma, ni adresse de la base, ni message d'erreur, seulement des états d'une
liste fermée et des nombres.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import AsyncExitStack
from functools import cache
from typing import Literal

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import TextClause, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.lineage.reading import TIMED_OUT, Reader
from answerlineage_api.store.migrate import MIGRATIONS

router = APIRouter(prefix="/api/v1", tags=["installation"])

# Au-delà, un décompte est un minimum : la route lit au plus une ligne de plus.
COUNT_LIMIT = 10_000

DatabaseState = Literal["available", "unavailable"]
MigrationState = Literal["current", "behind", "unknown_revision", "not_migrated"]

_ALEMBIC_TABLE = text("SELECT to_regclass('alembic_version') IS NOT NULL")
# Une histoire linéaire n'a qu'une ligne ; quelques-unes suffisent à juger une base qui en a plus.
_REVISIONS = text("SELECT version_num FROM alembic_version LIMIT 8")
_PENDING = text("SELECT count(*) FROM (SELECT 1 FROM ingest_queue LIMIT :limit) AS pending")
# Le plus ancien en attente, par l'index de la clé ; son âge à l'horloge de la base.
_OLDEST = text(
    """
    SELECT GREATEST(0, floor(EXTRACT(EPOCH FROM now() - enqueued_at)))::bigint
    FROM ingest_queue ORDER BY id LIMIT 1
    """
)
# Le rebut et son plus ancien, par l'index des heures d'échec, en une seule
# lecture : le décompte et l'âge s'accordent, même pendant un rejeu. Les lignes
# lues sont les plus anciennes : la borne ne change pas l'âge. GREATEST ignore
# un âge nul (NULL) : un rebut vide n'a pas d'âge, il ne vaut pas zéro seconde.
_BURIED = text(
    """
    SELECT count(*) AS events,
           CASE WHEN count(*) > 0 THEN
               GREATEST(0, floor(EXTRACT(EPOCH FROM now() - min(last_failed_at))))::bigint
           END AS oldest
    FROM (SELECT last_failed_at FROM dead_letter ORDER BY last_failed_at LIMIT :limit) AS buried
    """
)
_KNOWN = text("SELECT count(*) FROM (SELECT 1 FROM coverage_emitter LIMIT :limit) AS known")
# Muet tant que le contrôle des workers l'a signalé et qu'aucun relevé n'est venu depuis.
_MUTED = text(
    """
    SELECT count(*) FROM (
        SELECT 1 FROM coverage_emitter WHERE silent_since = last_report_at LIMIT :limit
    ) AS muted
    """
)
_MISSING = text("SELECT count(*) FROM (SELECT 1 FROM partition_alert LIMIT :limit) AS missing")


class QueueHealth(BaseModel):
    """La file d'ingestion : ce qui attend d'être projeté dans le graphe."""

    pending: int = Field(description="Événements reçus en attente de projection.")
    lower_bound: bool = Field(
        description=f"Vrai au-delà de {COUNT_LIMIT} événements : « pending » est un minimum."
    )
    oldest_age_seconds: int | None = Field(
        description=(
            "Depuis combien de secondes le plus ancien attend, à l'horloge de la base ;"
            " nul quand la file est vide."
        )
    )


class DeadLetterHealth(BaseModel):
    """La file de rebut : les événements que la projection n'a pas pu intégrer au graphe."""

    events: int = Field(description="Événements au rebut, qui attendent d'être rejoués.")
    lower_bound: bool = Field(
        description=f"Vrai au-delà de {COUNT_LIMIT} événements : « events » est un minimum."
    )
    oldest_age_seconds: int | None = Field(
        description=(
            "Depuis combien de secondes le plus ancien est au rebut, à l'horloge de la base ;"
            " nul quand le rebut est vide."
        )
    )


class EmitterHealth(BaseModel):
    """Les émetteurs qui publient des relevés de couverture."""

    known: int = Field(description="Émetteurs qui ont publié au moins un relevé.")
    muted: int = Field(
        description=(
            "Émetteurs signalés muets par le contrôle des workers, sans relevé reçu depuis."
        )
    )
    lower_bound: bool = Field(
        description=f"Vrai au-delà de {COUNT_LIMIT} émetteurs : les décomptes sont des minimums."
    )


class PartitionHealth(BaseModel):
    """Les partitions mensuelles que le contrôle des workers signale manquantes."""

    missing: int = Field(description="Partitions manquantes, par table et par mois.")
    lower_bound: bool = Field(
        description=f"Vrai au-delà de {COUNT_LIMIT} : « missing » est un minimum."
    )


class Health(BaseModel):
    """L'état de l'installation : des états d'une liste fermée et des décomptes bornés."""

    api_version: str = Field(description="Version de l'API.")
    database: DatabaseState = Field(
        description="« available » : l'API lit sa base. « unavailable » : elle ne la joint pas."
    )
    migrations: MigrationState | None = Field(
        description=(
            "« current » : le schéma est à jour. « behind » : des migrations restent à appliquer."
            " « unknown_revision » : la base porte une révision que l'API ne connaît pas."
            " « not_migrated » : aucune migration n'est appliquée. Nul quand la base est"
            " indisponible."
        )
    )
    queue: QueueHealth | None = Field(
        description="La file d'ingestion ; nulle tant que le schéma n'est pas à jour."
    )
    dead_letters: DeadLetterHealth | None = Field(
        description="La file de rebut ; nulle tant que le schéma n'est pas à jour."
    )
    emitters: EmitterHealth | None = Field(
        description="Les émetteurs connus et muets ; nuls tant que le schéma n'est pas à jour."
    )
    partitions: PartitionHealth | None = Field(
        description="Les partitions manquantes ; nulles tant que le schéma n'est pas à jour."
    )


@cache
def _revisions() -> tuple[str | None, frozenset[str]]:
    """La révision de tête des migrations, et toutes celles qu'elles connaissent."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    script = ScriptDirectory.from_config(config)
    known = frozenset(revision.revision for revision in script.walk_revisions())
    return script.get_current_head(), known


async def _migrations(reader: Reader) -> MigrationState:
    if not (await reader.execute(_ALEMBIC_TABLE, {})).scalar_one():
        return "not_migrated"
    applied = set((await reader.execute(_REVISIONS, {})).scalars())
    head, known = _revisions()
    if not applied:
        return "not_migrated"
    if applied == {head}:
        return "current"
    return "behind" if applied <= known else "unknown_revision"


async def _count(reader: Reader, statement: TextClause) -> tuple[int, bool]:
    """Un décompte plafonné, et vrai quand le plafond est dépassé."""
    found = int((await reader.execute(statement, {"limit": COUNT_LIMIT + 1})).scalar_one())
    return min(found, COUNT_LIMIT), found > COUNT_LIMIT


async def _queue(reader: Reader) -> QueueHealth:
    pending, beyond = await _count(reader, _PENDING)
    oldest = (await reader.execute(_OLDEST, {})).scalar_one_or_none()
    return QueueHealth(
        pending=pending,
        lower_bound=beyond,
        oldest_age_seconds=None if oldest is None else int(oldest),
    )


async def _dead_letters(reader: Reader) -> DeadLetterHealth:
    found = (await reader.execute(_BURIED, {"limit": COUNT_LIMIT + 1})).one()
    return DeadLetterHealth(
        events=min(found.events, COUNT_LIMIT),
        lower_bound=found.events > COUNT_LIMIT,
        oldest_age_seconds=None if found.oldest is None else int(found.oldest),
    )


async def _emitters(reader: Reader) -> EmitterHealth:
    known, beyond_known = await _count(reader, _KNOWN)
    muted, beyond_muted = await _count(reader, _MUTED)
    return EmitterHealth(known=known, muted=muted, lower_bound=beyond_known or beyond_muted)


async def _partitions(reader: Reader) -> PartitionHealth:
    missing, beyond = await _count(reader, _MISSING)
    return PartitionHealth(missing=missing, lower_bound=beyond)


@router.get(
    "/health",
    response_model=Health,
    summary="Donne l'état de l'installation",
    responses={**TIMED_OUT},
)
async def installation_health(request: Request) -> Health:
    """Base, migrations, file d'ingestion, rebut, émetteurs muets, partitions manquantes.

    Une base injoignable n'est pas une erreur de la route : elle répond
    « unavailable », sans dire pourquoi.
    """
    version = request.app.version
    engine: AsyncEngine = request.app.state.engine
    seconds = request.app.state.read_timeout_ms / 1000
    deadline = time.monotonic() + seconds
    async with AsyncExitStack() as stack:
        try:
            # La connexion est bornée : une base qui ne répond pas ne retient pas la route.
            async with asyncio.timeout(seconds):
                conn = await stack.enter_async_context(engine.connect())
            reader = Reader(conn, deadline=deadline)
            migrations = await _migrations(reader)
            if migrations != "current":
                return Health(
                    api_version=version,
                    database="available",
                    migrations=migrations,
                    queue=None,
                    dead_letters=None,
                    emitters=None,
                    partitions=None,
                )
            return Health(
                api_version=version,
                database="available",
                migrations=migrations,
                queue=await _queue(reader),
                dead_letters=await _dead_letters(reader),
                emitters=await _emitters(reader),
                partitions=await _partitions(reader),
            )
        except (TimeoutError, DBAPIError, OSError):
            return Health(
                api_version=version,
                database="unavailable",
                migrations=None,
                queue=None,
                dead_letters=None,
                emitters=None,
                partitions=None,
            )
