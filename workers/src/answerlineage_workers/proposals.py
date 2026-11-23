"""Propositions d'incident : des signaux de qualité en échec à ce qu'un humain décide.

La projection relève les signaux d'échec dans la transaction du lot
(``quality.py``). Un pas de la boucle des workers, après les lots et les
analyses, en fait des propositions, dans sa propre transaction :

- **Réclamer.** Au plus ``SIGNAL_BATCH`` signaux en attente, les plus anciens
  d'abord, verrouillés : deux workers ne traitent jamais le même
  (``FOR UPDATE SKIP LOCKED``). Rien pendant une reconstruction du graphe
  (``gate.py``).
- **Sous verrou.** La table des propositions est verrouillée le temps de la
  transaction (``SHARE ROW EXCLUSIVE``, comme l'activation d'une quarantaine) :
  deux workers ne créent pas deux propositions pour un même jeu, et la borne des
  propositions ouvertes se vérifie seule. Les lectures ne l'attendent pas.
- **Une proposition par jeu.** Un signal se rattache à la proposition signalée de
  son jeu, qui s'enrichit : colonnes, classes, plus forte gravité, dernier échec.
  Sinon, si le jeu a vu une proposition confirmée ou écartée depuis moins de
  ``MUTE``, il s'y rattache en sourdine, sans rien proposer. Sinon, une nouvelle
  proposition naît, tant que ``MAX_OPEN_PROPOSALS`` ne sont pas ouvertes ; au-delà,
  le signal est refusé, compté, et l'épisode dit une fois au journal.
- **La fenêtre déduite.** Son début est la dernière réussite connue des
  assertions de la colonne avant le premier échec (``last_pass``), au plus
  ``MAX_LOOKBACK`` avant lui ; à défaut, ou si cette réussite suit déjà l'échec,
  ``LOOKBACK`` avant lui (``lookback``).
- **Rien ne part.** Une proposition n'écrit ni demande d'analyse, ni annonce, ni
  quarantaine : seule sa confirmation par un humain déclare l'incident
  (``answerlineage_api.incidents.proposals``).
- **Fraîcheur.** Un signal reçu ou daté de plus de ``FRESHNESS`` ne propose rien.

Ni le journal ni les compteurs ne citent une valeur reçue : des identifiants tirés
par la base et des nombres.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timedelta
from itertools import groupby
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from answerlineage_api.store.schema import (
    INCIDENT_WINDOW_DAYS,
    MAX_OPEN_PROPOSALS,
    MAX_PROPOSAL_COLUMNS,
    QualitySeverity,
)
from answerlineage_workers import gate
from answerlineage_workers.quality import FRESHNESS, TABLE, count, stronger

logger = logging.getLogger("answerlineage_workers")

SIGNAL_BATCH: Final = 500
LOOKBACK: Final = timedelta(days=7)
MAX_LOOKBACK: Final = timedelta(days=INCIDENT_WINDOW_DAYS)
MUTE: Final = timedelta(hours=24)

_CLAIM = text(
    """
    SELECT run_id, namespace, name, column_name, classes, severity, emitter,
           event_time, recorded_at
    FROM quality_signal
    WHERE state = 'pending'
    ORDER BY event_time, run_id
    LIMIT :limit
    FOR UPDATE SKIP LOCKED
    """
)
# Les décisions et les créations passent l'une après l'autre ; les lectures ne sont pas retenues.
_LOCK = text("LOCK TABLE incident_proposal IN SHARE ROW EXCLUSIVE MODE")
_NOW = text("SELECT now()")
_OPEN = text(
    """
    SELECT id FROM incident_proposal
    WHERE state = 'signaled' AND namespace = :namespace AND name = :name
    """
)
_DECIDED = text(
    """
    SELECT id FROM incident_proposal
    WHERE namespace = :namespace AND name = :name
      AND state IN ('confirmed', 'dismissed') AND decided_at > :since
    ORDER BY decided_at DESC
    LIMIT 1
    """
)
_OPEN_COUNT = text("SELECT count(*) FROM incident_proposal WHERE state = 'signaled'")
_LAST_PASS = text(
    """
    SELECT last_passed_at FROM quality_column
    WHERE namespace = :namespace AND name = :name AND column_name = :column
    """
)
_CREATE = text(
    """
    INSERT INTO incident_proposal (
        namespace, name, whole_table, classes, severity, first_failed_at, last_failed_at,
        window_start, window_basis, emitter
    )
    VALUES (
        :namespace, :name, false, CAST(:classes AS text[]), :severity, :first, :first,
        :window_start, :window_basis, :emitter
    )
    RETURNING id
    """
)
_COLUMNS = text(
    "SELECT column_name FROM incident_proposal_column WHERE proposal_id = :id LIMIT :limit"
)
_ADD_COLUMNS = text(
    """
    INSERT INTO incident_proposal_column (proposal_id, column_name)
    SELECT :id, c FROM unnest(CAST(:columns AS text[])) AS c
    ON CONFLICT DO NOTHING
    """
)
_ENRICH = text(
    """
    UPDATE incident_proposal
    SET whole_table = whole_table OR :whole,
        classes = ARRAY(SELECT DISTINCT c FROM unnest(classes || CAST(:classes AS text[])) AS c
                        ORDER BY c),
        severity = CASE WHEN 'error' IN (severity, :severity) THEN 'error'
                        WHEN 'warn' IN (severity, :severity) THEN 'warn'
                        ELSE 'unspecified' END,
        last_failed_at = GREATEST(last_failed_at, :last),
        signaled_at = GREATEST(clock_timestamp(), signaled_at)
    WHERE id = :id
    """
)
_SETTLE = text(
    """
    UPDATE quality_signal s SET state = :state, proposal_id = :proposal
    FROM unnest(CAST(:runs AS uuid[]), CAST(:columns AS text[])) AS k(run_id, column_name)
    WHERE s.run_id = k.run_id AND s.namespace = :namespace AND s.name = :name
      AND s.column_name = k.column_name
    """
)
_REFUSING_SINCE = text("SELECT refused_since FROM quality_stats WHERE stage = 'proposals'")
_EPISODE = text(
    """
    INSERT INTO quality_stats (stage, refused_since) VALUES ('proposals', :since)
    ON CONFLICT (stage) DO UPDATE SET refused_since = excluded.refused_since
    """
)


async def process_signals(engine: AsyncEngine, *, limit: int = SIGNAL_BATCH) -> int:
    """Réclame des signaux en attente et en fait des propositions, dans une transaction.

    Rend le nombre de signaux traités : zéro quand aucun n'attend, ou qu'une
    reconstruction du graphe tient la barrière.
    """
    async with engine.begin() as conn:
        if not await gate.share(conn):
            return 0
        claimed = (await conn.execute(_CLAIM, {"limit": limit})).all()
        if not claimed:
            return 0
        await conn.execute(_LOCK)
        now: datetime = (await conn.execute(_NOW)).scalar_one()
        tally: Counter[str] = Counter()
        ordered = sorted(claimed, key=lambda row: (row.namespace, row.name, row.event_time))
        for (namespace, name), rows in groupby(ordered, key=lambda row: (row.namespace, row.name)):
            fresh: list[Row[Any]] = []
            stale: list[Row[Any]] = []
            for row in rows:
                recent = min(row.event_time, row.recorded_at) >= now - FRESHNESS
                (fresh if recent else stale).append(row)
            if stale:
                await _set(conn, namespace, name, stale, "stale", None)
                tally["stale"] += len(stale)
            if fresh:
                tally[await _settle(conn, namespace, name, fresh, now)] += len(fresh)
        await _account(conn, tally, now)
    return len(claimed)


async def _settle(
    conn: AsyncConnection, namespace: str, name: str, signals: Sequence[Row[Any]], now: datetime
) -> str:
    """Rattache les signaux d'un jeu : à sa proposition ouverte, en sourdine à une décision
    récente, à une proposition nouvelle, ou les refuse. Rend ce qu'il en est advenu."""
    where = {"namespace": namespace, "name": name}
    opened = (await conn.execute(_OPEN, where)).scalar_one_or_none()
    if opened is not None:
        await _attach(conn, opened, namespace, name, signals)
        return "attached"
    decided = (await conn.execute(_DECIDED, {**where, "since": now - MUTE})).scalar_one_or_none()
    if decided is not None:
        await _set(conn, namespace, name, signals, "muted", decided)
        return "muted"
    if (await conn.execute(_OPEN_COUNT)).scalar_one() >= MAX_OPEN_PROPOSALS:
        await _set(conn, namespace, name, signals, "refused", None)
        return "refused"
    created = await _create(conn, namespace, name, signals[0])
    await _attach(conn, created, namespace, name, signals)
    # L'identifiant est tiré par la base : rien de l'émetteur n'est cité.
    logger.info("Incident proposé par un test de qualité en échec : proposition %s.", created)
    return "created"


async def _create(conn: AsyncConnection, namespace: str, name: str, first: Row[Any]) -> UUID:
    """La proposition, née du plus ancien signal du jeu, et sa fenêtre déduite."""
    failed_at: datetime = first.event_time
    last_pass = (
        await conn.execute(
            _LAST_PASS, {"namespace": namespace, "name": name, "column": first.column_name}
        )
    ).scalar_one_or_none()
    if last_pass is not None and last_pass < failed_at:
        start, basis = max(last_pass, failed_at - MAX_LOOKBACK), "last_pass"
    else:
        start, basis = failed_at - LOOKBACK, "lookback"
    created: UUID = (
        await conn.execute(
            _CREATE,
            {
                "namespace": namespace,
                "name": name,
                "classes": list(first.classes),
                "severity": first.severity,
                "first": failed_at,
                "window_start": start,
                "window_basis": basis,
                "emitter": first.emitter,
            },
        )
    ).scalar_one()
    return created


async def _attach(
    conn: AsyncConnection, proposal: UUID, namespace: str, name: str, signals: Sequence[Row[Any]]
) -> None:
    """Enrichit la proposition de ces signaux et les y rattache.

    Au-delà de ``MAX_PROPOSAL_COLUMNS`` colonnes nommées, la proposition porte sur la
    table entière, comme pour une assertion sur la table.
    """
    named = set(
        (await conn.execute(_COLUMNS, {"id": proposal, "limit": MAX_PROPOSAL_COLUMNS})).scalars()
    )
    columns = sorted({row.column_name for row in signals if row.column_name != TABLE} - named)
    room = max(0, MAX_PROPOSAL_COLUMNS - len(named))
    if columns[:room]:
        await conn.execute(_ADD_COLUMNS, {"id": proposal, "columns": columns[:room]})
    severity: QualitySeverity = "unspecified"
    for row in signals:
        severity = stronger(severity, row.severity)
    await conn.execute(
        _ENRICH,
        {
            "id": proposal,
            "whole": len(columns) > room or any(row.column_name == TABLE for row in signals),
            "classes": sorted({kind for row in signals for kind in row.classes}),
            "severity": severity,
            "last": max(row.event_time for row in signals),
        },
    )
    await _set(conn, namespace, name, signals, "attached", proposal)


async def _set(
    conn: AsyncConnection,
    namespace: str,
    name: str,
    signals: Sequence[Row[Any]],
    state: str,
    proposal: UUID | None,
) -> None:
    await conn.execute(
        _SETTLE,
        {
            "state": state,
            "proposal": proposal,
            "namespace": namespace,
            "name": name,
            "runs": [row.run_id for row in signals],
            "columns": [row.column_name for row in signals],
        },
    )


async def _account(conn: AsyncConnection, tally: Counter[str], now: datetime) -> None:
    """Les compteurs de l'étape, et l'épisode de refus : dit une fois, à son début, et
    clos dès qu'une proposition naît de nouveau."""
    await count(
        conn,
        "proposals",
        **{name: tally[name] for name in ("refused", "stale") if tally[name]},
    )
    since = (await conn.execute(_REFUSING_SINCE)).scalar_one_or_none()
    if tally["refused"] and since is None:
        await conn.execute(_EPISODE, {"since": now})
        logger.error(
            "Borne des propositions d'incident ouvertes atteinte (%d) : %d signal(aux) refusé(s)"
            " ; les suivants le seront aussi, sans autre message, jusqu'à ce qu'une proposition"
            " soit décidée ou expire.",
            MAX_OPEN_PROPOSALS,
            tally["refused"],
        )
    elif tally["created"] and not tally["refused"] and since is not None:
        await conn.execute(_EPISODE, {"since": None})
