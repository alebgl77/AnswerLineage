"""Propager : d'un jeu de données faux aux réponses et aux destinataires touchés.

Une réponse est touchée quand un run de son arbre a lu le jeu de données
pendant la fenêtre : son propre run, un appel d'outil, un sous-agent. C'est
l'heure de la lecture qui compte, pas celle de la réponse. La propagation
s'arrête à la réponse : elle ne suit pas encore ce qu'un run touché a écrit.

Chaque étape a sa borne, et un résultat qui en dépasse une le dit
(``truncated``) : ses décomptes sont alors des minimums. Pour un rappel, taire
une réponse touchée est le pire défaut ; les bornes sont donc posées pour que
ce qui mène aux réponses passe d'abord :

- les lectures de la fenêtre ont leur propre borne, et une lecture qui ne mène
  à rien (ni réponse, ni run parent) n'y compte pas ;
- la remontée aux runs parents a la sienne : les lecteurs ne l'épuisent jamais
  avant elle. Elle va d'abord aux runs les plus proches des lectures.

Le résultat dit aussi ce que la propagation n'a pas pu voir : la couverture du
lignage sur la fenêtre de l'incident et ses trous connus (``coverage.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import TextClause, text

from answerlineage_api.lineage import coverage
from answerlineage_api.lineage.models import (
    DatasetName,
    ImpactCounts,
    ImpactResult,
    Window,
)
from answerlineage_api.lineage.overview import ANSWER_COLUMNS, view_of
from answerlineage_api.lineage.paging import AFTER_CURSOR, ANSWER_ORDER, Cursor
from answerlineage_api.lineage.reading import Reader

# Lectures du jeu de données pendant la fenêtre, les plus récentes d'abord.
MAX_READS = 20_000
# Runs qui ont délégué ces lectures, à toute profondeur.
MAX_RUNS = 20_000
# Réponses comptées, et liens de diffusion vers leurs destinataires.
MAX_ANSWERS = 100_000
MAX_LINKS = 200_000

_DATASET = text(
    "SELECT id, kind FROM dataset WHERE namespace = :namespace AND name = :name LIMIT :limit"
)

# Une lecture faite par un run sans parent ni réponse ne touche aucune réponse.
_READERS = text(
    """
    SELECT e.run_id
    FROM read_edge e JOIN run r ON r.run_id = e.run_id
    WHERE e.dataset_id = :dataset_id AND e.event_time >= :start AND e.event_time < :end
      AND (r.parent_run_id IS NOT NULL
           OR EXISTS (SELECT 1 FROM answer a WHERE a.run_id = e.run_id))
    ORDER BY e.event_time DESC, e.id DESC
    LIMIT :limit
    """
)

# Des lecteurs vers leurs runs parents, niveau par niveau. UNION, et non
# UNION ALL : des runs qui se désignent l'un l'autre comme parent ne font pas
# tourner la recherche sans fin.
_ANCESTORS = text(
    """
    WITH RECURSIVE up(run_id) AS (
        SELECT r.parent_run_id FROM run r
        WHERE r.run_id = ANY(:readers) AND r.parent_run_id IS NOT NULL
        UNION
        SELECT r.parent_run_id FROM run r JOIN up ON r.run_id = up.run_id
        WHERE r.parent_run_id IS NOT NULL
    )
    SELECT run_id FROM up LIMIT :limit
    """
)

# Chaque décompte lit au plus une ligne de plus que sa borne.
_COUNTS = text(
    """
    WITH touched AS (
        SELECT a.id FROM answer a WHERE a.run_id = ANY(:run_ids) LIMIT :answers_limit
    ), links AS (
        SELECT l.recipient_id
        FROM touched t
        JOIN delivery d ON d.answer_id = t.id
        JOIN delivery_recipient l ON l.delivery_id = d.id
        LIMIT :links_limit
    )
    SELECT (SELECT count(*) FROM touched) AS answers,
           (SELECT count(*) FROM links) AS links,
           (SELECT count(DISTINCT recipient_id) FROM links) AS recipients
    """
)


def _answers(cursor: Cursor | None) -> TextClause:
    after = "" if cursor is None else f"AND {AFTER_CURSOR}"
    return text(
        f"""
        SELECT {ANSWER_COLUMNS}
        FROM answer a
        JOIN run r ON r.run_id = a.run_id
        LEFT JOIN agent g ON g.id = r.agent_id
        WHERE a.run_id = ANY(:run_ids)
        {after}
        ORDER BY {ANSWER_ORDER}
        LIMIT :limit
        """  # noqa: S608 — seuls des fragments constants sont interpolés.
    )


async def propagate(
    reader: Reader,
    *,
    namespace: str,
    name: str,
    start: datetime,
    end: datetime,
    limit: int,
    cursor: Cursor | None,
    now: datetime | None = None,
) -> ImpactResult | None:
    """Réponses et destinataires touchés ; ``None`` quand le jeu de données est inconnu.

    Le résultat porte la couverture du lignage sur la fenêtre, et ses trous
    connus : ce que la propagation n'a pas pu voir. ``now`` borne la partie de
    la fenêtre qui peut être mesurée ; par défaut, l'heure courante.
    """
    found = await reader.fetch(_DATASET, {"namespace": namespace, "name": name}, limit=1)
    if not found.rows:
        return None
    dataset = found.rows[0]

    readers = await reader.fetch(
        _READERS, {"dataset_id": dataset.id, "start": start, "end": end}, limit=MAX_READS
    )
    reader_ids = list(dict.fromkeys(row.run_id for row in readers.rows))
    ancestors = await reader.fetch(_ANCESTORS, {"readers": reader_ids}, limit=MAX_RUNS)
    run_ids = list(dict.fromkeys([*reader_ids, *(row.run_id for row in ancestors.rows)]))

    counts = (
        await reader.execute(
            _COUNTS,
            {
                "run_ids": run_ids,
                "answers_limit": MAX_ANSWERS + 1,
                "links_limit": MAX_LINKS + 1,
            },
        )
    ).one()
    truncated = (
        readers.truncated
        or ancestors.truncated
        or counts.answers > MAX_ANSWERS
        or counts.links > MAX_LINKS
    )

    params = {"run_ids": run_ids} | ({} if cursor is None else cursor.parameters())
    picked = await reader.fetch(_answers(cursor), params, limit=limit)
    return ImpactResult(
        dataset=DatasetName(namespace=namespace, name=name, kind=dataset.kind),
        window=Window(start=start, end=end),
        counts=ImpactCounts(
            answers=counts.answers, recipients=counts.recipients, lower_bound=truncated
        ),
        coverage=await coverage.over_period(
            reader, start=start, end=end, now=now or datetime.now(UTC)
        ),
        graph=await view_of(reader, picked.rows, more=picked.truncated),
        truncated=truncated,
    )
