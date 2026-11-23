"""Arbres de runs : bornés en travail, coupés de la même façon quel que soit le plan d'exécution.

Une réponse dépend des lectures de son run et de ses runs enfants, à toute
profondeur (``descendants``) ; une lecture touche les réponses de ses runs
parents (``ancestors``). Ces deux parcours sont des requêtes récursives, et un
graphe hostile peut les rendre immenses : chacune porte une borne.

La borne ne coûte que ce qu'elle garde. PostgreSQL ne déroule une requête
récursive que jusqu'aux lignes que la requête externe lui demande, ici une de
plus que la borne : trier le résultat complet avant de le couper l'obligerait à
dérouler toute la récursion. Il la déroule niveau par niveau, en largeur : un
niveau est produit en entier avant le suivant. Les lignes reçues forment donc
des niveaux complets, puis une part du premier niveau qui ne tient pas, et
seule cette part dépend du plan d'exécution. La coupe la remplace par un choix
fixé d'avance :

- les niveaux complets sont gardés en entier, au plus près des racines ou des
  lectures ;
- du premier niveau qui ne tient pas, les runs qui viennent d'abord, jusqu'à la
  borne. Vers les enfants : ceux de la première racine, dans l'ordre où
  l'appelant les donne, puis par identifiant. Vers les parents : par identifiant.

Vers les enfants, ce niveau se relit en entier pour en choisir les premiers
runs, et un run peut en avoir sans limite : la relecture s'arrête à
``CUT_LEVEL_FACTOR`` fois la borne, et un niveau plus grand n'est pas gardé du
tout. Vers les parents, chaque run n'en a qu'un : le niveau se déduit des
parents du niveau complet qui le précède, déjà lus.

Deux lectures du même graphe gardent ainsi les mêmes runs, et ``truncated`` dit
quand la borne est dépassée.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from sqlalchemy import text

from answerlineage_api.lineage.reading import Reader, known

# Le premier niveau incomplet d'un arbre de runs enfants se relit jusqu'à ce multiple de la
# borne : de quoi tenir les appels d'outil de réponses ordinaires, sans suivre un run qui
# aurait des enfants sans nombre.
CUT_LEVEL_FACTOR: Final = 4

# Une racine ne se rencontre qu'une fois dans son arbre : un run n'a qu'un parent, et le
# seul cycle qu'un arbre puisse rencontrer passe par sa racine. Chaque couple (racine,
# run) n'est donc produit qu'une fois, à sa profondeur, et la récursion s'arrête.
DESCENDANTS = text(
    f"""
    WITH RECURSIVE tree(root, run_id, depth) AS (
        SELECT root, root, 0 FROM unnest(CAST(:roots AS uuid[])) AS root
        UNION
        SELECT tree.root, child.run_id, tree.depth + 1
        FROM run child JOIN tree ON child.parent_run_id = tree.run_id
        WHERE child.run_id <> tree.root AND {known("child")}
    )
    SELECT root, run_id, depth FROM tree LIMIT :limit
    """  # noqa: S608 — seul un fragment constant est interpolé.
)

# Les enfants d'un niveau complet, au plus ``:level_limit`` lignes lues ; aucun quand le
# niveau en a davantage, car les premiers ne se choisissent qu'en les lisant tous.
CUT_LEVEL = text(
    f"""
    WITH level AS MATERIALIZED (
        SELECT parent.rank, parent.root, child.run_id
        FROM unnest(CAST(:ranks AS integer[]), CAST(:roots AS uuid[]),
                    CAST(:parents AS uuid[])) AS parent(rank, root, run_id)
        JOIN run child ON child.parent_run_id = parent.run_id
        WHERE child.run_id <> parent.root AND {known("child")}
        LIMIT :level_limit
    )
    SELECT root, run_id FROM level
    WHERE (SELECT count(*) FROM level) < :level_limit
    ORDER BY rank, run_id
    LIMIT :limit
    """  # noqa: S608 — seul un fragment constant est interpolé.
)

# Chaque run atteint, avec son propre parent quand il est connu : un run n'a qu'un parent,
# la ligne ne dépend donc que du run, et UNION n'en garde qu'une. Des runs qui se
# désignent l'un l'autre comme parent ne font pas tourner la recherche sans fin.
ANCESTORS = text(
    f"""
    WITH RECURSIVE up(run_id, parent_run_id) AS (
        SELECT first.run_id, p.parent_run_id
        FROM unnest(CAST(:firsts AS uuid[])) AS first(run_id)
        LEFT JOIN run p ON p.run_id = first.run_id AND {known("p")}
        UNION
        SELECT up.parent_run_id, p.parent_run_id
        FROM up LEFT JOIN run p ON p.run_id = up.parent_run_id AND {known("p")}
        WHERE up.parent_run_id IS NOT NULL
    )
    SELECT run_id, parent_run_id FROM up LIMIT :limit
    """  # noqa: S608 — seul un fragment constant est interpolé.
)


@dataclass(frozen=True)
class Trees:
    """Les couples (racine, run) des arbres, racines comprises, des plus proches aux plus profonds.

    ``truncated`` : la borne est dépassée, et seuls les runs qu'elle garde sont là.
    """

    pairs: list[tuple[UUID, UUID]]
    truncated: bool


async def descendants(
    reader: Reader, roots: Sequence[UUID], *, limit: int, known_at: datetime | None = None
) -> Trees:
    """Les runs des arbres de ces racines, ``limit`` couples (racine, run) au plus.

    Les racines viennent dans l'ordre où l'appelant veut les servir d'abord. Au-delà
    de la borne : les niveaux complets, puis, du premier qui ne tient pas, les runs de
    la première racine, puis de la suivante, chacune par identifiant.
    """
    ranked = list(dict.fromkeys(roots))
    rank = {root: index for index, root in enumerate(ranked)}
    found = await reader.fetch(DESCENDANTS, {"roots": ranked, "known_at": known_at}, limit=limit)
    rows = sorted(found.rows, key=lambda row: (row.depth, rank[row.root], row.run_id))
    if not found.truncated:
        return Trees([(row.root, row.run_id) for row in rows], truncated=False)
    if not rows:
        # Une borne nulle : rien n'est gardé, et la coupe se dit.
        return Trees([], truncated=True)

    cut = rows[-1].depth
    if cut == 0:
        return Trees([(root, root) for root in ranked[:limit]], truncated=True)
    kept = [(row.root, row.run_id) for row in rows if row.depth < cut]
    remaining = limit - len(kept)
    if remaining:
        frontier = [row for row in rows if row.depth == cut - 1]
        level = await reader.fetch(
            CUT_LEVEL,
            {
                "ranks": [rank[row.root] for row in frontier],
                "roots": [row.root for row in frontier],
                "parents": [row.run_id for row in frontier],
                "level_limit": CUT_LEVEL_FACTOR * limit + 1,
                "known_at": known_at,
            },
            limit=remaining,
        )
        kept.extend((row.root, row.run_id) for row in level.rows)
    return Trees(kept, truncated=True)


async def ancestors(
    reader: Reader, firsts: Sequence[UUID], *, limit: int, known_at: datetime | None = None
) -> tuple[list[UUID], bool]:
    """Ces runs et leurs runs parents, à toute profondeur, ``limit`` au plus, et si la borne coupe.

    ``firsts`` sont les parents des lectures : le premier niveau. Les runs viennent
    des plus proches aux plus lointains, puis par identifiant ; au-delà de la borne,
    les niveaux complets, puis les premiers identifiants du niveau qui ne tient pas.
    """
    starts = sorted(set(firsts))
    found = await reader.fetch(ANCESTORS, {"firsts": starts, "known_at": known_at}, limit=limit)
    # Le niveau de chaque run : celui de l'enfant par lequel la récursion l'atteint
    # d'abord, plus un. Les lignes arrivent niveau par niveau.
    level: dict[UUID, int] = dict.fromkeys(starts, 1)
    parent_of: dict[UUID, UUID | None] = {}
    for row in found.rows:
        parent_of[row.run_id] = row.parent_run_id
        if row.parent_run_id is not None:
            level.setdefault(row.parent_run_id, level[row.run_id] + 1)
    reached = sorted(parent_of, key=lambda run_id: (level[run_id], run_id))
    if not found.truncated:
        return reached, False
    if not reached:
        # Une borne nulle : rien n'est gardé, et la coupe se dit.
        return [], True

    cut = level[reached[-1]]
    kept = [run_id for run_id in reached if level[run_id] < cut]
    following = sorted(run_id for run_id, depth in level.items() if depth == cut)
    return [*kept, *following[: limit - len(kept)]], True
