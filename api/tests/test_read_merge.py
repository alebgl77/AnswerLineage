"""Deux lectures d'un jeu par un même run se fusionnent sans jamais dire moins que l'une d'elles.

Une lecture dit deux choses : quelles lignes (toutes, un sous-ensemble, des
clés) et quelles colonnes (toutes, ou une liste) ont été lues. ``covers`` est
cette sémantique, écrite ici sans la règle de fusion : la fusion doit couvrir
chacune des deux lectures, et rien de plus que ce qu'il faut pour cela.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import permutations, product
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.store.graph import (
    DatasetRef,
    Read,
    ReadShape,
    RunRecord,
    canonical_read,
    merge_reads,
    record_run,
)

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
TAUX = DatasetRef("bigquery", "finance.ref.taux_change")

type Shape = ReadShape

# Chaque précision, sans colonne et avec des colonnes qui se recoupent ou non. La
# casse mêlée vérifie que l'ordre des colonnes ne dépend pas de la collation.
SHAPES: tuple[Shape, ...] = (
    ("table", ()),
    ("columns", ("a",)),
    ("columns", ("B",)),
    ("columns", ("B", "a")),
    ("subset", ()),
    ("subset", ("a",)),
    ("subset", ("B",)),
    ("keys", ()),
    ("keys", ("a",)),
    ("keys", ("B",)),
)
# Toutes les formes canoniques sur ces deux colonnes : les candidats d'une fusion.
CANDIDATES: tuple[Shape, ...] = (
    ("table", ()),
    *(
        (precision, columns)
        for precision in ("columns",)
        for columns in (("a",), ("B",), ("B", "a"))
    ),
    *(
        (precision, columns)
        for precision in ("subset", "keys")
        for columns in ((), ("a",), ("B",), ("B", "a"))
    ),
)


def _rows(shape: Shape) -> str:
    """Les lignes lues : toutes, ou une restriction que le graphe nomme sans la décrire."""
    return "all" if shape[0] in ("table", "columns") else shape[0]


def _columns(shape: Shape) -> frozenset[str] | None:
    """Les colonnes lues ; ``None`` pour toutes."""
    precision, columns = shape
    return None if precision == "table" or not columns else frozenset(columns)


def covers(wider: Shape, narrower: Shape) -> bool:
    """``wider`` dit au moins tout ce que ``narrower`` a lu."""
    rows = _rows(wider) == "all" or _rows(wider) == _rows(narrower)
    wide, narrow = _columns(wider), _columns(narrower)
    return rows and (wide is None or (narrow is not None and narrow <= wide))


@pytest.mark.parametrize(("first", "second"), list(product(SHAPES, repeat=2)))
def test_the_merge_never_says_less_than_either_read(first: Shape, second: Shape) -> None:
    merged = merge_reads(first, second)
    assert covers(merged, first)
    assert covers(merged, second)


@pytest.mark.parametrize(("first", "second"), list(product(SHAPES, repeat=2)))
def test_the_merge_is_the_narrowest_read_that_covers_both(first: Shape, second: Shape) -> None:
    merged = merge_reads(first, second)
    assert merged in CANDIDATES
    for candidate in CANDIDATES:
        if covers(candidate, first) and covers(candidate, second):
            assert covers(candidate, merged), candidate


@pytest.mark.parametrize(
    ("first", "second", "merged"),
    [
        # La table entière l'emporte sur tout.
        (("table", ()), ("columns", ("a",)), ("table", ())),
        (("table", ()), ("subset", ("a",)), ("table", ())),
        (("table", ()), ("keys", ()), ("table", ())),
        # Toutes les lignes, des colonnes nommées de part et d'autre : leur union.
        (("columns", ("a",)), ("columns", ("B",)), ("columns", ("B", "a"))),
        (("columns", ("a",)), ("subset", ("B",)), ("columns", ("B", "a"))),
        (("columns", ("a",)), ("keys", ("a",)), ("columns", ("a",))),
        # Une lecture de toutes les colonnes, même de quelques lignes, couvre toutes les colonnes.
        (("columns", ("a",)), ("subset", ()), ("table", ())),
        (("columns", ("a",)), ("keys", ()), ("table", ())),
        # Une même restriction des lignes se garde.
        (("subset", ("a",)), ("subset", ("B",)), ("subset", ("B", "a"))),
        (("subset", ("a",)), ("subset", ()), ("subset", ())),
        (("keys", ("a",)), ("keys", ("B",)), ("keys", ("B", "a"))),
        (("keys", ()), ("keys", ("a",)), ("keys", ())),
        # Deux restrictions différentes ne se décrivent par aucune des deux : toutes les lignes.
        (("subset", ("a",)), ("keys", ("B",)), ("columns", ("B", "a"))),
        (("subset", ()), ("keys", ("a",)), ("table", ())),
    ],
)
def test_the_merge_table(first: Shape, second: Shape, merged: Shape) -> None:
    assert merge_reads(first, second) == merged
    assert merge_reads(second, first) == merged


def test_the_merge_is_idempotent_commutative_and_associative() -> None:
    for first in SHAPES:
        assert merge_reads(first, first) == canonical_read(*first)
    for first, second in product(SHAPES, repeat=2):
        assert merge_reads(first, second) == merge_reads(second, first)
    for first, second, third in product(SHAPES, repeat=3):
        assert merge_reads(merge_reads(first, second), third) == merge_reads(
            first, merge_reads(second, third)
        )


@pytest.mark.parametrize(
    ("shape", "canonical"),
    [
        # Une table entière ne nomme aucune colonne.
        (("table", ("a",)), ("table", ())),
        # Des colonnes sans aucune colonne nommée : rien ne restreint la lecture.
        (("columns", ()), ("table", ())),
        # Les colonnes une fois chacune, dans l'ordre des points de code.
        (("columns", ("a", "B", "a")), ("columns", ("B", "a"))),
        (("subset", ()), ("subset", ())),
        (("keys", ("a", "a")), ("keys", ("a",))),
    ],
)
def test_a_read_has_one_canonical_form(shape: Shape, canonical: Shape) -> None:
    assert canonical_read(*shape) == canonical
    assert merge_reads(shape, shape) == canonical


# Dans la base : la fusion des événements reçus est celle de ``merge_reads``.


def _event(run_id: UUID, shape: Shape, second: int) -> RunRecord:
    precision, columns = shape
    return RunRecord(
        run_id,
        "tool",
        "RUNNING",
        T0 + timedelta(seconds=second),
        reads=(Read(TAUX, precision=precision, columns=columns),),
    )


async def _receive(engine: AsyncEngine, run_id: UUID, *shapes: Shape) -> None:
    """Chaque lecture arrive dans son propre événement, enregistré dans sa propre transaction."""
    for second, shape in enumerate(shapes):
        async with engine.begin() as conn:
            await record_run(conn, _event(run_id, shape, second))


async def _edge(engine: AsyncEngine, run_id: UUID) -> Shape:
    async with engine.connect() as conn:
        precision, columns = (
            await conn.execute(
                text("SELECT precision, columns FROM read_edge WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
        ).one()
    return precision, tuple(columns)


@pytest.mark.integration
@pytest.mark.anyio
async def test_the_graph_merges_every_pair_of_reads_in_either_order(engine: AsyncEngine) -> None:
    for first, second in product(SHAPES, repeat=2):
        merged = merge_reads(first, second)
        in_order, reversed_order = uuid4(), uuid4()
        await _receive(engine, in_order, first, second)
        await _receive(engine, reversed_order, second, first)
        assert await _edge(engine, in_order) == merged, (first, second)
        assert await _edge(engine, reversed_order) == merged, (second, first)


@pytest.mark.integration
@pytest.mark.anyio
async def test_a_replayed_read_changes_nothing(engine: AsyncEngine) -> None:
    for shape in SHAPES:
        run_id = uuid4()
        await _receive(engine, run_id, shape, shape, shape)
        assert await _edge(engine, run_id) == canonical_read(*shape)


@pytest.mark.integration
@pytest.mark.anyio
@pytest.mark.parametrize(
    "triplet",
    [
        (("table", ()), ("columns", ("a",)), ("columns", ("B",))),
        (("columns", ("a",)), ("subset", ("B",)), ("keys", ())),
        (("subset", ("a",)), ("subset", ("B",)), ("subset", ())),
        (("subset", ("a",)), ("keys", ("a",)), ("columns", ("B",))),
        (("keys", ("a",)), ("keys", ("B",)), ("keys", ("a",))),
    ],
)
async def test_three_reads_merged_in_three_times_give_the_same_edge_in_any_order(
    engine: AsyncEngine, triplet: tuple[Shape, Shape, Shape]
) -> None:
    first, second, third = triplet
    merged = merge_reads(merge_reads(first, second), third)
    for order in permutations(triplet):
        run_id = uuid4()
        await _receive(engine, run_id, *order)
        assert await _edge(engine, run_id) == merged, order
        # Rejouer les trois événements ne change rien.
        await _receive(engine, run_id, *order)
        assert await _edge(engine, run_id) == merged, order
