"""Contexte : la chaîne d'un adaptateur, le contexte de trace d'un point de capture."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from answerlineage_conformance.runid import derive_run_id
from conformance.tests.captures import (
    AGENT_RUN,
    DERIVED_RUN,
    OTHER_RUN,
    SPAN_ID,
    TOOL_RUN,
    TRACE_ID,
    agent_complete,
    agent_start,
    captured_call,
    found,
    judged,
    mutated,
    parent,
    request,
    tool_call,
)

ROOT = Path(__file__).resolve().parents[2]
VECTORS: dict[str, Any] = json.loads(
    (ROOT / "spec" / "run-id" / "vectors-v1.json").read_text(encoding="utf-8")
)
PARENT = "/run/facets/parent"
TOOL_CALL = "/run/facets/answerlineage_toolCall"
NO_CHAIN = ("no_complete_chain", None, None)


def test_a_tool_call_without_the_parent_facet_fails_and_the_missing_field_is_named() -> None:
    events = mutated(request(), 1, ["run", "facets", "parent"])
    assert found(events, "context") == [("parent_missing", 1, PARENT), NO_CHAIN]
    assert not judged(events).conformant


def test_a_capture_point_owes_no_parent() -> None:
    assert judged([captured_call()], profile="capture").problems == ()


BROKEN_PARENTS: dict[str, tuple[list[str | int], Any, tuple[str, int, str]]] = {
    "identifiant de run absent": (
        ["run", "runId"],
        ...,
        ("parent_run_id_missing", 1, f"{PARENT}/run/runId"),
    ),
    "run absent": (["run"], ..., ("parent_run_id_missing", 1, f"{PARENT}/run/runId")),
    "identifiant qui n'est pas un UUID": (
        ["run", "runId"],
        "run-de-l-agent",
        ("parent_run_id_not_canonical", 1, f"{PARENT}/run/runId"),
    ),
    "identifiant qui n'est pas un texte": (
        ["run", "runId"],
        7,
        ("parent_run_id_not_canonical", 1, f"{PARENT}/run/runId"),
    ),
    "parent hors de la capture": (
        ["run", "runId"],
        OTHER_RUN,
        ("parent_not_in_capture", 1, f"{PARENT}/run/runId"),
    ),
}


@pytest.mark.parametrize("case", list(BROKEN_PARENTS))
def test_a_parent_that_leads_nowhere_is_named_by_its_pointer(case: str) -> None:
    path, value, expected = BROKEN_PARENTS[case]
    events = mutated(request(), 1, ["run", "facets", "parent", *path], value)
    assert found(events, "context") == [expected, NO_CHAIN]


def test_a_parent_written_in_upper_case_designates_the_same_run() -> None:
    events = mutated(request(), 1, ["run", "facets", "parent", "run", "runId"], AGENT_RUN.upper())
    assert judged(events).problems == ()


def test_the_events_of_two_emitters_for_the_same_run_complete_each_other() -> None:
    """Capture combinée : le point de capture clôt l'appel, l'adaptateur le rattache à l'agent."""
    opened = tool_call(DERIVED_RUN)
    opened["eventType"], opened["inputs"] = "START", []
    events = [agent_start(), opened, captured_call(), agent_complete()]
    assert judged(events).problems == ()
    # Sans l'événement de l'adaptateur, rien ne rattache l'appel à l'agent.
    assert found([agent_start(), captured_call(), agent_complete()], "context") == [
        ("parent_missing", 1, PARENT),
        NO_CHAIN,
    ]


def test_the_chain_may_go_through_a_sub_agent() -> None:
    delegated = agent_start(OTHER_RUN, parent=parent())
    closed = agent_complete(OTHER_RUN, parent=parent())
    closed["outputs"] = []
    call = mutated([tool_call()], 0, ["run", "facets", "parent", "run", "runId"], OTHER_RUN)[0]
    events = [agent_start(), delegated, call, closed, agent_complete()]
    assert judged(events).problems == ()


def test_two_runs_that_name_each_other_as_parent_do_not_make_a_chain() -> None:
    first = mutated([tool_call()], 0, ["run", "facets", "parent", "run", "runId"], OTHER_RUN)[0]
    second = mutated(
        [tool_call(OTHER_RUN)], 0, ["run", "facets", "parent", "run", "runId"], TOOL_RUN
    )[0]
    assert found([first, second], "context") == [NO_CHAIN]


INCOMPLETE: dict[str, list[dict[str, Any]]] = {
    "aucun appel d'outil": [agent_start(), agent_complete()],
    "appel d'outil sans lecture": mutated(request(), 1, ["inputs"], []),
    "réponse sans diffusion": mutated(request(), 2, ["outputs"], []),
    "run qui n'est pas celui d'un agent": [
        mutated([event], 0, ["run", "facets", "answerlineage_agent"])[0] if rank != 1 else event
        for rank, event in enumerate(request())
    ],
}


@pytest.mark.parametrize("case", list(INCOMPLETE))
def test_a_capture_without_a_complete_chain_proves_nothing(case: str) -> None:
    assert found(INCOMPLETE[case], "context") == [NO_CHAIN]


def test_an_empty_capture_proves_nothing() -> None:
    assert found([], "context") == [NO_CHAIN]
    assert found([], "context", profile="capture") == [("no_tool_call", None, None)]


def test_a_run_without_a_terminal_event_is_reported() -> None:
    events = request()[:2]
    assert found(events, "context") == [("run_without_terminal_event", 0, "/eventType"), NO_CHAIN]


def test_a_run_with_several_terminal_events_is_reported() -> None:
    failed = mutated([tool_call()], 0, ["eventType"], "FAIL")[0]
    events = [*request(), failed]
    assert found(events, "context") == [("run_with_several_terminal_events", 3, "/eventType")]


def test_an_event_sent_twice_is_one_event() -> None:
    """L'ingestion ne garde le second envoi que comme un doublon."""
    events = [*request(), tool_call()]
    assert judged(events).problems == ()


def test_an_event_the_base_cannot_store_is_rejected_but_still_closes_its_run() -> None:
    events = mutated(
        request(), 1, ["run", "facets", "answerlineage_toolCall", "transport"], "a\x00b"
    )
    assert found(events, "shape") == [("ingestion_rejected", 1, "")]
    assert found(events, "context") == [NO_CHAIN]


def test_an_event_without_a_usable_run_identifier_belongs_to_no_run() -> None:
    events = [
        *request(),
        mutated([tool_call()], 0, ["run", "runId"], "x")[0],
        "COMPLETE",
        {"run": 3},
    ]
    judgement = judged(events)
    assert judgement.runs == 2
    assert [problem.code for problem in judgement.problems if problem.group == "context"] == []


def test_a_capture_point_cites_the_trace_context_of_each_call() -> None:
    events = [captured_call(trace_id=None)]
    assert found(events, "context", profile="capture") == [
        ("trace_context_missing", 0, f"{TOOL_CALL}/traceId")
    ]
    events = [captured_call(trace_id=None, span_id=None)]
    assert found(events, "context", profile="capture") == [
        ("trace_context_missing", 0, f"{TOOL_CALL}/spanId"),
        ("trace_context_missing", 0, f"{TOOL_CALL}/traceId"),
    ]


def test_the_events_of_a_call_may_share_the_citation_of_its_context() -> None:
    opened = captured_call(trace_id=None, span_id=None)
    opened["eventType"] = "START"
    assert judged([opened, captured_call()], profile="capture").problems == ()


def test_an_adapter_owes_no_trace_context() -> None:
    assert "trace_context_missing" not in {control.code for control in judged(request()).controls}


def test_a_run_identifier_that_does_not_derive_from_the_trace_context_is_reported() -> None:
    events = [captured_call(run_id=TOOL_RUN)]
    assert found(events, "context", profile="capture") == [("run_id_not_derived", 0, "/run/runId")]


def test_an_adapter_that_cites_a_trace_context_is_held_to_the_same_derivation() -> None:
    events = mutated(request(), 1, ["run", "facets", "answerlineage_toolCall", "traceId"], TRACE_ID)
    events = mutated(events, 1, ["run", "facets", "answerlineage_toolCall", "spanId"], SPAN_ID)
    assert found(events, "context") == [("run_id_not_derived", 1, "/run/runId")]


def test_a_derived_identifier_written_in_upper_case_is_the_same_identifier() -> None:
    events = [captured_call(run_id=DERIVED_RUN.upper())]
    assert judged(events, profile="capture").problems == ()


def test_a_malformed_trace_context_is_left_to_the_schema() -> None:
    events = [captured_call(trace_id="4BF9" * 8)]
    assert found(events, "context", profile="capture") == []
    assert found(events, "shape", profile="capture") == [
        ("facet_schema_violation", 0, f"{TOOL_CALL}/traceId")
    ]


def test_a_capture_point_only_emits_tool_calls() -> None:
    events = [captured_call(), agent_start()]
    assert found(events, "context", profile="capture") == [
        ("run_without_terminal_event", 1, "/eventType"),
        ("not_a_tool_call", 1, TOOL_CALL),
    ]


def test_a_capture_without_any_tool_call_proves_nothing() -> None:
    events = [agent_start(), agent_complete()]
    assert ("no_tool_call", None, None) in found(events, "context", profile="capture")


@pytest.mark.parametrize("vector", VECTORS["valid"], ids=lambda vector: vector["traceparent"])
def test_a_published_vector_passes(vector: dict[str, str]) -> None:
    trace_id, span_id, run_id = vector["traceId"], vector["parentId"], vector["runId"]
    assert derive_run_id(trace_id, span_id) == UUID(run_id)
    assert judged([captured_call(trace_id, span_id, run_id)], profile="capture").problems == ()


def test_the_identifier_of_one_published_vector_does_not_fit_the_context_of_another() -> None:
    first, *_, last = VECTORS["valid"]
    events = [captured_call(first["traceId"], first["parentId"], last["runId"])]
    assert found(events, "context", profile="capture") == [("run_id_not_derived", 0, "/run/runId")]
