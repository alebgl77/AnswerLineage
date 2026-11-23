"""La zone d'une quarantaine : les jeux de données dérivés de son périmètre, connus du graphe.

Un jeu Y dérive d'un jeu X quand un même run a lu X (``read_edge``) et écrit Y
(``write_edge``) : la vue qui lit une table, la table qu'un job bâtit d'une autre.
La dérivation se suit en largeur, niveau par niveau, depuis le périmètre, jusqu'à
``MAX_ZONE_DEPTH`` écritures : la profondeur d'un jeu est le plus petit nombre
d'écritures qui le séparent du périmètre. Un cycle s'arrête de lui-même : un jeu
déjà dans la zone, ou du périmètre, n'y revient pas.

- **Menée depuis les écritures.** Chaque niveau part des écritures du graphe et
  sonde, pour chacune, les lectures de son run, par l'index unique de
  ``read_edge`` (run, jeu, version). Les agents lisent beaucoup et n'écrivent
  pas : partir des lectures du périmètre parcourrait des millions de lectures
  pour n'y trouver aucune écriture. Le coût d'un niveau croît donc avec les
  écritures connues, pas avec les lectures des agents.
- **Sans filtre de temps.** Une vue créée avant la fenêtre d'un incident lit la
  table en direct, et le graphe ne distingue pas une vue d'une table : toute
  écriture connue compte. Une table dérivée bâtie avant l'incident, encore
  saine, entre donc aussi dans la zone : pour un avertissement, l'excès vaut
  mieux que l'oubli.
- **Le graphe tel qu'il était connu.** Avec ``known_at``, les lignes
  enregistrées après n'y comptent pas, comme pour l'analyse d'un incident ; un
  jeu du périmètre que le graphe ne connaît pas ne dérive de rien.
- **Bornée.** Au plus ``MAX_ZONE_DERIVED`` jeux, les plus proches du périmètre
  d'abord, puis par espace de noms et par nom, dans l'ordre de leurs
  caractères : la coupe ne dépend pas du plan d'exécution. Au-delà, la zone se
  dit tronquée. Chaque lecture part sous le délai du ``Reader``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text

from answerlineage_api.lineage.reading import Reader, known
from answerlineage_api.store.schema import MAX_ZONE_DEPTH, MAX_ZONE_DERIVED

_NO_JIT = text("SELECT set_config('jit', 'off', true)")

# Les jeux du périmètre que le graphe connaît, par leur clé naturelle.
_PERIMETER = text(
    f"""
    SELECT d.id FROM dataset d
    JOIN unnest(CAST(:namespaces AS text[]), CAST(:names AS text[])) AS p(namespace, name)
      ON d.namespace = p.namespace AND d.name = p.name
    WHERE {known("d")}
    ORDER BY d.id
    LIMIT :limit
    """  # noqa: S608 — seul un fragment constant est interpolé.
)

# Un niveau de la dérivation : les jeux écrits par un run qui a lu un jeu de la
# frontière, hors de ceux déjà vus. Chaque écriture sonde les lectures de son run.
# ``OFFSET 0`` garde la sonde par écriture : sans lui, PostgreSQL fait de l'EXISTS une
# semi-jointure, et choisit parfois de parcourir toutes les lectures de la frontière
# pour les confronter aux écritures, un travail qui croît avec les lectures des agents.
_LEVEL = text(
    f"""
    SELECT d.id, d.namespace, d.name
    FROM (
        SELECT DISTINCT w.dataset_id FROM write_edge w
        WHERE w.dataset_id <> ALL(CAST(:seen AS bigint[])) AND {known("w")}
          AND EXISTS (
              SELECT 1 FROM read_edge r
              WHERE r.run_id = w.run_id AND r.dataset_id = ANY(CAST(:frontier AS bigint[]))
                AND {known("r")}
              OFFSET 0
          )
    ) AS written
    JOIN dataset d ON d.id = written.dataset_id
    ORDER BY d.namespace COLLATE "C", d.name COLLATE "C"
    LIMIT :limit
    """  # noqa: S608 — seuls des fragments constants sont interpolés.
)


@dataclass(frozen=True)
class Derived:
    """Un jeu dérivé du périmètre, et les écritures qui l'en séparent, de 1 à 3."""

    namespace: str
    name: str
    depth: int


@dataclass(frozen=True)
class Zone:
    """Les jeux dérivés, par profondeur puis par nom, et si une borne en a écarté d'autres."""

    derived: tuple[Derived, ...]
    truncated: bool


async def derive(
    reader: Reader, perimeter: Sequence[tuple[str, str]], *, known_at: datetime | None
) -> Zone:
    """La zone du périmètre (espaces de noms et noms), sur le graphe connu à ``known_at``.

    La compilation à la volée de PostgreSQL est coupée pour la transaction : le coût
    estimé des sondes la déclencherait, et elle coûte plus qu'elle ne fait gagner.
    """
    await reader.execute(_NO_JIT, {})
    found = await reader.fetch(
        _PERIMETER,
        {
            "namespaces": [namespace for namespace, _ in perimeter],
            "names": [name for _, name in perimeter],
            "known_at": known_at,
        },
        limit=len(perimeter),
    )
    seen: list[int] = [row.id for row in found.rows]
    frontier = list(seen)
    derived: list[Derived] = []
    truncated = False
    for depth in range(1, MAX_ZONE_DEPTH + 1):
        if not frontier:
            break
        # Sans place restante, la lecture d'une ligne dit encore si la zone est tronquée.
        level = await reader.fetch(
            _LEVEL,
            {"seen": seen, "frontier": frontier, "known_at": known_at},
            limit=MAX_ZONE_DERIVED - len(derived),
        )
        derived.extend(Derived(row.namespace, row.name, depth) for row in level.rows)
        if level.truncated:
            truncated = True
            break
        frontier = [row.id for row in level.rows]
        seen.extend(frontier)
    return Zone(tuple(derived), truncated)
