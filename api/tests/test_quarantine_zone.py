"""La zone d'une quarantaine : les vues et les tables dérivées de son périmètre, connues du graphe.

Un jeu dérive d'un autre quand un même run a lu l'un et écrit l'autre. La zone se
suit en largeur depuis le périmètre, jusqu'à trois écritures ; elle est bornée,
coupée de la même façon quel que soit le plan d'exécution, et ne compte que ce que
le graphe connaissait à son heure.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.lineage.reading import Reader, ReadTimedOut
from answerlineage_api.lineage.zone import Derived, Zone, derive
from answerlineage_api.store.graph import (
    AgentRef,
    Answer,
    DatasetRef,
    Read,
    RunRecord,
    ToolRef,
    Write,
    record_run,
)
from answerlineage_api.store.schema import MAX_ZONE_DEPTH, MAX_ZONE_DERIVED

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

T0 = datetime(2026, 11, 16, 6, 0, tzinfo=UTC)
ORDERS = DatasetRef("bigquery", "ventes.suivi.commandes")
DAILY = DatasetRef("bigquery", "ventes.vues.commandes_du_jour")
REVENUE = DatasetRef("bigquery", "finance.marts.chiffre_affaires")
BOARD = DatasetRef("bigquery", "direction.tableau.synthese")
ARCHIVE = DatasetRef("gcs", "archives/direction/synthese")
PERIMETER = [(ORDERS.namespace, ORDERS.name)]
SALES = AgentRef("agent://ventes", "assistant-commercial")
SQL = ToolRef("entrepot", "execute_sql")
# Des plans qui parcourent les écritures et les lectures dans des ordres différents.
PLANS: tuple[tuple[str, ...], ...] = (
    (),
    ("enable_hashjoin",),
    ("enable_nestloop",),
    ("enable_indexscan", "enable_bitmapscan"),
)


async def record(engine: AsyncEngine, *records: RunRecord) -> None:
    async with engine.begin() as conn:
        for item in records:
            await record_run(conn, item)


def job(
    reads: Sequence[DatasetRef],
    writes: Sequence[DatasetRef],
    *,
    recorded_at: datetime | None = None,
) -> RunRecord:
    """Un run de transformation : il lit ses entrées et écrit ses sorties."""
    return RunRecord(
        run_id=uuid4(),
        kind="job",
        state="COMPLETE",
        event_time=T0,
        reads=tuple(Read(dataset) for dataset in reads),
        writes=tuple(Write(dataset) for dataset in writes),
        recorded_at=recorded_at,
    )


@asynccontextmanager
async def reading(
    engine: AsyncEngine, *disabled: str, timeout: float = 30.0
) -> AsyncIterator[Reader]:
    async with engine.connect() as conn:
        for setting in disabled:
            await conn.execute(text(f"SET LOCAL {setting} = off"))
        yield Reader(conn, deadline=time.monotonic() + timeout)


async def zone_of(
    engine: AsyncEngine,
    perimeter: Sequence[tuple[str, str]] = PERIMETER,
    *,
    known_at: datetime | None = None,
    disabled: tuple[str, ...] = (),
) -> Zone:
    async with reading(engine, *disabled) as reader:
        return await derive(reader, perimeter, known_at=known_at)


def named(zone: Zone) -> list[tuple[str, int]]:
    return [(item.name, item.depth) for item in zone.derived]


async def test_a_view_that_reads_the_table_has_depth_one_a_table_built_from_the_view_depth_two(
    engine: AsyncEngine,
) -> None:
    await record(
        engine,
        job([ORDERS], [DAILY]),
        job([DAILY], [REVENUE]),
        job([REVENUE], [BOARD, ARCHIVE]),
    )

    zone = await zone_of(engine)

    assert zone == Zone(
        (
            Derived(DAILY.namespace, DAILY.name, 1),
            Derived(REVENUE.namespace, REVENUE.name, 2),
            # À profondeur égale, par espace de noms, puis par nom.
            Derived(BOARD.namespace, BOARD.name, 3),
            Derived(ARCHIVE.namespace, ARCHIVE.name, 3),
        ),
        truncated=False,
    )


async def test_a_dataset_keeps_its_shortest_distance_from_the_perimeter(
    engine: AsyncEngine,
) -> None:
    await record(
        engine,
        job([ORDERS], [DAILY]),
        job([DAILY], [REVENUE]),
        # Un raccourci : le même jeu, écrit aussi directement depuis le périmètre.
        job([ORDERS], [REVENUE]),
    )
    assert named(await zone_of(engine)) == [(REVENUE.name, 1), (DAILY.name, 1)]


async def test_the_fourth_write_is_beyond_the_zone_and_a_cycle_ends(engine: AsyncEngine) -> None:
    fourth = DatasetRef("bigquery", "direction.exports.quatrieme")
    await record(
        engine,
        job([ORDERS], [DAILY]),
        job([DAILY], [REVENUE]),
        job([REVENUE], [BOARD]),
        job([BOARD], [fourth]),
        # Un cycle : la synthèse réécrit la table fausse, et la vue d'où elle vient.
        job([BOARD], [ORDERS, DAILY]),
    )

    zone = await zone_of(engine)

    assert named(zone) == [(DAILY.name, 1), (REVENUE.name, 2), (BOARD.name, MAX_ZONE_DEPTH)]
    # La profondeur est une limite de la zone, pas une coupe : rien n'est tronqué.
    assert not zone.truncated
    # Le périmètre n'est jamais un jeu dérivé de lui-même.
    assert ORDERS.name not in {name for name, _ in named(zone)}


async def test_reads_of_agents_that_write_nothing_add_nothing(engine: AsyncEngine) -> None:
    request, call = uuid4(), uuid4()
    await record(
        engine,
        RunRecord(
            run_id=request,
            kind="agent",
            state="COMPLETE",
            event_time=T0,
            agent=SALES,
            answers=(Answer("sha256:" + "a" * 64),),
        ),
        RunRecord(
            run_id=call,
            kind="tool",
            state="COMPLETE",
            event_time=T0,
            tool=SQL,
            parent_run_id=request,
            reads=(Read(ORDERS),),
        ),
        # Un run qui écrit sans avoir lu le périmètre, et l'enfant d'un lecteur qui écrit :
        # la dérivation tient à un même run, qui lit et écrit.
        job([DatasetRef("bigquery", "rh.paie")], [BOARD]),
        RunRecord(
            run_id=uuid4(),
            kind="tool",
            state="COMPLETE",
            event_time=T0,
            tool=SQL,
            parent_run_id=call,
            writes=(Write(ARCHIVE),),
        ),
    )

    assert await zone_of(engine) == Zone((), truncated=False)


async def test_beyond_its_bound_the_zone_keeps_the_closest_then_by_name_and_says_so(
    engine: AsyncEngine,
) -> None:
    near = [DatasetRef("bigquery", f"ventes.vues.v{rank:03d}") for rank in range(MAX_ZONE_DERIVED)]
    await record(
        engine,
        job([ORDERS], list(reversed(near))),
        # Plus loin, mais avant dans l'ordre des noms : la profondeur passe d'abord.
        job([near[0]], [DatasetRef("bigquery", "a.premier.par.son.nom")]),
    )

    for disabled in PLANS:
        zone = await zone_of(engine, disabled=disabled)
        assert named(zone) == [(item.name, 1) for item in near], disabled
        assert zone.truncated, disabled


async def test_a_zone_exactly_at_its_bound_is_whole(engine: AsyncEngine) -> None:
    near = [DatasetRef("bigquery", f"ventes.vues.v{rank:03d}") for rank in range(MAX_ZONE_DERIVED)]
    await record(engine, job([ORDERS], near))

    zone = await zone_of(engine)

    assert len(zone.derived) == MAX_ZONE_DERIVED
    assert not zone.truncated


async def test_within_a_level_the_bound_cuts_by_name_whatever_the_plan(
    engine: AsyncEngine,
) -> None:
    many = [
        DatasetRef("bigquery", f"ventes.vues.v{rank:03d}") for rank in range(MAX_ZONE_DERIVED + 10)
    ]
    await record(engine, *(job([ORDERS], [item]) for item in reversed(many)))

    for disabled in PLANS:
        zone = await zone_of(engine, disabled=disabled)
        assert named(zone) == [(item.name, 1) for item in many[:MAX_ZONE_DERIVED]], disabled
        assert zone.truncated, disabled


async def test_only_what_was_known_counts(engine: AsyncEngine) -> None:
    await record(
        engine,
        job([ORDERS], [DAILY], recorded_at=T0),
        job([DAILY], [REVENUE], recorded_at=T0 + timedelta(hours=2)),
    )

    # Le graphe tel qu'il était connu avant la seconde écriture.
    assert named(await zone_of(engine, known_at=T0 + timedelta(hours=1))) == [(DAILY.name, 1)]
    assert named(await zone_of(engine, known_at=T0 - timedelta(hours=1))) == []
    assert named(await zone_of(engine)) == [(DAILY.name, 1), (REVENUE.name, 2)]
    # Un jeu du périmètre que le graphe ne connaît pas ne dérive de rien.
    unknown = [("bigquery", "ventes.inconnue"), (ORDERS.namespace, ORDERS.name)]
    assert named(await zone_of(engine, unknown)) == [(DAILY.name, 1), (REVENUE.name, 2)]
    assert await zone_of(engine, [("bigquery", "ventes.inconnue")]) == Zone((), truncated=False)


async def test_a_derivation_beyond_its_deadline_is_interrupted(engine: AsyncEngine) -> None:
    await record(engine, job([ORDERS], [DAILY]))
    async with reading(engine, timeout=-1.0) as reader:
        with pytest.raises(ReadTimedOut):
            await derive(reader, PERIMETER, known_at=None)
