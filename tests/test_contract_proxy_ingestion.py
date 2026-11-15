"""Contrat entre composants : ce que le proxy émet, l'API d'ingestion l'accepte."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from answerlineage_api.ingest.models import validate_event
from answerlineage_api.ingest.store import event_identifier
from answerlineage_proxy.events import build_tool_call_event
from answerlineage_proxy.inspection import Inspection
from answerlineage_proxy.sql import analyze_sql
from answerlineage_proxy.tracecontext import resolve_trace
from api.tests.events import official_run_event_schema

OFFICIAL = Draft202012Validator(official_run_event_schema(), format_checker=FormatChecker())

INSPECTIONS = {
    "appel réussi": Inspection(
        is_tool_call=True, tool="execute_sql", protocol_version="2026-07-28"
    ),
    "nom d'outil refusé": Inspection(is_tool_call=True, invalid_tool_name=True),
    "en-têtes contredits": Inspection(
        is_tool_call=True, tool="get_weather", header_mismatches=("mcp-name", "mcp-method")
    ),
}


@pytest.mark.parametrize("name", list(INSPECTIONS))
def test_proxy_event_is_a_valid_openlineage_run_event(name: str) -> None:
    trace = resolve_trace(None, [])
    event = build_tool_call_event(
        run_id=trace.run_id,
        event_time=datetime.now(UTC),
        outcome="COMPLETE",
        server="entrepot",
        inspection=INSPECTIONS[name],
        http_status=200,
        trace=trace,
    )
    assert list(OFFICIAL.iter_errors(event)) == []
    accepted, errors = validate_event(event)
    assert errors == []
    assert accepted is not None
    assert accepted.job.namespace == "mcp://entrepot"
    assert "answerlineage_toolCall" in accepted.run.facets
    # L'API d'ingestion accepte l'identifiant dérivé du contexte de trace (UUID version 8).
    assert accepted.run.run_id == trace.run_id
    assert len(event_identifier(event)) == 64


def test_proxy_event_with_sql_reads_is_a_valid_openlineage_run_event() -> None:
    sql = "SELECT c.nom, fx.taux FROM crm.clients c JOIN ref.taux_change fx ON fx.devise = c.devise"
    event = build_tool_call_event(
        run_id=uuid4(),
        event_time=datetime.now(UTC),
        outcome="COMPLETE",
        server="entrepot",
        inspection=Inspection(is_tool_call=True, tool="execute_sql"),
        http_status=200,
        reads=analyze_sql(sql + " WHERE fx.date = '2026-11-02'", "bigquery"),
        namespace="bigquery",
    )
    assert list(OFFICIAL.iter_errors(event)) == []
    accepted, errors = validate_event(event)
    assert errors == []
    assert accepted is not None
    assert [(item.namespace, item.name) for item in accepted.inputs] == [
        ("bigquery", "crm.clients"),
        ("bigquery", "ref.taux_change"),
    ]
    assert set(accepted.inputs[1].facets) == {"schema"}
