"""Contrat entre composants : ce que le proxy émet, l'API d'ingestion l'accepte."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from answerlineage_api.ingest.models import validate_event
from answerlineage_api.ingest.store import event_identifier
from answerlineage_proxy.events import UNDESCRIBED_TOOL, build_tool_call_event
from answerlineage_proxy.fingerprint import RESULT_LABEL, FingerprintKey, arguments_fingerprint
from answerlineage_proxy.inspection import Inspection
from answerlineage_proxy.rules import KEY_LABEL, DeclaredDataset, DeclaredReads
from answerlineage_proxy.signals import OUTCOMES, CallOutcome, CallSignals
from answerlineage_proxy.sql import SqlAnalysis, analyze_sql
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


def test_a_stdio_proxy_event_is_a_valid_openlineage_run_event() -> None:
    """En stdio, ni statut HTTP ni en-têtes : la facette dit le transport."""
    trace = resolve_trace(None, [])
    event = build_tool_call_event(
        run_id=trace.run_id,
        event_time=datetime.now(UTC),
        outcome="FAIL",
        server="entrepot",
        inspection=Inspection(is_tool_call=True, tool="execute_sql"),
        trace=trace,
        signals=CallSignals(latency_ms=2.5, added_latency_ms=0.01, outcome="transport_error"),
        transport="stdio",
    )
    facet = event["run"]["facets"]["answerlineage_toolCall"]
    assert facet["transport"] == "stdio"
    assert "httpStatus" not in facet
    assert list(OFFICIAL.iter_errors(event)) == []
    accepted, errors = validate_event(event)
    assert errors == []
    assert accepted is not None
    assert accepted.run.run_id == trace.run_id


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


def test_proxy_event_with_fingerprints_and_a_sql_shape_is_accepted() -> None:
    key = FingerprintKey(bytes(range(32)))
    event = build_tool_call_event(
        run_id=uuid4(),
        event_time=datetime.now(UTC),
        outcome="COMPLETE",
        server="entrepot",
        inspection=Inspection(is_tool_call=True, tool="execute_sql"),
        http_status=200,
        reads=analyze_sql("SELECT c.nom FROM crm.clients c WHERE c.nom = 'Dupont'", "postgres"),
        namespace="postgres",
        dialect="postgres",
        arguments_fingerprint=arguments_fingerprint(key, {"query": "…"}),
        result_fingerprint=key.fingerprint(RESULT_LABEL, b"{}"),
        result_size=2,
    )
    assert list(OFFICIAL.iter_errors(event)) == []
    accepted, errors = validate_event(event)
    assert errors == []
    assert accepted is not None
    assert set(accepted.job.facets) == {"sql"}


@pytest.mark.parametrize("outcome", OUTCOMES)
def test_proxy_event_with_quality_of_service_signals_is_accepted(outcome: CallOutcome) -> None:
    """Chaque issue que le proxy émet, avec ses signaux, passe le schéma officiel et l'ingestion."""
    code = {"jsonrpc_error": -32602, "http_error": 503}.get(outcome)
    signals = CallSignals(
        latency_ms=4.5, added_latency_ms=0.12, outcome=outcome, error_code=code, retry_count=1
    )
    event = build_tool_call_event(
        run_id=uuid4(),
        event_time=datetime.now(UTC),
        outcome="COMPLETE",
        server="entrepot",
        inspection=Inspection(is_tool_call=True, tool="get_weather"),
        signals=signals,
    )
    assert list(OFFICIAL.iter_errors(event)) == []
    accepted, errors = validate_event(event)
    assert errors == []
    assert accepted is not None
    qos = accepted.run.facets["answerlineage_qos"].model_dump(by_alias=True, mode="json")
    assert qos["outcome"] == outcome
    assert qos["_schemaURL"].endswith("/spec/facets/1-1-0/QosRunFacet.json#/$defs/QosRunFacet")


@pytest.mark.parametrize(
    "declared",
    [
        DeclaredReads(
            (DeclaredDataset("postgres://referentiel:5432", "ref.tarifs", ("produit", "prix")),),
            (FingerprintKey(bytes(range(32))).fingerprint(KEY_LABEL, b'"P-1042"'),),
            "fingerprinted",
        ),
        DeclaredReads((DeclaredDataset("gs://documents", "politique%20voyages.pdf"),), (), None),
    ],
    ids=["clé lue", "sans clé"],
)
def test_proxy_event_with_reads_declared_by_a_rule_is_accepted(declared: DeclaredReads) -> None:
    event = build_tool_call_event(
        run_id=uuid4(),
        event_time=datetime.now(UTC),
        outcome="COMPLETE",
        server="catalogue",
        inspection=Inspection(is_tool_call=True, tool="get_price"),
        http_status=200,
        declared=declared,
    )
    assert list(OFFICIAL.iter_errors(event)) == []
    accepted, errors = validate_event(event)
    assert errors == []
    assert accepted is not None
    (dataset,) = accepted.inputs
    assert (dataset.namespace, dataset.name) == (
        declared.datasets[0].namespace,
        declared.datasets[0].name,
    )
    assert ("answerlineage_keys" in dataset.input_facets) == bool(declared.keys)


def test_proxy_event_of_an_undescribed_tool_is_accepted() -> None:
    event = build_tool_call_event(
        run_id=uuid4(),
        event_time=datetime.now(UTC),
        outcome="COMPLETE",
        server="catalogue",
        inspection=Inspection(is_tool_call=True, tool="get_time"),
        http_status=200,
        reads=SqlAnalysis(resolved=False, reason=UNDESCRIBED_TOOL),
    )
    assert list(OFFICIAL.iter_errors(event)) == []
    accepted, errors = validate_event(event)
    assert errors == []
    assert accepted is not None
    assert accepted.inputs == []
