"""Propager : d'un jeu de données faux aux réponses et aux destinataires touchés.

Une réponse est touchée quand un run de son arbre a lu le jeu de données
pendant la fenêtre : son propre run, un appel d'outil, un sous-agent. C'est
l'heure de la lecture qui compte, pas celle de la réponse. La propagation
s'arrête à la réponse : elle ne suit pas encore ce qu'un run touché a écrit.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from answerlineage_api.lineage.models import (
    Coverage,
    DatasetName,
    ImpactCounts,
    ImpactResult,
    Window,
)
from answerlineage_api.lineage.overview import view_of

# Borne des runs touchés par un incident : lecteurs et runs qui les ont délégués.
MAX_RUNS = 20_000

_DATASET = text("SELECT id, kind FROM dataset WHERE namespace = :namespace AND name = :name")

# Des lectures de la fenêtre vers les runs parents. UNION, et non UNION ALL :
# des runs qui se désignent l'un l'autre comme parent ne font pas tourner la
# recherche sans fin.
_TOUCHED_RUNS = text(
    """
    WITH RECURSIVE touched(run_id) AS (
        SELECT e.run_id FROM read_edge e
        WHERE e.dataset_id = :dataset_id AND e.event_time >= :start AND e.event_time < :end
        UNION
        SELECT r.parent_run_id FROM run r JOIN touched ON r.run_id = touched.run_id
        WHERE r.parent_run_id IS NOT NULL
    )
    SELECT run_id FROM touched LIMIT :limit
    """
)

_COUNTS = text(
    """
    SELECT count(DISTINCT a.id) AS answers, count(DISTINCT l.recipient_id) AS recipients
    FROM answer a
    LEFT JOIN delivery d ON d.answer_id = a.id
    LEFT JOIN delivery_recipient l ON l.delivery_id = d.id
    WHERE a.run_id = ANY(:run_ids)
    """
)

_ANSWERS = text(
    """
    SELECT a.id, a.run_id, a.fingerprint, a.event_time,
           g.namespace AS agent_namespace, g.name AS agent_name
    FROM answer a
    JOIN run r ON r.run_id = a.run_id
    LEFT JOIN agent g ON g.id = r.agent_id
    WHERE a.run_id = ANY(:run_ids)
      AND (CAST(:before AS timestamptz) IS NULL OR a.event_time < CAST(:before AS timestamptz))
    ORDER BY a.event_time DESC, a.id DESC
    LIMIT :limit
    """
)


async def propagate(
    conn: AsyncConnection,
    *,
    namespace: str,
    name: str,
    start: datetime,
    end: datetime,
    limit: int,
    before: datetime | None,
) -> ImpactResult | None:
    """Réponses et destinataires touchés ; ``None`` quand le jeu de données est inconnu."""
    dataset = (await conn.execute(_DATASET, {"namespace": namespace, "name": name})).one_or_none()
    if dataset is None:
        return None

    found = (
        (
            await conn.execute(
                _TOUCHED_RUNS,
                {"dataset_id": dataset.id, "start": start, "end": end, "limit": MAX_RUNS + 1},
            )
        )
        .scalars()
        .all()
    )
    truncated = len(found) > MAX_RUNS
    run_ids = list(found[:MAX_RUNS])

    counts = (await conn.execute(_COUNTS, {"run_ids": run_ids})).one()
    picked = (
        await conn.execute(_ANSWERS, {"run_ids": run_ids, "before": before, "limit": limit + 1})
    ).all()
    return ImpactResult(
        dataset=DatasetName(namespace=namespace, name=name, kind=dataset.kind),
        window=Window(start=start, end=end),
        counts=ImpactCounts(answers=counts.answers, recipients=counts.recipients),
        coverage=Coverage(status="unknown"),
        graph=await view_of(conn, picked[:limit], more=len(picked) > limit),
        truncated=truncated,
    )
