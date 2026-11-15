"""Contrat entre composants : l'entreprise synthétique parle comme les émetteurs réels.

Ses événements sont des RunEvents OpenLineage que l'API d'ingestion accepte,
ses appels d'outils sont ceux que le proxy émet, et la projection en tire le
graphe attendu.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from answerlineage_api.app import DEFAULT_MAX_BATCH_EVENTS
from answerlineage_api.ingest.body import DEFAULT_MAX_BODY_BYTES, MAX_DEPTH, nesting_depth
from answerlineage_api.ingest.coverage import validate_report
from answerlineage_api.ingest.models import validate_event
from answerlineage_api.store.graph import (
    AgentRef,
    Answer,
    DatasetRef,
    Delivery,
    Read,
    ToolRef,
    Write,
)
from answerlineage_corpus.coverage import coverage_reports
from answerlineage_corpus.enterprise import Enterprise, generate
from answerlineage_corpus.events import PROTOCOL_VERSION, render, request_events, tool_call_event
from answerlineage_corpus.scenarios import load_corpus
from answerlineage_proxy.events import build_tool_call_event
from answerlineage_proxy.inspection import Inspection
from answerlineage_proxy.sql import SqlAnalysis, TableRead
from answerlineage_workers.projection import project
from api.tests.events import official_run_event_schema

OFFICIAL = Draft202012Validator(official_run_event_schema(), format_checker=FormatChecker())


@pytest.fixture(scope="module")
def enterprise() -> Enterprise:
    return generate(7)


def test_every_event_is_an_openlineage_run_event_the_ingestion_accepts(
    enterprise: Enterprise,
) -> None:
    events = render(enterprise)
    assert len(events) > 900
    for event in events:
        assert list(OFFICIAL.iter_errors(event)) == []
        accepted, errors = validate_event(event)
        assert errors == []
        assert accepted is not None


def test_every_coverage_report_is_one_the_ingestion_accepts_from_the_proxys_emitter(
    enterprise: Enterprise,
) -> None:
    # À la date de ses relevés : l'entreprise peut être datée après l'horloge du test.
    latest = max(request.ended_at for request in enterprise.requests)
    emitters = {event["job"]["namespace"] for event in render(enterprise)}
    for report in coverage_reports(enterprise):
        accepted, errors = validate_report(report, received_at=latest)
        assert errors == []
        assert accepted is not None
        # L'émetteur d'un relevé est celui des appels qu'il compte.
        assert accepted.emitter in emitters


def without_origin(event: dict[str, Any]) -> dict[str, Any]:
    """L'événement sans ce qui tient à son émetteur : producteur, run parent, version lue."""

    def neutral(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: "émetteur" if key in ("producer", "_producer") else neutral(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [neutral(item) for item in value]
        return value

    shaped: dict[str, Any] = neutral(copy.deepcopy(event))
    shaped["run"]["facets"].pop("parent", None)
    for dataset in shaped["inputs"]:
        facets = dataset.get("facets", {})
        facets.pop("version", None)
        if not facets:
            dataset.pop("facets", None)
    return shaped


def test_a_tool_call_is_described_as_the_proxy_describes_it(enterprise: Enterprise) -> None:
    compared = 0
    for request in enterprise.requests:
        for call in request.calls:
            (namespace,) = {read.table.namespace for read in call.reads}
            emitted_by_the_proxy = build_tool_call_event(
                run_id=call.run_id,
                event_time=call.time,
                outcome="COMPLETE",
                server=call.tool.server,
                inspection=Inspection(
                    is_tool_call=True, tool=call.tool.name, protocol_version=PROTOCOL_VERSION
                ),
                http_status=200,
                reads=SqlAnalysis(
                    resolved=True,
                    tables=tuple(
                        TableRead(
                            name=read.table.name,
                            precision="columns" if read.columns else "table",
                            columns=read.columns,
                        )
                        for read in call.reads
                    ),
                ),
                namespace=namespace,
            )
            assert without_origin(tool_call_event(request, call)) == without_origin(
                emitted_by_the_proxy
            )
            compared += 1
    assert compared > 400


def test_the_projection_reads_a_request_as_an_agent_run_its_tool_runs_and_its_answer(
    enterprise: Enterprise,
) -> None:
    for request in enterprise.requests:
        opening, *calls, closing = [project(event) for event in request_events(request)]

        agent = AgentRef(request.agent.namespace, request.agent.name)
        for record in (opening, closing):
            assert (record.run_id, record.kind, record.agent) == (request.run_id, "agent", agent)
            assert record.parent_run_id is None
            assert record.reads == ()
        assert (opening.state, opening.event_time) == ("START", request.started_at)
        assert closing.event_time == request.ended_at

        for record, call in zip(calls, request.calls, strict=True):
            assert (record.run_id, record.kind, record.state) == (call.run_id, "tool", "COMPLETE")
            assert record.tool == ToolRef(call.tool.server, call.tool.name)
            assert record.parent_run_id == request.run_id
            assert record.event_time == call.time
            assert record.reads == tuple(
                Read(
                    DatasetRef(read.table.namespace, read.table.name, version=read.version),
                    precision="columns" if read.columns else "table",
                    columns=tuple(sorted(read.columns)),
                )
                for read in call.reads
            )

        assert closing.writes == ()
        if request.answer is None:
            assert (closing.state, closing.answers) == ("FAIL", ())
        else:
            assert closing.state == "COMPLETE"
            assert closing.answers == (
                Answer(
                    fingerprint=request.answer.fingerprint,
                    deliveries=(
                        Delivery(
                            channel=request.answer.channel,
                            message_ref=request.answer.message_ref,
                            recipients=request.answer.recipients,
                        ),
                    ),
                ),
            )


def test_every_event_of_the_truth_corpus_is_accepted_and_projected() -> None:
    for scenario in load_corpus():
        runs = {run.run_id: run for run in scenario.runs}
        calls = {call.run_id: call for run in scenario.runs for call in run.calls}
        for event in scenario.events():
            assert list(OFFICIAL.iter_errors(event)) == []
            accepted, errors = validate_event(event)
            assert (accepted is not None, errors) == (True, [])

            record = project(event)
            if record.run_id in calls:
                call = calls[record.run_id]
                assert (record.kind, record.tool) == ("tool", ToolRef(call.server, call.tool))
                assert record.reads == tuple(
                    Read(
                        DatasetRef(
                            read.dataset.namespace,
                            read.dataset.name,
                            kind="memory" if read.dataset.kind == "memory" else "table",
                            version=read.version,
                        ),
                        precision="columns" if read.columns else "table",
                        columns=tuple(sorted(read.columns)),
                    )
                    for read in call.reads
                )
                continue
            run = runs[record.run_id]
            assert (record.kind, record.agent) == ("agent", AgentRef(*run.agent))
            above = (
                None
                if run.parent is None
                else next(other.run_id for other in scenario.runs if other.key == run.parent)
            )
            assert record.parent_run_id == above
            if record.state == "START":
                assert (record.writes, record.answers) == ((), ())
                continue
            assert record.state == ("FAIL" if run.failed else "COMPLETE")
            # Une sortie qui porte une diffusion est une réponse ; les autres sont des écritures.
            assert record.writes == tuple(
                Write(
                    DatasetRef(
                        written.namespace,
                        written.name,
                        kind="memory" if written.kind == "memory" else "table",
                    )
                )
                for written in run.writes
            )
            assert [answer.fingerprint for answer in record.answers] == (
                [] if run.answer is None else [run.answer.fingerprint]
            )


def test_a_full_batch_of_the_heaviest_corpus_events_fits_the_ingestion_limits(
    enterprise: Enterprise,
) -> None:
    """Un lot plein du plus lourd des événements de l'entreprise et du corpus de vérité
    passe largement, en taille comme en profondeur."""
    events = render(enterprise) + [
        event for scenario in load_corpus() for event in scenario.events()
    ]
    heaviest = sorted(events, key=lambda event: len(json.dumps(event)))[-1:]
    batch = json.dumps(heaviest * DEFAULT_MAX_BATCH_EVENTS).encode()
    assert len(batch) * 4 < DEFAULT_MAX_BODY_BYTES
    assert nesting_depth(batch) * 4 < MAX_DEPTH
