"""Deux appels d'outils différents sous le même contexte de trace, jusque dans le graphe.

Un client qui réutilise le même span pour deux appels donne aux deux le même
identifiant de run dérivé. Le graphe fusionne alors leurs événements en un seul
run : rien ne plante, les lectures des deux appels s'additionnent, mais le run
garde l'outil du premier événement projeté. Ce test constate ce comportement,
que ``docs/proxy.md`` décrit dans ses limites.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.ingest.models import validate_event
from answerlineage_api.ingest.store import AcceptedEvent, event_identifier, store_events
from answerlineage_proxy.events import build_tool_call_event
from answerlineage_proxy.inspection import Inspection
from answerlineage_proxy.sql import SqlAnalysis, TableRead
from answerlineage_proxy.tracecontext import CallTrace, parse_traceparent
from answerlineage_workers.queue import BatchResult, process_batch

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

REUSED_SPAN = parse_traceparent("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
TABLES = {"execute_sql": "crm.clients", "query_warehouse": "ref.taux_change"}


def call_event(tool: str, moment: datetime) -> dict[str, Any]:
    assert REUSED_SPAN is not None
    trace = CallTrace(REUSED_SPAN, "meta")
    return build_tool_call_event(
        run_id=trace.run_id,
        event_time=moment,
        outcome="COMPLETE",
        server="entrepot",
        inspection=Inspection(is_tool_call=True, tool=tool),
        http_status=200,
        reads=SqlAnalysis(resolved=True, tables=(TableRead(TABLES[tool], "table"),)),
        namespace="bigquery",
        trace=trace,
    )


@pytest.mark.parametrize(
    "arrival", [("execute_sql", "query_warehouse"), ("query_warehouse", "execute_sql")]
)
async def test_two_tools_under_the_same_span_merge_into_one_run(
    engine: AsyncEngine, arrival: tuple[str, str]
) -> None:
    moments = {"execute_sql": T0, "query_warehouse": T0 + timedelta(seconds=1)}
    accepted = []
    for tool in arrival:
        payload = call_event(tool, moments[tool])
        event, errors = validate_event(payload)
        assert event is not None, errors
        accepted.append(AcceptedEvent(event_identifier(payload), event, payload))
    async with engine.begin() as conn:
        assert await store_events(conn, accepted) == 2

    # Rien ne plante : les deux événements sont projetés, aucun n'est remis à plus tard.
    assert await process_batch(engine) == BatchResult(processed=2)

    async with engine.connect() as conn:
        runs = (
            await conn.execute(
                text(
                    "SELECT run.kind, run.state, run.ended_at, tool.name, run.trace_id, "
                    "run.span_id FROM run JOIN tool ON tool.id = run.tool_id"
                )
            )
        ).all()
        reads = (
            await conn.execute(
                text(
                    "SELECT dataset.name FROM read_edge "
                    "JOIN dataset ON dataset.id = read_edge.dataset_id ORDER BY dataset.name"
                )
            )
        ).scalars()
        tools = (await conn.execute(text("SELECT name FROM tool ORDER BY name"))).scalars()
        read_names, tool_names = list(reads), list(tools)

    # Un seul run, qui porte les lectures des deux appels.
    ((kind, state, ended_at, tool, trace_id, span_id),) = runs
    assert (kind, state, ended_at) == ("tool", "COMPLETE", T0 + timedelta(seconds=1))
    # Le run retient le contexte dont son identifiant dérive : on passe du graphe à la trace.
    assert REUSED_SPAN is not None
    assert (trace_id, span_id) == (REUSED_SPAN.trace_id, REUSED_SPAN.parent_id)
    assert read_names == ["crm.clients", "ref.taux_change"]
    assert tool_names == ["execute_sql", "query_warehouse"]
    # Limite constatée : le run garde l'outil du premier événement projeté, qui
    # dépend donc de l'ordre d'arrivée.
    assert tool == arrival[0]
