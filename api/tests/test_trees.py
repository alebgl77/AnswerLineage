"""Les arbres de runs : bornés en travail, et coupés de la même façon quel que soit le plan.

Chaque arbre est inséré dans un ordre mélangé, puis parcouru sous plusieurs plans
d'exécution : PostgreSQL rend alors les runs d'un même niveau dans des ordres
différents, et la coupe doit rester la même.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import TextClause, text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.lineage import trees
from answerlineage_api.lineage.reading import Reader

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
# Des plans qui parcourent les runs d'un niveau dans des ordres différents.
PLANS: tuple[tuple[str, ...], ...] = (
    (),
    ("enable_hashjoin",),
    ("enable_nestloop",),
    ("enable_hashjoin", "enable_mergejoin"),
    ("enable_indexscan", "enable_bitmapscan"),
)

type Edges = dict[UUID, UUID | None]


def _digest(*parts: object) -> bytes:
    return hashlib.sha256("/".join(str(part) for part in parts).encode()).digest()


class Draws:
    """Des identifiants tirés d'une étiquette : sans ordre entre eux, les mêmes à chaque fois."""

    def __init__(self, label: str) -> None:
        self._label = label
        self._drawn = 0

    def ids(self, count: int) -> list[UUID]:
        self._drawn += count
        return [
            UUID(bytes=_digest(self._label, rank)[:16])
            for rank in range(self._drawn - count, self._drawn)
        ]


def tree(draws: Draws, *widths: int) -> tuple[UUID, list[list[UUID]], Edges]:
    """Une racine, puis des niveaux où chaque run a ``width`` enfants : les niveaux et les liens."""
    (root,) = draws.ids(1)
    levels = [[root]]
    edges: Edges = {root: None}
    for width in widths:
        children: list[UUID] = []
        for parent in levels[-1]:
            for child in draws.ids(width):
                edges[child] = parent
                children.append(child)
        levels.append(children)
    return root, levels, edges


async def insert(engine: AsyncEngine, edges: Edges) -> None:
    """Les runs, mélangés : l'ordre physique de la table n'est pas celui des niveaux."""
    rows = [{"run_id": run_id, "parent": parent} for run_id, parent in edges.items()]
    rows.sort(key=lambda row: _digest("ordre", row["run_id"]))
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO run (run_id, kind, parent_run_id, state, state_time)"
                " VALUES (:run_id, 'agent', :parent, 'COMPLETE', :at)"
            ),
            [{**row, "at": T0} for row in rows],
        )
        await conn.execute(text("ANALYZE run"))


@asynccontextmanager
async def reading(engine: AsyncEngine, plan: Sequence[str] = ()) -> AsyncIterator[Reader]:
    """Une lecture sous ce plan : chaque méthode nommée est écartée, pour la transaction."""
    async with engine.connect() as conn:
        for setting in plan:
            await conn.execute(text(f"SET LOCAL {setting} = off"))
        yield Reader(conn, deadline=time.monotonic() + 30)


async def test_a_tree_within_the_bound_comes_whole_from_the_root_down(engine: AsyncEngine) -> None:
    draws = Draws("1")
    root, levels, edges = tree(draws, 3, 2)
    await insert(engine, edges)

    async with reading(engine) as reader:
        found = await trees.descendants(reader, [root], limit=len(edges))

    assert found.truncated is False
    assert found.pairs == [(root, run_id) for level in levels for run_id in sorted(level)]


async def test_beyond_the_bound_a_tree_keeps_its_complete_levels_then_the_first_identifiers(
    engine: AsyncEngine,
) -> None:
    draws = Draws("2")
    root, levels, edges = tree(draws, 6, 5)
    await insert(engine, edges)
    expected = [(root, run_id) for run_id in [root, *sorted(levels[1]), *sorted(levels[2])[:3]]]

    for plan in PLANS:
        async with reading(engine, plan) as reader:
            found = await trees.descendants(reader, [root], limit=10)
        assert (found.pairs, found.truncated) == (expected, True), plan


async def test_the_cut_level_serves_the_roots_in_the_order_given(engine: AsyncEngine) -> None:
    draws = Draws("3")
    first, first_levels, first_edges = tree(draws, 4)
    second, second_levels, second_edges = tree(draws, 4)
    await insert(engine, {**first_edges, **second_edges})

    for roots, (top, top_children), (next_, next_children) in (
        ([first, second], (first, first_levels[1]), (second, second_levels[1])),
        ([second, first], (second, second_levels[1]), (first, first_levels[1])),
    ):
        expected = [
            (top, top),
            (next_, next_),
            *((top, child) for child in sorted(top_children)),
            (next_, min(next_children)),
        ]
        for plan in PLANS:
            async with reading(engine, plan) as reader:
                found = await trees.descendants(reader, roots, limit=7)
            assert (found.pairs, found.truncated) == (expected, True), plan


async def test_a_cut_level_too_large_to_be_read_is_left_out(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    draws = Draws("4")
    root, levels, edges = tree(draws, 2, 6)
    await insert(engine, edges)
    monkeypatch.setattr(trees, "CUT_LEVEL_FACTOR", 1)

    async with reading(engine) as reader:
        found = await trees.descendants(reader, [root], limit=5)

    # Le troisième niveau compte 12 runs, plus de cinq : aucun n'est gardé.
    assert found.pairs == [(root, root), *((root, child) for child in sorted(levels[1]))]
    assert found.truncated is True


async def test_more_roots_than_the_bound_keep_the_first_roots(engine: AsyncEngine) -> None:
    draws = Draws("5")
    roots = draws.ids(5)
    await insert(engine, dict.fromkeys(roots))

    async with reading(engine) as reader:
        found = await trees.descendants(reader, roots, limit=3)

    assert found.pairs == [(root, root) for root in roots[:3]]
    assert found.truncated is True


async def test_a_tree_whose_root_is_its_own_descendant_ends(engine: AsyncEngine) -> None:
    draws = Draws("6")
    root, child = draws.ids(2)
    await insert(engine, {root: child, child: root})

    async with reading(engine) as reader:
        found = await trees.descendants(reader, [root], limit=10)

    assert (found.pairs, found.truncated) == ([(root, root), (root, child)], False)


def _plan_node(plan: dict[str, Any], wanted: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    """Le premier nœud voulu d'un plan JSON d'``EXPLAIN``."""
    stack = [plan]
    while stack:
        node = stack.pop()
        if wanted(node):
            return node
        stack.extend(node.get("Plans", ()))
    raise AssertionError("nœud absent du plan")


async def _explained(
    engine: AsyncEngine, statement: TextClause, parameters: dict[str, Any]
) -> dict[str, Any]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(f"EXPLAIN (ANALYZE, FORMAT JSON) {statement.text}"), parameters
        )
        plan = result.scalar_one()
    document = json.loads(plan) if isinstance(plan, str) else plan
    node: dict[str, Any] = document[0]["Plan"]
    return node


async def test_the_recursion_stops_at_the_bound_on_a_tree_larger_than_it(
    engine: AsyncEngine,
) -> None:
    draws = Draws("7")
    root, levels, edges = tree(draws, 40, 50)
    await insert(engine, edges)
    limit = 100

    # PostgreSQL ne déroule la récursion que jusqu'à la ligne qui suit la borne.
    plan = await _explained(
        engine, trees.DESCENDANTS, {"roots": [root], "known_at": None, "limit": limit + 1}
    )
    assert len(edges) == 2_041
    recursion = _plan_node(plan, lambda node: node["Node Type"] == "Recursive Union")
    assert recursion["Actual Rows"] == limit + 1

    # Le niveau coupé se lit jusqu'à sa propre borne, pas en entier.
    level_limit = 500
    plan = await _explained(
        engine,
        trees.CUT_LEVEL,
        {
            "ranks": [0] * len(levels[1]),
            "roots": [root] * len(levels[1]),
            "parents": levels[1],
            "level_limit": level_limit,
            "known_at": None,
            "limit": limit + 1,
        },
    )
    assert len(levels[2]) == 2_000
    level = _plan_node(plan, lambda node: node.get("Subplan Name") == "CTE level")
    assert level["Actual Rows"] == level_limit


def chains(draws: Draws, starts: int) -> tuple[list[UUID], list[UUID], UUID, Edges]:
    """Des parents de lectures, chacun avec son propre parent, tous sous un même run."""
    firsts, seconds = draws.ids(starts), draws.ids(starts)
    (top,) = draws.ids(1)
    edges: Edges = {top: None}
    edges.update(zip(firsts, seconds, strict=True))
    edges.update(dict.fromkeys(seconds, top))
    return firsts, seconds, top, edges


async def test_ancestors_within_the_bound_come_from_the_nearest(engine: AsyncEngine) -> None:
    draws = Draws("8")
    firsts, seconds, top, edges = chains(draws, 6)
    await insert(engine, edges)

    async with reading(engine) as reader:
        found = await trees.ancestors(reader, firsts, limit=13)

    assert found == ([*sorted(firsts), *sorted(seconds), top], False)


async def test_beyond_the_bound_ancestors_keep_the_nearest_levels_then_the_first_identifiers(
    engine: AsyncEngine,
) -> None:
    draws = Draws("9")
    firsts, seconds, _, edges = chains(draws, 6)
    await insert(engine, edges)

    for limit, expected in (
        (8, [*sorted(firsts), *sorted(seconds)[:2]]),
        (12, [*sorted(firsts), *sorted(seconds)]),
        (4, sorted(firsts)[:4]),
    ):
        for plan in PLANS:
            async with reading(engine, plan) as reader:
                found = await trees.ancestors(reader, firsts, limit=limit)
            assert found == (expected, True), (limit, plan)


async def test_ancestors_that_name_each_other_as_parent_end(engine: AsyncEngine) -> None:
    draws = Draws("10")
    first, second = sorted(draws.ids(2))
    await insert(engine, {first: second, second: first})

    async with reading(engine) as reader:
        assert await trees.ancestors(reader, [first], limit=2) == ([first, second], False)
        assert await trees.ancestors(reader, [first], limit=1) == ([first], True)
