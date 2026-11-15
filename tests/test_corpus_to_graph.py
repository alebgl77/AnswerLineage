"""L'entreprise synthétique arrive dans le graphe par le chemin réel : ingestion, file, workers."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_corpus.enterprise import Enterprise, generate
from answerlineage_corpus.events import render
from answerlineage_workers.queue import process_batch

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def rows(engine: AsyncEngine, sql: str) -> list[tuple[Any, ...]]:
    async with engine.connect() as conn:
        return [tuple(row) for row in await conn.execute(text(sql))]


async def load(engine: AsyncEngine, enterprise: Enterprise) -> None:
    events = render(enterprise)
    transport = httpx.ASGITransport(app=create_app(engine))
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
        response = await client.post("/api/v1/lineage/batch", json=events)
    assert response.status_code == 202
    assert response.json() == {"accepted": len(events), "duplicates": 0, "rejected": []}
    while not (await process_batch(engine)).empty:
        pass
    assert await rows(engine, "SELECT count(*) FROM ingest_queue") == [(0,)]


async def test_the_graph_holds_the_enterprise_and_nothing_else(engine: AsyncEngine) -> None:
    enterprise = generate(7, requests=60, days=5, incidents=2)
    await load(engine, enterprise)
    requests = enterprise.requests
    calls = [(request, call) for request in requests for call in request.calls]

    assert set(await rows(engine, "SELECT namespace, name FROM agent")) == {
        (request.agent.namespace, request.agent.name) for request in requests
    }
    assert set(await rows(engine, "SELECT server, name FROM tool")) == {
        (call.tool.server, call.tool.name) for _, call in calls
    }
    assert set(await rows(engine, "SELECT namespace, name, kind FROM dataset")) == {
        (read.table.namespace, read.table.name, "table") for _, call in calls for read in call.reads
    }

    runs = await rows(
        engine,
        "SELECT run_id, kind, parent_run_id, state, started_at, ended_at FROM run",
    )
    assert set(runs) == {
        (
            request.run_id,
            "agent",
            None,
            "FAIL" if request.answer is None else "COMPLETE",
            request.started_at,
            request.ended_at,
        )
        for request in requests
    } | {
        (call.run_id, "tool", request.run_id, "COMPLETE", None, call.time)
        for request, call in calls
    }

    reads = await rows(
        engine,
        """
        SELECT e.run_id, d.namespace, d.name, v.version, e.precision, e.columns, e.event_time
        FROM read_edge e
        JOIN dataset d ON d.id = e.dataset_id
        LEFT JOIN dataset_version v ON v.id = e.dataset_version_id
        """,
    )
    assert {(*read[:5], tuple(read[5]), read[6]) for read in reads} == {
        (
            call.run_id,
            read.table.namespace,
            read.table.name,
            read.version,
            "columns" if read.columns else "table",
            tuple(sorted(read.columns)),
            call.time,
        )
        for _, call in calls
        for read in call.reads
    }
    assert len(reads) == sum(len(call.reads) for _, call in calls)
    assert await rows(engine, "SELECT count(*) FROM write_edge") == [(0,)]

    answers = await rows(
        engine,
        """
        SELECT a.run_id, a.fingerprint, a.event_time, d.channel, d.message_ref,
               array_agg(r.pseudonym ORDER BY r.pseudonym)
        FROM answer a
        JOIN delivery d ON d.answer_id = a.id
        JOIN delivery_recipient dr ON dr.delivery_id = d.id
        JOIN recipient r ON r.id = dr.recipient_id
        GROUP BY a.id, d.id
        """,
    )
    assert {(*answer[:5], tuple(answer[5])) for answer in answers} == {
        (
            request.run_id,
            request.answer.fingerprint,
            request.answer.time,
            request.answer.channel,
            request.answer.message_ref,
            request.answer.recipients,
        )
        for request in requests
        if request.answer is not None
    }
    assert await rows(engine, "SELECT count(*) FROM answer") == [(len(answers),)]
    assert set(await rows(engine, "SELECT pseudonym FROM recipient")) == {
        (pseudonym,)
        for request in requests
        if request.answer is not None
        for pseudonym in request.answer.recipients
    }
