"""Les événements OpenLineage de l'entreprise synthétique."""

from __future__ import annotations

import json
from typing import Any

import pytest

from answerlineage_corpus.enterprise import Enterprise, Request, generate
from answerlineage_corpus.events import PRODUCER, render, request_events, tool_call_event


@pytest.fixture(scope="module")
def enterprise() -> Enterprise:
    return generate(7)


def answered(enterprise: Enterprise) -> Request:
    return next(
        request
        for request in enterprise.requests
        if request.answer is not None and len(request.calls) > 1
    )


def test_a_request_opens_its_run_calls_its_tools_then_answers(enterprise: Enterprise) -> None:
    request = answered(enterprise)
    events = request_events(request)
    assert [event["eventType"] for event in events] == [
        "START",
        *["COMPLETE"] * len(request.calls),
        "COMPLETE",
    ]
    opening, closing = events[0], events[-1]
    for event in (opening, closing):
        assert event["run"]["runId"] == str(request.run_id)
        assert event["job"] == {"namespace": request.agent.namespace, "name": request.agent.name}
        assert set(event["run"]["facets"]) == {"answerlineage_agent"}
    assert opening["eventTime"] == request.started_at.strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    assert opening["outputs"] == []
    assert request.answer is not None
    assert closing["eventTime"] == request.answer.time.strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def test_the_answer_is_an_output_described_by_its_fingerprint_and_its_delivery(
    enterprise: Enterprise,
) -> None:
    request = answered(enterprise)
    assert request.answer is not None
    (output,) = request_events(request)[-1]["outputs"]
    assert output["namespace"] == request.agent.namespace
    assert output["name"] == f"{request.agent.name}/reponses"
    delivery = output["outputFacets"]["answerlineage_delivery"]
    assert {key: value for key, value in delivery.items() if not key.startswith("_")} == {
        "fingerprint": request.answer.fingerprint,
        "channel": request.answer.channel,
        "messageRef": request.answer.message_ref,
        "recipients": list(request.answer.recipients),
    }


def test_a_run_that_did_not_answer_ends_in_failure_without_output(
    enterprise: Enterprise,
) -> None:
    silent = next(request for request in enterprise.requests if request.answer is None)
    closing = request_events(silent)[-1]
    assert closing["eventType"] == "FAIL"
    assert closing["outputs"] == []
    assert closing["eventTime"] == silent.calls[-1].time.strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def test_a_tool_call_is_a_child_run_carrying_the_tables_it_read(enterprise: Enterprise) -> None:
    request = answered(enterprise)
    call = request.calls[0]
    event = tool_call_event(request, call)
    assert event["run"]["runId"] == str(call.run_id)
    assert event["job"] == {"namespace": f"mcp://{call.tool.server}", "name": call.tool.name}
    parent = event["run"]["facets"]["parent"]
    assert parent["run"] == {"runId": str(request.run_id)}
    assert parent["job"] == {"namespace": request.agent.namespace, "name": request.agent.name}
    tool_call = event["run"]["facets"]["answerlineage_toolCall"]
    assert (tool_call["server"], tool_call["tool"]) == (call.tool.server, call.tool.name)
    assert tool_call["readResolution"] == "resolved"
    assert [(dataset["namespace"], dataset["name"]) for dataset in event["inputs"]] == [
        (read.table.namespace, read.table.name) for read in call.reads
    ]


def test_columns_and_version_of_a_read_travel_in_standard_facets(enterprise: Enterprise) -> None:
    seen: set[tuple[bool, bool]] = set()
    for request in enterprise.requests:
        for call in request.calls:
            for read, dataset in zip(
                call.reads, tool_call_event(request, call)["inputs"], strict=True
            ):
                facets: dict[str, Any] = dataset.get("facets", {})
                seen.add((bool(read.columns), read.version is not None))
                assert ("schema" in facets) == bool(read.columns)
                assert ("version" in facets) == (read.version is not None)
                if read.columns:
                    assert [field["name"] for field in facets["schema"]["fields"]] == list(
                        read.columns
                    )
                if read.version is not None:
                    assert facets["version"]["datasetVersion"] == call.time.date().isoformat()
    # Table entière ou colonnes, table d'entrepôt versionnée ou base opérationnelle.
    assert seen == {(True, True), (False, True), (False, False)}


def test_events_come_in_the_order_of_time_and_name_their_producer(
    enterprise: Enterprise,
) -> None:
    events = render(enterprise)
    assert len(events) == sum(2 + len(request.calls) for request in enterprise.requests)
    times = [event["eventTime"] for event in events]
    assert times == sorted(times)
    assert {event["producer"] for event in events} == {PRODUCER}
    assert len({json.dumps(event, sort_keys=True) for event in events}) == len(events)


def test_no_event_carries_anything_but_names_fingerprints_and_pseudonyms(
    enterprise: Enterprise,
) -> None:
    allowed_run_facets = {"parent", "answerlineage_agent", "answerlineage_toolCall"}
    for event in render(enterprise):
        assert set(event) == {
            "eventType",
            "eventTime",
            "producer",
            "schemaURL",
            "run",
            "job",
            "inputs",
            "outputs",
        }
        assert set(event["run"]["facets"]) <= allowed_run_facets
        for dataset in event["inputs"]:
            assert set(dataset) <= {"namespace", "name", "facets"}
            assert set(dataset.get("facets", {})) <= {"schema", "version"}
        for dataset in event["outputs"]:
            assert set(dataset) == {"namespace", "name", "outputFacets"}
