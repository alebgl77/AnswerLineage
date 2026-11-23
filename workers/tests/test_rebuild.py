"""La reconstruction du graphe : vidé, puis rejoué depuis les événements bruts, à l'identique.

« Identique » se vérifie ligne à ligne sur toutes les tables du graphe, heures
d'événement et d'enregistrement comprises (``graph_rows``). Seuls en sont exclus
les identifiants numériques tirés à l'écriture : ils changent d'une écriture à
l'autre, rien hors du graphe ne les cite et l'API ne les rend jamais. Une clé
étrangère qui en porte un est remplacée par la clé naturelle de la ligne visée :
le jeu de données par son espace de noms et son nom, la réponse par son run et
son empreinte, etc.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Table, select, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from answerlineage_api.incidents.models import validate_declaration
from answerlineage_api.incidents.store import declare
from answerlineage_api.ingest.store import event_identifier
from answerlineage_api.store import schema
from answerlineage_workers import cli
from answerlineage_workers.incidents import process_analysis
from answerlineage_workers.queue import BatchResult, process_batch, record_event
from answerlineage_workers.rebuild import (
    REBUILD_LOCK_KEY,
    Assessment,
    RebuildRefusedError,
    RebuildReport,
    assess,
    rebuild_graph,
)
from workers.tests.test_queue import ingest, scalar

pytestmark = pytest.mark.anyio
integration = pytest.mark.integration

PRODUCER = "https://example.com/p"
SCHEMA_URL = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"
# Le nom d'une table lue : un contenu, qui ne doit apparaître dans aucun journal.
CANARY = "canari7d1e"
AGENT_RUN = UUID("0193a9c5-7a10-7c3e-9d5e-2f1b6c8affff")


# Le graphe, ligne à ligne, et le reste de la base.

GRAPH_ROWS: dict[str, tuple[str, tuple[str, ...]]] = {
    "agent": (
        "SELECT namespace, name, recorded_at FROM agent ORDER BY 1, 2",
        ("namespace", "name", "recorded_at"),
    ),
    "tool": (
        "SELECT server, name, recorded_at FROM tool ORDER BY 1, 2",
        ("server", "name", "recorded_at"),
    ),
    "dataset": (
        "SELECT namespace, name, kind, recorded_at FROM dataset ORDER BY 1, 2",
        ("namespace", "name", "kind", "recorded_at"),
    ),
    "dataset_version": (
        "SELECT d.namespace, d.name, v.version, v.recorded_at"
        " FROM dataset_version v JOIN dataset d ON d.id = v.dataset_id ORDER BY 1, 2, 3",
        ("dataset_id", "version", "recorded_at"),
    ),
    "run": (
        "SELECT r.run_id, r.kind, g.namespace, g.name, t.server, t.name, r.parent_run_id,"
        " r.trace_id, r.span_id, r.started_at, r.ended_at, r.state, r.state_time, r.recorded_at"
        " FROM run r LEFT JOIN agent g ON g.id = r.agent_id LEFT JOIN tool t ON t.id = r.tool_id"
        " ORDER BY r.run_id",
        (
            "run_id",
            "kind",
            "agent_id",
            "tool_id",
            "parent_run_id",
            "trace_id",
            "span_id",
            "started_at",
            "ended_at",
            "state",
            "state_time",
            "recorded_at",
        ),
    ),
    "read_edge": (
        "SELECT e.run_id, d.namespace, d.name, v.version, e.precision, e.columns, e.origin,"
        " e.event_time, e.last_event_time, e.recorded_at"
        " FROM read_edge e JOIN dataset d ON d.id = e.dataset_id"
        " LEFT JOIN dataset_version v ON v.id = e.dataset_version_id"
        " ORDER BY 1, 2, 3, 4 NULLS FIRST",
        (
            "run_id",
            "dataset_id",
            "dataset_version_id",
            "precision",
            "columns",
            "origin",
            "event_time",
            "last_event_time",
            "recorded_at",
        ),
    ),
    "write_edge": (
        "SELECT e.run_id, d.namespace, d.name, v.version, e.event_time, e.recorded_at"
        " FROM write_edge e JOIN dataset d ON d.id = e.dataset_id"
        " LEFT JOIN dataset_version v ON v.id = e.dataset_version_id"
        " ORDER BY 1, 2, 3, 4 NULLS FIRST",
        ("run_id", "dataset_id", "dataset_version_id", "event_time", "recorded_at"),
    ),
    "answer": (
        "SELECT run_id, fingerprint, event_time, recorded_at FROM answer ORDER BY 1, 2",
        ("run_id", "fingerprint", "event_time", "recorded_at"),
    ),
    "delivery": (
        "SELECT a.run_id, a.fingerprint, d.channel, d.message_ref, d.event_time, d.recorded_at"
        " FROM delivery d JOIN answer a ON a.id = d.answer_id ORDER BY 1, 2, 3, 4",
        ("answer_id", "channel", "message_ref", "event_time", "recorded_at"),
    ),
    "recipient": (
        "SELECT pseudonym, recorded_at FROM recipient ORDER BY 1",
        ("pseudonym", "recorded_at"),
    ),
    "delivery_recipient": (
        "SELECT a.run_id, a.fingerprint, d.channel, d.message_ref, c.pseudonym, l.event_time,"
        " l.recorded_at FROM delivery_recipient l JOIN delivery d ON d.id = l.delivery_id"
        " JOIN answer a ON a.id = d.answer_id JOIN recipient c ON c.id = l.recipient_id"
        " ORDER BY 1, 2, 3, 4, 5",
        ("delivery_id", "recipient_id", "event_time", "recorded_at"),
    ),
}

Rows = dict[str, list[tuple[Any, ...]]]


async def graph_rows(engine: AsyncEngine) -> Rows:
    """Toutes les lignes des tables du graphe, sans les identifiants tirés à l'écriture."""
    async with engine.connect() as conn:
        return {
            name: [tuple(row) for row in await conn.execute(text(sql))]
            for name, (sql, _) in GRAPH_ROWS.items()
        }


def _outside_the_graph() -> list[Table]:
    return [
        table
        for table in schema.metadata.sorted_tables
        if table not in schema.GRAPH and table is not schema.graph_rebuild
    ]


async def other_rows(engine: AsyncEngine) -> Rows:
    """Toutes les lignes des autres tables, sauf la trace des reconstructions."""
    async with engine.connect() as conn:
        return {
            table.name: [tuple(row) for row in await conn.execute(select(table).order_by(*table.c))]
            for table in _outside_the_graph()
        }


async def rows(engine: AsyncEngine, sql: str, **params: Any) -> list[tuple[Any, ...]]:
    async with engine.connect() as conn:
        return [tuple(row) for row in await conn.execute(text(sql), params)]


async def drain(engine: AsyncEngine) -> None:
    """Un worker vide la file : ce qui est remis à plus tard est repris aussitôt."""
    while await scalar(engine, "SELECT count(*) FROM ingest_queue"):
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE ingest_queue SET available_at = now()"))
        await process_batch(engine)


# Les événements d'une petite entreprise.


def _facet(**fields: Any) -> dict[str, Any]:
    return {"_producer": PRODUCER, "_schemaURL": "https://example.com/s", **fields}


def _event(kind: str, at: str, run: dict[str, Any], job: dict[str, str]) -> dict[str, Any]:
    return {
        "eventType": kind,
        "eventTime": f"2026-11-02T{at}Z",
        "producer": PRODUCER,
        "schemaURL": SCHEMA_URL,
        "run": run,
        "job": job,
        "inputs": [],
        "outputs": [],
    }


def call(
    index: int,
    at: str,
    *tables: str,
    kind: str = "COMPLETE",
    columns: tuple[str, ...] = (),
    version: str | None = None,
    writes: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Un appel d'outil du run de l'agent, qui lit et écrit des tables."""
    facets: dict[str, Any] = {}
    if columns:
        facets["schema"] = _facet(fields=[{"name": column} for column in columns])
    if version is not None:
        facets["version"] = _facet(datasetVersion=version)
    event = _event(
        kind,
        at,
        {
            "runId": str(UUID(int=0x0193A9C57A107C3E9D5E2F1B6C8A0000 + index)),
            "facets": {
                "parent": _facet(
                    run={"runId": str(AGENT_RUN)},
                    job={"namespace": "agent://ventes", "name": "assistant"},
                ),
                "answerlineage_toolCall": _facet(server="entrepot", tool="execute_sql"),
            },
        },
        {"namespace": "mcp://entrepot", "name": "execute_sql"},
    )
    event["inputs"] = [
        {"namespace": "bigquery", "name": table, "facets": facets} for table in tables
    ]
    event["outputs"] = [{"namespace": "bigquery", "name": table} for table in writes]
    return event


def answered(kind: str, at: str, *recipients: str, rank: int = 0) -> dict[str, Any]:
    """Un événement du run de l'agent ; terminé, il porte la réponse et sa diffusion."""
    event = _event(
        kind,
        at,
        {"runId": str(AGENT_RUN), "facets": {"answerlineage_agent": _facet()}},
        {"namespace": "agent://ventes", "name": "assistant"},
    )
    if recipients:
        event["outputs"] = [
            {
                "namespace": "agent://ventes",
                "name": "assistant/reponses",
                "outputFacets": {
                    "answerlineage_delivery": _facet(
                        fingerprint=f"sha256:{rank:064x}",
                        channel="slack",
                        messageRef=f"C042/{rank}",
                        recipients=list(recipients),
                    )
                },
            }
        ]
    return event


def history() -> list[list[dict[str, Any]]]:
    """Trois envois successifs, reçus à trois heures différentes.

    Le deuxième rejoue une lecture antidatée avec une colonne de plus : il élargit
    son intervalle et ses colonnes, sans changer sa première réception.
    """
    return [
        [
            answered("START", "09:00:00"),
            call(0, "09:00:01", "ventes.commandes", columns=("devise", "taux"), version="v1"),
            call(1, "09:00:02", f"ventes.{CANARY}", writes=("ventes.syntheses",)),
        ],
        [
            call(
                0,
                "08:59:59",
                "ventes.commandes",
                columns=("montant",),
                version="v1",
                kind="RUNNING",
            ),
            answered("COMPLETE", "09:00:05", "pseudo:a", "pseudo:b"),
        ],
        [call(2, "09:01:00", "finance.taux"), answered("COMPLETE", "09:00:05", "pseudo:c", rank=1)],
    ]


async def received(engine: AsyncEngine) -> None:
    """La petite entreprise arrive, envoi par envoi, et un worker la projette."""
    for sent in history():
        await ingest(engine, sent)
        await drain(engine)


async def poisoned(engine: AsyncEngine) -> str:
    """Un événement enregistré avant le contrôle des identifiants : la projection le refuse."""
    payload = call(9, "09:02:00", "ventes.commandes")
    payload["job"]["namespace"] = "mcp://<entrepot>"
    event_id = event_identifier(payload)
    async with engine.begin() as conn:
        month = (
            await conn.execute(
                schema.event_raw.insert()
                .values(
                    event_id=event_id,
                    run_id=UUID(payload["run"]["runId"]),
                    event_type="COMPLETE",
                    event_time=datetime.fromisoformat(payload["eventTime"]),
                    producer=PRODUCER,
                    payload=payload,
                    emitter="mcp://<entrepot>",
                    tool_call=True,
                )
                .returning(schema.event_raw.c.recorded_month)
            )
        ).scalar_one()
        await conn.execute(
            schema.ingest_queue.insert().values(event_id=event_id, recorded_month=month)
        )
    await drain(engine)
    return event_id


async def reference(engine: AsyncEngine) -> tuple[Rows, Rows]:
    """Le graphe que les workers ont bâti, et le reste de la base."""
    graph = await graph_rows(engine)
    assert all(graph.values()), "chaque table du graphe a des lignes"
    return graph, await other_rows(engine)


# La comparaison n'oublie rien.


def test_the_comparison_reads_every_column_of_every_graph_table() -> None:
    assert set(GRAPH_ROWS) == {table.name for table in schema.GRAPH}
    for table in schema.GRAPH:
        generated = {column.name for column in table.c if column.identity is not None}
        _, covered = GRAPH_ROWS[table.name]
        assert set(covered) | generated == set(table.c.keys()), table.name
        assert not set(covered) & generated, table.name


@integration
async def test_the_projection_writes_the_graph_tables_and_nothing_else(
    engine: AsyncEngine,
) -> None:
    """Les tables que vide la reconstruction sont exactement celles qu'écrit la projection."""
    for sent in history():
        await ingest(engine, sent)
    before = await other_rows(engine)
    await drain(engine)

    after = await other_rows(engine)
    changed = {name for name in before if before[name] != after[name]}
    assert changed == {"ingest_queue"}
    assert all((await graph_rows(engine)).values())


# Identique à l'original.


@integration
async def test_the_rebuilt_graph_is_the_one_the_workers_built(engine: AsyncEngine) -> None:
    await received(engine)
    refused = await poisoned(engine)
    graph, others = await reference(engine)

    report = await rebuild_graph(engine)

    assert await graph_rows(engine) == graph
    # Le reste de la base n'a pas changé : file vide, rebut tel quel, avec son motif.
    assert await other_rows(engine) == others
    assert await rows(engine, "SELECT event_id, reason FROM dead_letter") == [
        (refused, "invalid_identifier")
    ]
    first, last = (await rows(engine, "SELECT min(recorded_at), max(recorded_at) FROM event_raw"))[
        0
    ]
    assert report.finished and not report.resumed
    assert (report.replayed, report.dead_letters, report.buried, report.remaining) == (7, 1, 0, 0)
    assert (report.first_received, report.last_received, report.unrecoverable) == (first, last, 0)
    assert await rows(
        engine, "SELECT replayed, dead_letters, finished_at IS NOT NULL FROM graph_rebuild"
    ) == [(7, 1, True)]


@integration
async def test_a_corrupted_graph_is_repaired(engine: AsyncEngine) -> None:
    await received(engine)
    graph, _ = await reference(engine)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM delivery_recipient"))
        await conn.execute(text("DELETE FROM write_edge"))
        await conn.execute(text("UPDATE read_edge SET columns = '{}', precision = 'table'"))
        await conn.execute(text("UPDATE run SET state = 'FAIL', recorded_at = now()"))
        await conn.execute(text("UPDATE dataset SET kind = 'memory'"))
    assert await graph_rows(engine) != graph

    await rebuild_graph(engine)

    assert await graph_rows(engine) == graph


@integration
async def test_a_rebuild_on_an_empty_base_leaves_an_empty_graph(engine: AsyncEngine) -> None:
    report = await rebuild_graph(engine)

    assert report.finished and report.replayed == 0
    assert (report.first_received, report.last_received) == (None, None)
    assert "le graphe reconstruit est vide" in " ".join(report.lines())
    assert not any((await graph_rows(engine)).values())


# Ce que la conservation a purgé.


@integration
async def test_rows_older_than_the_conserved_events_stop_the_rebuild_unless_lost_knowingly(
    engine: AsyncEngine,
) -> None:
    await received(engine)
    # La conservation a purgé le premier envoi : ses événements ne sont plus rejouables.
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "DELETE FROM event_raw WHERE recorded_at = (SELECT min(recorded_at) FROM event_raw)"
            )
        )
    graph, others = await reference(engine)
    first = (await rows(engine, "SELECT min(recorded_at) FROM event_raw"))[0][0]
    older = sum(len([row for row in table if row[-1] < first]) for table in graph.values())
    assert older > 0

    found = await assess(engine)
    assert (found.unrecoverable, found.first_received) == (older, first)
    assert any("--accept-loss" in line for line in found.lines())
    with pytest.raises(RebuildRefusedError, match="--accept-loss"):
        await rebuild_graph(engine)
    # Refusée, la reconstruction n'a rien changé.
    assert await graph_rows(engine) == graph
    assert await other_rows(engine) == others
    assert await scalar(engine, "SELECT count(*) FROM graph_rebuild") == 0

    report = await rebuild_graph(engine, accept_loss=True)

    assert report.finished and (report.unrecoverable, report.first_received) == (older, first)
    assert any("ne sont pas rendues telles quelles" in line for line in report.lines())
    rebuilt = await graph_rows(engine)
    # Ce qui reste vient des événements conservés : rien n'est enregistré avant eux.
    assert all(row[-1] >= first for table in rebuilt.values() for row in table)
    assert rebuilt["run"] and rebuilt != graph


# Une reconstruction à la fois, et les workers à l'écart.


@integration
async def test_a_second_rebuild_is_refused_while_one_runs(engine: AsyncEngine) -> None:
    await received(engine)
    graph, _ = await reference(engine)
    async with engine.connect() as holder:
        await holder.execute(text("SELECT pg_advisory_lock(:key)"), {"key": REBUILD_LOCK_KEY})
        with pytest.raises(RebuildRefusedError, match="Une autre reconstruction"):
            await rebuild_graph(engine)
        await holder.execute(text("SELECT pg_advisory_unlock_all()"))
        await holder.commit()
    assert await graph_rows(engine) == graph
    assert await scalar(engine, "SELECT count(*) FROM graph_rebuild") == 0


@integration
async def test_workers_stand_aside_while_the_rebuild_projects(engine: AsyncEngine) -> None:
    await received(engine)
    graph, _ = await reference(engine)
    await declared(engine)
    seen: list[object] = []

    async def watched(
        conn: AsyncConnection, payload: Mapping[str, Any], received_at: datetime
    ) -> None:
        if not seen:
            seen.append(await process_batch(engine))
            seen.append(await process_analysis(engine))
        await record_event(conn, payload, received_at)

    # Un événement par lot : le reste de la file serait à prendre, sans la barrière.
    await rebuild_graph(engine, handler=watched, batch_size=1)

    assert seen == [BatchResult(), None]
    assert await graph_rows(engine) == graph


@integration
async def test_the_rebuild_lets_a_batch_in_progress_finish_before_emptying_the_graph(
    engine: AsyncEngine,
) -> None:
    for sent in history():
        await ingest(engine, sent)
    began = asyncio.Event()

    async def slow(
        conn: AsyncConnection, payload: Mapping[str, Any], received_at: datetime
    ) -> None:
        began.set()
        await record_event(conn, payload, received_at)
        await asyncio.sleep(0.05)

    async def rebuild_once_the_batch_began() -> RebuildReport:
        await began.wait()
        return await rebuild_graph(engine)

    batch, report = await asyncio.gather(
        process_batch(engine, handler=slow), rebuild_once_the_batch_began()
    )

    assert batch.processed == 7 and report.finished
    # Le lot a fini avant que le graphe soit vidé : la reconstruction a rejoué ses événements.
    assert report.replayed == 7
    await drain(engine)
    graph = await graph_rows(engine)
    await rebuild_graph(engine)
    assert await graph_rows(engine) == graph


# Ce que la projection refuse pendant la reconstruction.


def refusing(run_id: str, error: Exception, times: int) -> Any:
    """La projection échoue ``times`` fois sur les événements de ce run, puis les accepte."""
    failures = 0

    async def handler(
        conn: AsyncConnection, payload: Mapping[str, Any], received_at: datetime
    ) -> None:
        nonlocal failures
        if payload["run"]["runId"] == run_id and failures < times:
            failures += 1
            raise error
        await record_event(conn, payload, received_at)

    return handler


@integration
async def test_an_event_the_projection_now_refuses_goes_to_the_dead_letters(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    await received(engine)
    refused = call(2, "09:01:00", "finance.taux")

    with caplog.at_level(logging.INFO, logger="answerlineage_workers"):
        report = await rebuild_graph(
            engine,
            batch_size=2,
            handler=refusing(refused["run"]["runId"], KeyError(CANARY), times=1),
            progress_every=timedelta(0),
        )

    assert report.finished and (report.replayed, report.buried) == (7, 1)
    assert "1 mis au rebut pendant la reconstruction" in report.lines()[0]
    assert await rows(engine, "SELECT event_id, reason FROM dead_letter") == [
        (event_identifier(refused), "invalid_event")
    ]
    assert await rows(
        engine, "SELECT count(*) FROM run WHERE run_id = :id", id=UUID(refused["run"]["runId"])
    ) == [(0,)]
    # La progression, à chaque lot : des nombres seulement.
    assert caplog.text.count("Reconstruction du graphe : ") >= 4
    assert CANARY not in caplog.text


@integration
async def test_the_rebuild_waits_for_an_event_put_off_until_it_is_projected(
    engine: AsyncEngine,
) -> None:
    await received(engine)
    graph, _ = await reference(engine)
    late = call(2, "09:01:00", "finance.taux")["run"]["runId"]

    report = await rebuild_graph(engine, handler=refusing(late, RuntimeError(), times=1))

    # Remis à plus tard, l'événement est repris à son heure ; la reconstruction l'attend.
    assert report.finished and (report.buried, report.remaining) == (0, 0)
    assert await graph_rows(engine) == graph


# Reprise après une interruption.


def stopping_after(count: int, stop: asyncio.Event) -> Any:
    handled = 0

    async def handler(
        conn: AsyncConnection, payload: Mapping[str, Any], received_at: datetime
    ) -> None:
        nonlocal handled
        await record_event(conn, payload, received_at)
        handled += 1
        if handled == count:
            stop.set()

    return handler


@integration
async def test_an_interrupted_rebuild_resumes_without_emptying_the_graph_again(
    engine: AsyncEngine,
) -> None:
    await received(engine)
    graph, others = await reference(engine)
    stop = asyncio.Event()

    interrupted = await rebuild_graph(
        engine, stop=stop, batch_size=1, handler=stopping_after(2, stop)
    )

    assert not interrupted.finished and interrupted.remaining == 5
    assert "Reconstruction interrompue : 5 à projeter" in interrupted.lines()[0]
    # Le point de reprise est dans la base : la file, et la reconstruction inachevée.
    assert await scalar(engine, "SELECT count(*) FROM ingest_queue") == 5
    assert await rows(engine, "SELECT finished_at FROM graph_rebuild") == [(None,)]
    found = await assess(engine)
    assert found.unfinished_since == interrupted.started_at
    assert any("--yes la reprend" in line for line in found.lines())

    resumed = await rebuild_graph(engine)

    assert resumed.finished and resumed.resumed
    assert (resumed.started_at, resumed.replayed) == (interrupted.started_at, 7)
    assert await scalar(engine, "SELECT count(*) FROM graph_rebuild") == 1
    assert await graph_rows(engine) == graph
    assert await other_rows(engine) == others


@integration
async def test_workers_finish_an_interrupted_rebuild_once_it_lets_go(engine: AsyncEngine) -> None:
    await received(engine)
    graph, _ = await reference(engine)
    stop = asyncio.Event()
    await rebuild_graph(engine, stop=stop, batch_size=2, handler=stopping_after(2, stop))

    await drain(engine)

    assert await graph_rows(engine) == graph
    # La reconstruction reste à clore : la commande suivante le fait, sans rien rejouer.
    finished = await rebuild_graph(engine)
    assert finished.finished and finished.resumed
    assert await graph_rows(engine) == graph


# Les analyses d'incident attendent un graphe entier.


async def declared(engine: AsyncEngine) -> UUID:
    document = {
        "scope": {"datasets": [{"namespace": "bigquery", "name": "ventes.commandes"}]},
        "window": {"from": "2026-11-02T08:00:00Z", "to": "2026-11-02T10:00:00Z"},
        "severity": "high",
    }
    declaration, errors = validate_declaration(document)
    assert declaration is not None, errors
    async with engine.begin() as conn:
        return (await declare(conn, declaration)).id


async def touched(engine: AsyncEngine, incident_id: UUID) -> list[tuple[Any, ...]]:
    return await rows(
        engine,
        "SELECT a.run_id, a.fingerprint, r.pseudonym FROM incident_answer a"
        " JOIN incident_recipient r USING (incident_id, run_id, fingerprint)"
        " WHERE a.incident_id = :id ORDER BY 1, 2, 3",
        id=incident_id,
    )


@integration
async def test_an_analysis_waits_until_the_rebuild_has_given_the_graph_back(
    engine: AsyncEngine,
) -> None:
    await received(engine)
    before = await declared(engine)
    assert await process_analysis(engine) is not None
    expected = await touched(engine, before)
    assert len(expected) == 3
    stop = asyncio.Event()
    await rebuild_graph(engine, stop=stop, batch_size=1, handler=stopping_after(1, stop))

    during = await declared(engine)
    # La barrière est rendue, mais la file n'a pas tout rendu au graphe : l'analyse attend.
    assert await process_analysis(engine) is None
    await drain(engine)
    processed = await process_analysis(engine)

    assert processed is not None and processed.incident_id == during
    assert await touched(engine, during) == expected


# La commande.


@pytest.fixture
def configured(database: URL, monkeypatch: pytest.MonkeyPatch) -> URL:
    monkeypatch.setenv("ANSWERLINEAGE_DATABASE_URL", database.render_as_string(hide_password=False))
    return database


@integration
async def test_without_yes_the_command_only_shows_what_it_would_do(
    engine: AsyncEngine, configured: URL, caplog: pytest.LogCaptureFixture
) -> None:
    await received(engine)
    await poisoned(engine)
    graph, others = await reference(engine)

    with caplog.at_level(logging.INFO, logger="answerlineage_workers"):
        assert await cli.rebuild(confirmed=False) == 1

    assert await graph_rows(engine) == graph
    assert await other_rows(engine) == others
    assert await scalar(engine, "SELECT count(*) FROM graph_rebuild") == 0
    graph_size = sum(len(table) for table in graph.values())
    assert f"Le graphe compte {graph_size} lignes." in caplog.text
    assert "8 événements bruts conservés" in caplog.text
    assert "sauf 1 au rebut" in caplog.text
    assert "Rien n'est fait sans --yes." in caplog.text
    assert CANARY not in caplog.text


@integration
async def test_the_command_logs_its_progress_and_result_in_numbers_only(
    engine: AsyncEngine, configured: URL, caplog: pytest.LogCaptureFixture
) -> None:
    await received(engine)
    await poisoned(engine)
    graph, _ = await reference(engine)

    with caplog.at_level(logging.INFO, logger="answerlineage_workers"):
        assert await cli.rebuild(confirmed=True, batch_size=2) == 0

    assert await graph_rows(engine) == graph
    assert "Reconstruction du graphe commencée : 7 événement(s) à rejouer" in caplog.text
    assert "Reconstruction du graphe : 7 événement(s) projeté(s), 0 à projeter" in caplog.text
    assert "Graphe reconstruit en " in caplog.text
    assert "7 événements rejoués, 1 au rebut laissés tels quels" in caplog.text
    assert "ce que la conservation a purgé avant n'est pas rendu" in caplog.text
    assert CANARY not in caplog.text


@integration
async def test_the_command_refuses_to_lose_rows_and_says_so(
    engine: AsyncEngine, configured: URL, caplog: pytest.LogCaptureFixture
) -> None:
    await received(engine)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM event_raw"))
    graph, _ = await reference(engine)

    with caplog.at_level(logging.INFO, logger="answerlineage_workers"):
        assert await cli.rebuild(confirmed=True) == 1
    assert await graph_rows(engine) == graph
    assert "sans --accept-loss. Rien n'a changé." in caplog.text

    assert await cli.rebuild(confirmed=True, accept_loss=True) == 0
    assert not any((await graph_rows(engine)).values())


@integration
async def test_an_interrupted_command_says_how_to_resume(
    engine: AsyncEngine, configured: URL, caplog: pytest.LogCaptureFixture
) -> None:
    await received(engine)
    stop = asyncio.Event()
    stop.set()

    with caplog.at_level(logging.INFO, logger="answerlineage_workers"):
        assert await cli.rebuild(confirmed=True, stop=stop) == 1

    assert "Reconstruction interrompue : 7 à projeter" in caplog.text
    assert "« answerlineage-worker rebuild --yes » reprend la reconstruction" in caplog.text


async def test_a_failing_rebuild_is_reported_by_the_class_of_its_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("ANSWERLINEAGE_DATABASE_URL", "postgresql://u@127.0.0.1:1/base")

    async def failing(*_: object, **__: object) -> RebuildReport:
        raise OperationalError("SELECT 1", {}, RuntimeError(f"connexion perdue : {CANARY}"))

    monkeypatch.setattr(cli, "rebuild_graph", failing)
    with caplog.at_level(logging.INFO, logger="answerlineage_workers"):
        assert await cli.rebuild(confirmed=True) == 1
    assert "Reconstruction du graphe en échec (RuntimeError)" in caplog.text
    assert CANARY not in caplog.text


def test_the_command_line_rebuilds_only_when_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    async def rebuild(**options: object) -> int:
        calls.append(options)
        return 0 if options["confirmed"] else 1

    monkeypatch.setattr(cli, "rebuild", rebuild)
    assert cli.main(["rebuild"]) == 1
    assert cli.main(["rebuild", "--yes", "--accept-loss", "--batch-size", "500"]) == 0
    assert calls == [
        {"confirmed": False, "accept_loss": False, "batch_size": 100, "reemit": False},
        {"confirmed": True, "accept_loss": True, "batch_size": 500, "reemit": False},
    ]
    # Avec une destination de réémission, ce qui n'était jamais entré au graphe partira.
    assert cli.main(["--reemit-url", "http://127.0.0.1:5000/api/v1/lineage", "rebuild"]) == 1
    assert calls[-1]["reemit"] is True


@pytest.mark.parametrize("value", ["0", "10001", "-1", "cent"])
def test_the_command_line_refuses_a_batch_size_out_of_bounds(value: str) -> None:
    with pytest.raises(SystemExit):
        cli.main(["rebuild", "--batch-size", value])


def test_the_command_line_reports_a_missing_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ANSWERLINEAGE_DATABASE_URL", raising=False)
    assert cli.main(["rebuild", "--yes"]) == 2
    assert "ANSWERLINEAGE_DATABASE_URL" in capsys.readouterr().err


async def test_a_batch_beyond_its_bounds_is_refused() -> None:
    with pytest.raises(ValueError, match="de 1 à 10000"):
        await rebuild_graph(None, batch_size=0)  # type: ignore[arg-type]


def test_an_assessment_without_events_says_the_graph_would_be_empty() -> None:
    found = Assessment(
        graph_rows=3,
        events=0,
        queued=0,
        dead_letters=0,
        first_received=None,
        last_received=None,
        unrecoverable=3,
    )
    assert found.lines() == [
        "Le graphe compte 3 lignes.",
        "Aucun événement brut conservé : le graphe reconstruit serait vide.",
        "3 lignes du graphe, enregistrées avant le premier événement conservé, ne seraient pas"
        " rendues telles quelles : --accept-loss l'accepte.",
        "Rien n'est fait sans --yes.",
    ]
