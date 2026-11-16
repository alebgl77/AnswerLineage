"""Le graphe relie le jeu de données au destinataire."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.store.graph import (
    AgentRef,
    Answer,
    DatasetRef,
    Delivery,
    Read,
    RunRecord,
    ToolRef,
    Write,
    record_run,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
TAUX = DatasetRef("bigquery", "finance.ref.taux_change")
VENTES = DatasetRef("bigquery", "finance.ventes.commandes")

# Du jeu de données au destinataire, en ne suivant que les arêtes du graphe.
_REACHED = text(
    """
    SELECT d.name AS dataset, rc.pseudonym
    FROM dataset d
    JOIN read_edge re ON re.dataset_id = d.id
    JOIN run r ON r.run_id = re.run_id
    JOIN answer a ON a.run_id = r.run_id
    JOIN delivery dl ON dl.answer_id = a.id
    JOIN delivery_recipient dr ON dr.delivery_id = dl.id
    JOIN recipient rc ON rc.id = dr.recipient_id
    ORDER BY 1, 2
    """
)


def run_with_two_reads_and_an_answer(run_id: UUID) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        kind="agent",
        state="COMPLETE",
        event_time=T0,
        agent=AgentRef("agent://ventes", "analyste-ventes"),
        reads=(
            Read(TAUX, precision="columns", columns=("devise", "taux")),
            Read(VENTES),
        ),
        answers=(
            Answer(
                fingerprint="hmac:9f2c",
                deliveries=(
                    Delivery("slack", "C042/1731747600.000200", ("pseudo:a1", "pseudo:b2")),
                ),
            ),
        ),
    )


async def record(engine: AsyncEngine, *records: RunRecord) -> None:
    async with engine.begin() as conn:
        for item in records:
            await record_run(conn, item)


async def rows(engine: AsyncEngine, sql: str) -> list[tuple[Any, ...]]:
    async with engine.connect() as conn:
        return [tuple(row) for row in await conn.execute(text(sql))]


async def counts(engine: AsyncEngine) -> dict[str, int]:
    tables = (
        "agent",
        "tool",
        "dataset",
        "dataset_version",
        "run",
        "read_edge",
        "write_edge",
        "answer",
        "delivery",
        "recipient",
        "delivery_recipient",
    )
    result = {}
    for table in tables:
        result[table] = (await rows(engine, f"SELECT count(*) FROM {table}"))[0][0]  # noqa: S608
    return result


async def test_edges_link_the_dataset_to_the_recipient(engine: AsyncEngine) -> None:
    await record(engine, run_with_two_reads_and_an_answer(uuid4()))

    async with engine.connect() as conn:
        reached = [tuple(row) for row in await conn.execute(_REACHED)]
    assert reached == [
        ("finance.ref.taux_change", "pseudo:a1"),
        ("finance.ref.taux_change", "pseudo:b2"),
        ("finance.ventes.commandes", "pseudo:a1"),
        ("finance.ventes.commandes", "pseudo:b2"),
    ]


async def test_recording_the_same_run_twice_changes_nothing(engine: AsyncEngine) -> None:
    item = run_with_two_reads_and_an_answer(uuid4())
    await record(engine, item)
    first = await counts(engine)
    await record(engine, item, item)
    assert await counts(engine) == first
    assert first["read_edge"] == 2
    assert first["delivery_recipient"] == 2


async def test_edges_carry_event_time_and_recorded_time(engine: AsyncEngine) -> None:
    """Sans heure de réception connue, l'enregistrement date de la transaction."""
    await record(engine, run_with_two_reads_and_an_answer(uuid4()))
    for table in ("read_edge", "answer", "delivery", "delivery_recipient"):
        ((event_time, recorded_at),) = await rows(
            engine,
            f"SELECT DISTINCT event_time, recorded_at FROM {table}",  # noqa: S608
        )
        assert event_time == T0
        assert recorded_at > T0
        assert datetime.now(UTC) - recorded_at < timedelta(minutes=5)


async def test_events_of_a_run_commute(engine: AsyncEngine) -> None:
    """Dans l'ordre ou dans le désordre, l'état final du run est le même."""
    in_order, reversed_order = uuid4(), uuid4()
    tool = ToolRef("mcp://entrepot", "execute_sql")

    def events(run_id: UUID) -> list[RunRecord]:
        return [
            RunRecord(run_id, "job", "START", T0, trace_id="0af7651916cd43dd8448eb211c80319c"),
            RunRecord(
                run_id,
                "tool",
                "COMPLETE",
                T0 + timedelta(seconds=2),
                tool=tool,
                parent_run_id=UUID(int=7),
                reads=(Read(TAUX),),
            ),
        ]

    await record(engine, *events(in_order))
    await record(engine, *reversed(events(reversed_order)))

    result = await rows(
        engine,
        "SELECT kind, state, started_at, ended_at, state_time, trace_id, parent_run_id, tool_id "
        "FROM run ORDER BY run_id",
    )
    assert len(result) == 2
    assert result[0] == result[1]
    kind, state, started_at, ended_at, state_time, trace_id, parent_run_id, tool_id = result[0]
    assert (kind, state) == ("tool", "COMPLETE")
    assert started_at == T0
    assert ended_at == state_time == T0 + timedelta(seconds=2)
    assert trace_id == "0af7651916cd43dd8448eb211c80319c"
    assert parent_run_id == UUID(int=7)
    assert tool_id is not None


# Réceptions des événements par AnswerLineage : l'heure d'enregistrement du graphe.
R1 = datetime(2026, 11, 2, 9, 5, tzinfo=UTC)
R2 = R1 + timedelta(minutes=3)
NOTES = DatasetRef("memory://analyste-ventes", "notes", kind="memory", version="7")
TRUNCATE_GRAPH = text(
    "TRUNCATE delivery_recipient, recipient, delivery, answer, write_edge, read_edge, run,"
    " dataset_version, dataset, tool, agent RESTART IDENTITY CASCADE"
)

# Chaque ligne datée du graphe, désignée par ses clés naturelles, heures comprises.
_DATED = {
    "run": """
        SELECT r.run_id, r.kind, r.state, r.started_at, r.ended_at, r.state_time,
               r.parent_run_id, g.name, t.name, r.recorded_at
        FROM run r LEFT JOIN agent g ON g.id = r.agent_id LEFT JOIN tool t ON t.id = r.tool_id
        ORDER BY r.run_id""",
    "read_edge": """
        SELECT e.run_id, d.name, v.version, e.precision, e.columns, e.origin,
               e.event_time, e.last_event_time, e.recorded_at
        FROM read_edge e JOIN dataset d ON d.id = e.dataset_id
        LEFT JOIN dataset_version v ON v.id = e.dataset_version_id
        ORDER BY 1, 2, 3""",
    "write_edge": """
        SELECT e.run_id, d.name, v.version, e.event_time, e.recorded_at
        FROM write_edge e JOIN dataset d ON d.id = e.dataset_id
        LEFT JOIN dataset_version v ON v.id = e.dataset_version_id
        ORDER BY 1, 2, 3""",
    "answer": "SELECT run_id, fingerprint, event_time, recorded_at FROM answer ORDER BY 1, 2",
    "delivery": """
        SELECT a.fingerprint, d.channel, d.message_ref, d.event_time, d.recorded_at
        FROM delivery d JOIN answer a ON a.id = d.answer_id ORDER BY 1, 2, 3""",
    "delivery_recipient": """
        SELECT a.fingerprint, d.channel, c.pseudonym, l.event_time, l.recorded_at
        FROM delivery_recipient l JOIN delivery d ON d.id = l.delivery_id
        JOIN answer a ON a.id = d.answer_id JOIN recipient c ON c.id = l.recipient_id
        ORDER BY 1, 2, 3""",
    "agent": "SELECT namespace, name, recorded_at FROM agent ORDER BY 1, 2",
    "tool": "SELECT server, name, recorded_at FROM tool ORDER BY 1, 2",
    "dataset": "SELECT namespace, name, kind, recorded_at FROM dataset ORDER BY 1, 2",
    "dataset_version": """
        SELECT d.name, v.version, v.recorded_at
        FROM dataset_version v JOIN dataset d ON d.id = v.dataset_id ORDER BY 1, 2""",
    "recipient": "SELECT pseudonym, recorded_at FROM recipient ORDER BY 1",
}


async def dated_rows(engine: AsyncEngine) -> dict[str, list[tuple[Any, ...]]]:
    return {table: await rows(engine, sql) for table, sql in _DATED.items()}


def two_events_of_one_run(run_id: UUID) -> list[RunRecord]:
    """Le début et la fin d'un run d'agent, reçus à deux heures différentes."""
    delivery = Delivery("slack", "C042/1731747600.000200", ("pseudo:a1",))
    return [
        RunRecord(
            run_id,
            "agent",
            "START",
            T0,
            agent=AgentRef("agent://ventes", "analyste-ventes"),
            recorded_at=R1,
            reads=(Read(TAUX, precision="columns", columns=("devise",)),),
            writes=(Write(NOTES),),
            answers=(Answer("hmac:9f2c", deliveries=(delivery,)),),
        ),
        RunRecord(
            run_id,
            "agent",
            "COMPLETE",
            T0 + timedelta(seconds=5),
            agent=AgentRef("agent://ventes", "analyste-ventes"),
            recorded_at=R2,
            reads=(Read(TAUX, precision="columns", columns=("taux",)), Read(VENTES)),
            writes=(Write(NOTES),),
            answers=(
                Answer(
                    "hmac:9f2c",
                    deliveries=(
                        Delivery("slack", "C042/1731747600.000200", ("pseudo:a1", "pseudo:b2")),
                    ),
                ),
            ),
        ),
    ]


async def test_two_events_of_a_run_received_out_of_order_give_the_same_dated_rows(
    engine: AsyncEngine,
) -> None:
    """Toutes les lignes datées, comparées une à une, heures d'enregistrement comprises."""
    run_id = uuid4()
    await record(engine, *two_events_of_one_run(run_id))
    in_order = await dated_rows(engine)
    async with engine.begin() as conn:
        await conn.execute(TRUNCATE_GRAPH)
    await record(engine, *reversed(two_events_of_one_run(run_id)))
    assert await dated_rows(engine) == in_order

    later = T0 + timedelta(seconds=5)
    assert in_order["read_edge"] == [
        (run_id, "finance.ref.taux_change", None, "columns", ["devise", "taux"], "analysis",
         T0, later, R1),
        (run_id, "finance.ventes.commandes", None, "table", [], "analysis", later, later, R2),
    ]  # fmt: skip
    assert in_order["delivery_recipient"] == [
        ("hmac:9f2c", "slack", "pseudo:a1", T0, R1),
        ("hmac:9f2c", "slack", "pseudo:b2", later, R2),
    ]
    assert in_order["write_edge"] == [(run_id, "notes", "7", T0, R1)]
    assert in_order["answer"] == [(run_id, "hmac:9f2c", T0, R1)]
    assert in_order["delivery"][0][3:] == (T0, R1)
    assert in_order["run"][0][3:] == (T0, later, later, None, "analyste-ventes", None, R1)
    assert in_order["dataset"] == [
        ("bigquery", "finance.ref.taux_change", "table", R1),
        ("bigquery", "finance.ventes.commandes", "table", R2),
        ("memory://analyste-ventes", "notes", "memory", R1),
    ]
    assert in_order["dataset_version"] == [("notes", "7", R1)]
    assert in_order["recipient"] == [("pseudo:a1", R1), ("pseudo:b2", R2)]
    assert in_order["agent"] == [("agent://ventes", "analyste-ventes", R1)]


async def test_an_event_projected_again_later_changes_no_recorded_time(
    engine: AsyncEngine,
) -> None:
    """Rejouer un événement, ou le projeter plus tard, garde la réception d'origine."""
    first, second = two_events_of_one_run(uuid4())
    await record(engine, first, second)
    before = await dated_rows(engine)
    await record(engine, second, first, second)
    assert await dated_rows(engine) == before
    assert {row[-1] for row in before["read_edge"]} == {R1, R2}


async def test_the_reception_of_a_shared_node_is_the_earliest_known(engine: AsyncEngine) -> None:
    """Un jeu de données, un outil ou un destinataire déjà connus : la plus ancienne réception."""
    tool = ToolRef("entrepot", "execute_sql")
    late = RunRecord(
        uuid4(), "tool", "COMPLETE", T0, tool=tool, recorded_at=R2, reads=(Read(TAUX),)
    )
    early = RunRecord(
        uuid4(), "tool", "COMPLETE", T0, tool=tool, recorded_at=R1, reads=(Read(TAUX),)
    )
    await record(engine, late, early)
    assert await rows(engine, "SELECT recorded_at FROM dataset") == [(R1,)]
    assert await rows(engine, "SELECT recorded_at FROM tool") == [(R1,)]
    await record(engine, late)
    assert await rows(engine, "SELECT recorded_at FROM dataset") == [(R1,)]


async def test_a_backdated_event_widens_the_read_instead_of_moving_it(engine: AsyncEngine) -> None:
    """Un rejeu antidaté, dans un ordre ou dans l'autre : la lecture garde ses deux bornes."""
    honest, backdated = uuid4(), uuid4()

    def reads(run_id: UUID) -> list[RunRecord]:
        return [
            RunRecord(run_id, "tool", "COMPLETE", T0, recorded_at=R1, reads=(Read(TAUX),)),
            RunRecord(
                run_id, "tool", "COMPLETE", T0 - timedelta(days=1), recorded_at=R2,
                reads=(Read(TAUX),),
            ),
        ]  # fmt: skip

    await record(engine, *reads(honest))
    await record(engine, *reversed(reads(backdated)))
    assert await rows(
        engine, "SELECT DISTINCT event_time, last_event_time, recorded_at FROM read_edge"
    ) == [(T0 - timedelta(days=1), T0, R1)]


async def test_terminal_state_wins_when_two_events_share_a_timestamp(engine: AsyncEngine) -> None:
    first, second = uuid4(), uuid4()
    await record(
        engine, RunRecord(first, "tool", "START", T0), RunRecord(first, "tool", "FAIL", T0)
    )
    await record(
        engine, RunRecord(second, "tool", "FAIL", T0), RunRecord(second, "tool", "START", T0)
    )
    assert await rows(engine, "SELECT DISTINCT state FROM run") == [("FAIL",)]


async def test_two_reads_of_a_dataset_by_a_run_merge_into_one_edge(engine: AsyncEngine) -> None:
    run_id = uuid4()
    await record(
        engine,
        RunRecord(run_id, "tool", "START", T0 + timedelta(seconds=1), reads=(Read(TAUX),)),
        RunRecord(
            run_id,
            "tool",
            "COMPLETE",
            T0 + timedelta(seconds=3),
            reads=(Read(TAUX, precision="columns", columns=("taux", "devise")),),
        ),
        RunRecord(
            run_id,
            "tool",
            "COMPLETE",
            T0,
            reads=(Read(TAUX, precision="columns", columns=("date",), origin="reconciliation"),),
        ),
    )
    assert await rows(engine, "SELECT precision, columns, origin, event_time FROM read_edge") == [
        ("columns", ["date", "devise", "taux"], "reconciliation", T0)
    ]


async def test_a_coarser_later_read_does_not_degrade_the_edge(engine: AsyncEngine) -> None:
    run_id = uuid4()
    await record(
        engine,
        RunRecord(
            run_id, "tool", "START", T0, reads=(Read(TAUX, precision="columns", columns=("taux",)),)
        ),
        RunRecord(run_id, "tool", "COMPLETE", T0 + timedelta(seconds=1), reads=(Read(TAUX),)),
    )
    assert await rows(engine, "SELECT precision, columns, origin FROM read_edge") == [
        ("columns", ["taux"], "analysis")
    ]


async def test_versions_are_separate_edges_of_one_dataset(engine: AsyncEngine) -> None:
    run_id = uuid4()
    v1 = DatasetRef("iceberg", "ref.tarifs", version="snapshot-1")
    v2 = DatasetRef("iceberg", "ref.tarifs", version="snapshot-2")
    memory = DatasetRef("memory://analyste-ventes", "notes", kind="memory", version="42")
    await record(
        engine,
        RunRecord(
            run_id,
            "agent",
            "COMPLETE",
            T0,
            reads=(Read(v1), Read(v2), Read(v1)),
            writes=(Write(memory), Write(memory)),
        ),
    )
    result = await counts(engine)
    assert result["dataset"] == 2
    assert result["dataset_version"] == 3
    assert result["read_edge"] == 2
    assert result["write_edge"] == 1
    assert await rows(engine, "SELECT kind FROM dataset WHERE name = 'notes'") == [("memory",)]


async def test_concurrent_writers_share_new_datasets_without_error(engine: AsyncEngine) -> None:
    async def writer(index: int) -> None:
        async with engine.begin() as conn:
            await record_run(
                conn,
                RunRecord(
                    uuid4(),
                    "tool",
                    "COMPLETE",
                    T0 + timedelta(seconds=index),
                    tool=ToolRef("mcp://entrepot", "execute_sql"),
                    reads=(Read(TAUX), Read(VENTES)),
                ),
            )

    await asyncio.gather(*(writer(index) for index in range(8)))
    result = await counts(engine)
    assert (result["dataset"], result["tool"]) == (2, 1)
    assert (result["run"], result["read_edge"]) == (8, 16)


async def test_naive_event_time_is_refused(engine: AsyncEngine) -> None:
    naive = datetime(2026, 11, 2, 9, 0)
    with pytest.raises(ValueError, match="fuseau"):
        async with engine.begin() as conn:
            await record_run(conn, RunRecord(uuid4(), "agent", "START", naive))
