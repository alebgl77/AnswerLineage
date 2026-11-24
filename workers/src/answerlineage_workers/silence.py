"""Contrôle des émetteurs muets.

Un émetteur publie ses relevés de couverture fenêtre par fenêtre, y compris
une fenêtre sans appel : c'est le battement qui prouve qu'il n'est pas muet.
Un émetteur connu dont le dernier relevé date de plus que le seuil (dix
minutes par défaut) est signalé : une ligne de journal qui le nomme, et un
état que lit l'API (``coverage_emitter.silent_since``, muet depuis la
réception de son dernier relevé).

Un épisode de silence se signale une fois : l'état retient le relevé après
lequel le silence a commencé, et un contrôle suivant ne le signale plus. Un
émetteur qui republie cesse d'être muet ; s'il se tait de nouveau, c'est un
nouvel épisode.

Plusieurs workers tournent en même temps. Le contrôle prend un verrou
consultatif de transaction sans l'attendre : un worker qui le trouve pris
passe son tour, et ne se bloque jamais derrière un autre. La condition de la
mise à jour suffirait d'ailleurs à ne signaler un épisode qu'une fois.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger("answerlineage_workers")

DEFAULT_SILENCE: Final = timedelta(minutes=10)
# Clé du verrou consultatif du contrôle, propre à AnswerLineage.
LOCK_KEY: Final = 0x414C_5349_4C45_4E54

_TRY_LOCK = text("SELECT pg_try_advisory_xact_lock(:key)")
_SIGNAL = text(
    """
    UPDATE coverage_emitter
    SET silent_since = last_report_at, signalled_at = :now
    WHERE last_report_at < :limit
      AND silent_since IS DISTINCT FROM last_report_at
    RETURNING emitter, last_report_at
    """
)


@dataclass(frozen=True)
class SilentEmitter:
    """Un émetteur signalé muet : aucun relevé reçu depuis ``since``."""

    emitter: str
    since: datetime


async def check_silent_emitters(
    engine: AsyncEngine, *, now: datetime, threshold: timedelta = DEFAULT_SILENCE
) -> list[SilentEmitter]:
    """Signale les émetteurs muets depuis plus que ``threshold`` à l'heure ``now``.

    Rend les épisodes signalés par ce contrôle-ci, chacun une fois : un épisode
    déjà signalé, ou un contrôle qui trouve un autre en cours, ne rend rien.
    """
    async with engine.begin() as conn:
        if not (await conn.execute(_TRY_LOCK, {"key": LOCK_KEY})).scalar_one():
            return []
        rows = (await conn.execute(_SIGNAL, {"now": now, "limit": now - threshold})).all()
    signalled = sorted(
        (SilentEmitter(row.emitter, row.last_report_at) for row in rows),
        key=lambda item: item.emitter,
    )
    for item in signalled:
        # L'émetteur est un identifiant contrôlé à la réception du relevé : jamais un contenu.
        logger.warning(
            "Émetteur muet : %s, aucun relevé depuis %s.", item.emitter, item.since.isoformat()
        )
    return signalled
