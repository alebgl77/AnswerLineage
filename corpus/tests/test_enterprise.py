"""Une entreprise synthétique : même graine, même jeu, et la vérité de chaque incident."""

from __future__ import annotations

import json
import re
from datetime import UTC, timedelta

import pytest

from answerlineage_corpus.enterprise import Enterprise, Incident, Request, generate
from answerlineage_corpus.events import render
from answerlineage_corpus.truth import truth

SEED = 7


@pytest.fixture(scope="module")
def enterprise() -> Enterprise:
    return generate(SEED)


def reads_the_table_in_the_window(request: Request, incident: Incident) -> bool:
    return any(
        read.table == incident.table and incident.start <= call.time < incident.end
        for call in request.calls
        for read in call.reads
    )


def test_same_seed_gives_the_same_enterprise(enterprise: Enterprise) -> None:
    again = generate(SEED)
    assert again == enterprise
    assert json.dumps(render(again)) == json.dumps(render(enterprise))
    assert json.dumps(truth(again)) == json.dumps(truth(enterprise))


def test_another_seed_gives_another_enterprise(enterprise: Enterprise) -> None:
    other = generate(SEED + 1)
    assert other.requests != enterprise.requests
    assert other.recipients != enterprise.recipients
    assert other.incidents != enterprise.incidents


def test_enterprise_has_agents_tools_data_requests_and_incidents(enterprise: Enterprise) -> None:
    assert len(enterprise.agents) >= 4
    assert len(enterprise.tools) >= 3
    assert len(enterprise.tables) >= 10
    assert len(enterprise.requests) == 240
    assert len(enterprise.incidents) == 6
    assert {agent for request in enterprise.requests for agent in [request.agent]} == set(
        enterprise.agents
    )


def test_size_is_chosen_by_the_caller() -> None:
    small = generate(SEED, requests=12, days=2, incidents=1)
    assert len(small.requests) == 12
    assert len(small.incidents) == 1
    assert {request.started_at.date() for request in small.requests} <= {
        small.requests[0].started_at.date() + timedelta(days=offset) for offset in range(-1, 2)
    }


def test_an_agent_only_reads_through_its_own_tools(enterprise: Enterprise) -> None:
    for request in enterprise.requests:
        allowed = {(tool, table) for tool, tables in request.agent.access for table in tables}
        assert request.calls
        for call in request.calls:
            assert call.reads
            for read in call.reads:
                assert (call.tool, read.table) in allowed
                assert set(read.columns) <= set(read.table.columns)


def test_requests_happen_during_working_hours_in_order(enterprise: Enterprise) -> None:
    starts = [request.started_at for request in enterprise.requests]
    assert starts == sorted(starts)
    for request in enterprise.requests:
        assert request.started_at.tzinfo is UTC
        assert request.started_at.weekday() < 5
        assert 8 <= request.started_at.hour < 18
        moments = [request.started_at, *(call.time for call in request.calls)]
        if request.answer is not None:
            moments.append(request.answer.time)
        assert moments == sorted(set(moments))
        assert request.ended_at == moments[-1]


def test_run_identifiers_are_distinct_uuid_version_7(enterprise: Enterprise) -> None:
    identifiers = [request.run_id for request in enterprise.requests] + [
        call.run_id for request in enterprise.requests for call in request.calls
    ]
    assert len(set(identifiers)) == len(identifiers)
    assert {identifier.version for identifier in identifiers} == {7}
    first = enterprise.requests[0]
    assert first.run_id.int >> 80 == int(first.started_at.timestamp() * 1000)


def test_most_requests_end_with_an_answer_sent_to_the_agent_audience(
    enterprise: Enterprise,
) -> None:
    answered = [request for request in enterprise.requests if request.answer is not None]
    assert 0.8 * len(enterprise.requests) < len(answered) < len(enterprise.requests)
    fingerprints = {request.answer.fingerprint for request in answered if request.answer}
    assert len(fingerprints) == len(answered)
    for request in answered:
        assert request.answer is not None
        assert request.answer.channel == request.agent.channel
        assert 1 <= len(request.answer.recipients) <= 3
        assert set(request.answer.recipients) <= set(request.agent.audience)
        assert len(set(request.answer.recipients)) == len(request.answer.recipients)


def test_recipients_are_pseudonyms_never_names(enterprise: Enterprise) -> None:
    assert len(set(enterprise.recipients)) == len(enterprise.recipients)
    for pseudonym in enterprise.recipients:
        assert re.fullmatch(r"[0-9a-f]{32}", pseudonym)
    assert {pseudonym for agent in enterprise.agents for pseudonym in agent.audience} == set(
        enterprise.recipients
    )


def test_truth_of_an_incident_is_the_exact_list_of_touched_answers(
    enterprise: Enterprise,
) -> None:
    for incident in enterprise.incidents:
        expected = [
            request
            for request in enterprise.requests
            if request.answer is not None and reads_the_table_in_the_window(request, incident)
        ]
        assert list(enterprise.touched(incident)) == expected


def test_an_incident_touches_some_answers_but_not_every_reader_of_the_table(
    enterprise: Enterprise,
) -> None:
    for incident in enterprise.incidents:
        touched = enterprise.touched(incident)
        readers = [
            request
            for request in enterprise.requests
            if request.answer is not None
            and any(read.table == incident.table for call in request.calls for read in call.reads)
        ]
        assert touched
        assert len(touched) < len(readers)


def test_incidents_bear_on_different_tables(enterprise: Enterprise) -> None:
    tables = [incident.table for incident in enterprise.incidents]
    assert len(set(tables)) == len(tables)
    assert [incident.name for incident in enterprise.incidents] == [
        f"incident-{rank:02d}" for rank in range(1, 7)
    ]


def test_a_run_without_answer_has_nothing_to_recall(enterprise: Enterprise) -> None:
    silent = next(request for request in enterprise.requests if request.answer is None)
    call = silent.calls[0]
    incident = Incident(
        "essai", call.reads[0].table, call.time - timedelta(hours=1), call.time + timedelta(hours=1)
    )
    assert reads_the_table_in_the_window(silent, incident)
    assert silent not in enterprise.touched(incident)


def test_a_window_includes_its_start_and_excludes_its_end(enterprise: Enterprise) -> None:
    first = next(request for request in enterprise.requests if request.answer is not None)
    call = first.calls[0]
    table = call.reads[0].table
    opening = Incident("essai", table, call.time, call.time + timedelta(seconds=1))
    closing = Incident("essai", table, call.time - timedelta(hours=1), call.time)
    assert first in enterprise.touched(opening)
    assert first not in enterprise.touched(closing)


def test_an_answer_built_on_a_read_made_after_the_window_is_not_touched(
    enterprise: Enterprise,
) -> None:
    first = next(request for request in enterprise.requests if request.answer is not None)
    call = first.calls[0]
    table = call.reads[0].table
    incident = Incident("essai", table, call.time, call.time + timedelta(seconds=1))
    later = [
        request
        for request in enterprise.requests
        if request is not first
        and request.answer is not None
        and any(read.table == table for call in request.calls for read in call.reads)
    ]
    assert later
    assert list(enterprise.touched(incident)) == [first]


def test_an_incident_starts_with_the_nightly_load_of_a_day_the_table_was_read(
    enterprise: Enterprise,
) -> None:
    for incident in enterprise.incidents:
        assert (incident.start.hour, incident.start.minute, incident.start.second) == (6, 0, 0)
        assert incident.start.weekday() < 5
        assert timedelta(hours=6) <= incident.end - incident.start <= timedelta(days=3)


def test_truth_is_published_without_any_content(enterprise: Enterprise) -> None:
    document = truth(enterprise)
    assert document["seed"] == SEED
    assert [incident["name"] for incident in document["incidents"]] == [
        incident.name for incident in enterprise.incidents
    ]
    for incident, published in zip(enterprise.incidents, document["incidents"], strict=True):
        touched = enterprise.touched(incident)
        assert published["dataset"] == {
            "namespace": incident.table.namespace,
            "name": incident.table.name,
        }
        assert published["from"] == incident.start.isoformat().replace("+00:00", "Z")
        assert published["to"] == incident.end.isoformat().replace("+00:00", "Z")
        assert [answer["fingerprint"] for answer in published["answers"]] == [
            request.answer.fingerprint for request in touched if request.answer
        ]
        assert published["recipients"] == sorted(
            {
                pseudonym
                for request in touched
                if request.answer
                for pseudonym in request.answer.recipients
            }
        )
        for answer in published["answers"]:
            assert set(answer) == {"fingerprint", "channel", "messageRef", "recipients", "agent"}
