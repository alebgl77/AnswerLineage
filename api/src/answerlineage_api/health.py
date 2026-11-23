"""État de l'installation, tel que la vérifie ``answerlineage doctor``.

La route dit si l'API lit sa base, si le schéma est à jour, si la file
d'ingestion avance, combien d'événements attendent au rebut, combien
d'émetteurs sont muets, combien de partitions manquent, où en est la
réémission vers un consommateur OpenLineage externe, où en sont les
webhooks qui annoncent les incidents, et combien d'incidents proposés par des
tests de qualité attendent un humain. Elle reste petite et
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
from answerlineage_api.store.schema import MAX_OPEN_PROPOSALS

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
_REEMIT_QUEUED = text(
    "SELECT count(*) FROM (SELECT 1 FROM reemission_queue LIMIT :limit) AS queued"
)
# Par l'index partiel des événements en reprise.
_REEMIT_RETRYING = text(
    "SELECT count(*) FROM (SELECT 1 FROM reemission_queue WHERE attempts > 0 LIMIT :limit) AS r"
)
_REEMIT_OLDEST = text(
    """
    SELECT GREATEST(0, floor(EXTRACT(EPOCH FROM now() - enqueued_at)))::bigint
    FROM reemission_queue ORDER BY enqueued_at LIMIT 1
    """
)
# Une ligne au plus ; aucune tant que rien n'a été réémis ni perdu.
_REEMIT_COUNTS = text("SELECT sent, rejected, dropped FROM reemission_stats")
_WEBHOOK_QUEUED = text("SELECT count(*) FROM (SELECT 1 FROM webhook_queue LIMIT :limit) AS queued")
# Par l'index partiel des messages en reprise.
_WEBHOOK_RETRYING = text(
    "SELECT count(*) FROM (SELECT 1 FROM webhook_queue WHERE attempts > 0 LIMIT :limit) AS r"
)
_WEBHOOK_OLDEST = text(
    """
    SELECT GREATEST(0, floor(EXTRACT(EPOCH FROM now() - enqueued_at)))::bigint
    FROM webhook_queue ORDER BY enqueued_at LIMIT 1
    """
)
# Les propositions signalées, 100 au plus, et l'âge de la plus ancienne.
_PROPOSALS = text(
    """
    SELECT count(*) AS open,
           CASE WHEN count(*) > 0 THEN
               GREATEST(0, floor(EXTRACT(EPOCH FROM now() - min(proposed_at))))::bigint
           END AS oldest
    FROM incident_proposal WHERE state = 'signaled'
    """
)
# Deux lignes au plus, une par étape ; aucune tant qu'aucun signal n'a été écarté.
_PROPOSAL_COUNTS = text(
    "SELECT coalesce(sum(refused), 0) AS refused, coalesce(sum(dropped), 0) AS lost"
    " FROM quality_stats"
)
# Une ligne au plus ; aucune tant qu'aucun worker n'a eu de webhook.
_WEBHOOK_STATE = text(
    """
    SELECT coalesce(in_service_until > now(), false) AS in_service, sent, rejected, dropped
    FROM webhook_stats
    """
)


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


class ReemissionHealth(BaseModel):
    """La réémission vers un consommateur OpenLineage externe, que règlent les workers."""

    queued: int = Field(description="Événements projetés qui attendent d'être réémis.")
    retrying: int = Field(
        description=(
            "Parmi eux, ceux dont un envoi a échoué sans que la destination dise les avoir pris :"
            " ils seront renvoyés."
        )
    )
    lower_bound: bool = Field(
        description=(
            f"Vrai au-delà de {COUNT_LIMIT} événements : « queued » et « retrying » sont des"
            " minimums."
        )
    )
    oldest_age_seconds: int | None = Field(
        description=(
            "Depuis combien de secondes le plus ancien attend, à l'horloge de la base ;"
            " nul quand la file est vide."
        )
    )
    sent: int = Field(description="Événements remis à la destination depuis la mise en service.")
    lost: int = Field(
        description=(
            "Événements perdus depuis la mise en service : refusés par la destination,"
            " abandonnés par les bornes de la file ou par la purge."
        )
    )
    rejected: int = Field(description="Parmi les perdus, ceux que la destination a refusés.")


class WebhookHealth(BaseModel):
    """Les webhooks qui annoncent les changements d'état des incidents au récepteur."""

    in_service: bool = Field(
        description=(
            "Vrai quand un worker qui a un webhook est passé depuis moins que la borne d'âge"
            " de sa file : un changement d'état d'incident est alors mis en file."
        )
    )
    queued: int = Field(description="Messages qui attendent d'être envoyés au récepteur.")
    retrying: int = Field(
        description=(
            "Parmi eux, ceux dont un envoi a échoué sans que le récepteur dise les avoir pris :"
            " ils seront renvoyés."
        )
    )
    lower_bound: bool = Field(
        description=(
            f"Vrai au-delà de {COUNT_LIMIT} messages : « queued » et « retrying » sont des"
            " minimums."
        )
    )
    oldest_age_seconds: int | None = Field(
        description=(
            "Depuis combien de secondes le plus ancien attend, à l'horloge de la base ;"
            " nul quand la file est vide."
        )
    )
    sent: int = Field(description="Messages remis au récepteur depuis la mise en service.")
    lost: int = Field(
        description=(
            "Messages perdus depuis la mise en service : refusés par le récepteur, ou"
            " abandonnés par les bornes de la file."
        )
    )
    rejected: int = Field(description="Parmi les perdus, ceux que le récepteur a refusés.")


class ProposalHealth(BaseModel):
    """Les incidents proposés par des tests de qualité en échec, qui attendent un humain."""

    open: int = Field(
        description=(
            f"Propositions signalées, en attente d'une décision ; {MAX_OPEN_PROPOSALS} au plus."
        )
    )
    oldest_age_seconds: int | None = Field(
        description=(
            "Depuis combien de secondes la plus ancienne attend, à l'horloge de la base ;"
            " nul quand aucune n'attend."
        )
    )
    refused: int = Field(
        description=(
            "Signaux d'échec refusés depuis la mise en service, la borne des propositions"
            " ouvertes atteinte."
        )
    )
    lost: int = Field(
        description=(
            "Signaux d'échec perdus depuis la mise en service : au-delà d'une borne, ou faute"
            " d'avoir pu être écrits."
        )
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
    reemission: ReemissionHealth | None = Field(
        description=(
            "La file de réémission et ses compteurs ; nuls tant que le schéma n'est pas à jour."
        )
    )
    webhooks: WebhookHealth | None = Field(
        description=(
            "Les webhooks des incidents, leur file et leurs compteurs ; nuls tant que le schéma"
            " n'est pas à jour."
        )
    )
    proposals: ProposalHealth | None = Field(
        description=(
            "Les incidents proposés par des tests de qualité qui attendent une décision ; nuls"
            " tant que le schéma n'est pas à jour."
        )
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


async def _reemission(reader: Reader) -> ReemissionHealth:
    queued, beyond_queued = await _count(reader, _REEMIT_QUEUED)
    retrying, beyond_retrying = await _count(reader, _REEMIT_RETRYING)
    oldest = (await reader.execute(_REEMIT_OLDEST, {})).scalar_one_or_none()
    counts = (await reader.execute(_REEMIT_COUNTS, {})).first()
    sent, rejected, dropped = (0, 0, 0) if counts is None else tuple(counts)
    return ReemissionHealth(
        queued=queued,
        retrying=retrying,
        lower_bound=beyond_queued or beyond_retrying,
        oldest_age_seconds=None if oldest is None else int(oldest),
        sent=sent,
        lost=rejected + dropped,
        rejected=rejected,
    )


async def _webhooks(reader: Reader) -> WebhookHealth:
    queued, beyond_queued = await _count(reader, _WEBHOOK_QUEUED)
    retrying, beyond_retrying = await _count(reader, _WEBHOOK_RETRYING)
    oldest = (await reader.execute(_WEBHOOK_OLDEST, {})).scalar_one_or_none()
    found = (await reader.execute(_WEBHOOK_STATE, {})).first()
    in_service, sent, rejected, dropped = (False, 0, 0, 0) if found is None else tuple(found)
    return WebhookHealth(
        in_service=in_service,
        queued=queued,
        retrying=retrying,
        lower_bound=beyond_queued or beyond_retrying,
        oldest_age_seconds=None if oldest is None else int(oldest),
        sent=sent,
        lost=rejected + dropped,
        rejected=rejected,
    )


async def _proposals(reader: Reader) -> ProposalHealth:
    found = (await reader.execute(_PROPOSALS, {})).one()
    counts = (await reader.execute(_PROPOSAL_COUNTS, {})).one()
    return ProposalHealth(
        open=found.open,
        oldest_age_seconds=None if found.oldest is None else int(found.oldest),
        refused=int(counts.refused),
        lost=int(counts.lost),
    )


@router.get(
    "/health",
    response_model=Health,
    summary="Donne l'état de l'installation",
    responses={**TIMED_OUT},
)
async def installation_health(request: Request) -> Health:
    """Base, migrations, file d'ingestion, rebut, émetteurs muets, partitions manquantes,
    réémission, webhooks, incidents proposés en attente.

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
                    reemission=None,
                    webhooks=None,
                    proposals=None,
                )
            return Health(
                api_version=version,
                database="available",
                migrations=migrations,
                queue=await _queue(reader),
                dead_letters=await _dead_letters(reader),
                emitters=await _emitters(reader),
                partitions=await _partitions(reader),
                reemission=await _reemission(reader),
                webhooks=await _webhooks(reader),
                proposals=await _proposals(reader),
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
                reemission=None,
                webhooks=None,
                proposals=None,
            )
