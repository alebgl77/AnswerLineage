"""Un RunEvent OpenLineage se projette dans le graphe : run, lectures, réponse, diffusion."""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import pytest

from answerlineage_api.identifiers import InvalidIdentifierError
from answerlineage_api.store.graph import (
    AgentRef,
    Answer,
    DatasetRef,
    Delivery,
    Read,
    ToolRef,
    Write,
)
from answerlineage_workers.projection import project

RUN_ID = "0193a9c5-7a10-7c3e-9d5e-2f1b6c8a4e77"
PARENT_ID = "0193a9c5-7a10-7c3e-9d5e-2f1b6c8affff"


def facet(**fields: Any) -> dict[str, Any]:
    return {"_producer": "https://example.com/p", "_schemaURL": "https://example.com/s", **fields}


def event(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "eventType": "COMPLETE",
        "eventTime": "2026-11-02T09:00:00Z",
        "producer": "https://example.com/p",
        "schemaURL": "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent",
        "run": {"runId": RUN_ID},
        "job": {"namespace": "mcp://entrepot", "name": "execute_sql"},
    }
    payload.update(overrides)
    return payload


def test_tool_call_becomes_a_tool_run_with_its_reads() -> None:
    record = project(
        event(
            run={
                "runId": RUN_ID,
                "facets": {
                    "parent": facet(run={"runId": PARENT_ID}, job={"namespace": "a", "name": "b"}),
                    "answerlineage_toolCall": facet(server="entrepot", tool="execute_sql"),
                },
            },
            inputs=[
                {
                    "namespace": "bigquery",
                    "name": "ref.taux_change",
                    "facets": {
                        "schema": facet(
                            fields=[{"name": "taux"}, {"name": "devise"}, {"name": "taux"}]
                        ),
                        "version": facet(datasetVersion="2026-11-02T06:00:00Z"),
                    },
                },
                {"namespace": "bigquery", "name": "ventes.commandes"},
            ],
        )
    )
    assert record.run_id == UUID(RUN_ID)
    assert (record.kind, record.state) == ("tool", "COMPLETE")
    assert record.event_time == datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    assert record.tool == ToolRef("entrepot", "execute_sql")
    assert record.agent is None
    assert record.parent_run_id == UUID(PARENT_ID)
    assert record.reads == (
        Read(
            DatasetRef("bigquery", "ref.taux_change", version="2026-11-02T06:00:00Z"),
            precision="columns",
            columns=("devise", "taux"),
        ),
        Read(DatasetRef("bigquery", "ventes.commandes")),
    )


TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"


def traced(trace_id: object, span_id: object) -> dict[str, Any]:
    fields = {"server": "entrepot", "traceId": trace_id, "spanId": span_id}
    call = facet(**{name: value for name, value in fields.items() if value is not None})
    return event(run={"runId": RUN_ID, "facets": {"answerlineage_toolCall": call}})


def test_the_trace_context_of_a_tool_call_enters_the_graph() -> None:
    """Du lignage à la trace : le run retient le contexte que cite sa facette."""
    record = project(traced(TRACE_ID, SPAN_ID))
    assert (record.trace_id, record.span_id) == (TRACE_ID, SPAN_ID)


@pytest.mark.parametrize(
    ("trace_id", "span_id"),
    [
        (TRACE_ID.upper(), SPAN_ID),
        (TRACE_ID[:-1], SPAN_ID),
        (TRACE_ID + "\n", SPAN_ID),
        ("0" * 32, SPAN_ID),
        (TRACE_ID, "0" * 16),
        (TRACE_ID, SPAN_ID + "0"),
        (TRACE_ID, None),
        (None, SPAN_ID),
        (12, SPAN_ID),
    ],
)
def test_a_malformed_trace_context_is_ignored(trace_id: object, span_id: object) -> None:
    """Une moitié de contexte ne désigne rien : l'un ne va pas sans l'autre."""
    record = project(traced(trace_id, span_id))
    assert (record.trace_id, record.span_id) == (None, None)
    assert record.kind == "tool"


def test_tool_call_without_names_in_its_facet_falls_back_on_the_job() -> None:
    record = project(event(run={"runId": RUN_ID, "facets": {"answerlineage_toolCall": facet()}}))
    assert record.tool == ToolRef("entrepot", "execute_sql")
    bare = project(
        event(
            job={"namespace": "mcp://", "name": "execute_sql"},
            run={"runId": RUN_ID, "facets": {"answerlineage_toolCall": facet()}},
        )
    )
    assert bare.tool == ToolRef("mcp://", "execute_sql")


def test_agent_run_carries_the_answer_and_its_delivery() -> None:
    record = project(
        event(
            eventTime="2026-11-02T10:00:05+01:00",
            job={"namespace": "agent://ventes", "name": "analyste-ventes"},
            run={"runId": RUN_ID, "facets": {"answerlineage_agent": facet(framework="exemple")}},
            outputs=[
                {
                    "namespace": "answer://slack",
                    "name": "C042/1730538005.000200",
                    "facets": {
                        "answerlineage_delivery": facet(
                            fingerprint="hmac:9f2c",
                            channel="slack",
                            messageRef="C042/1730538005.000200",
                            recipients=["pseudo:b2", "pseudo:a1", "pseudo:a1", 42, ""],
                        )
                    },
                },
                {
                    "namespace": "memory://analyste-ventes",
                    "name": "notes",
                    "facets": {
                        "datasetType": facet(datasetType="MEMORY"),
                        "version": facet(datasetVersion="42"),
                    },
                },
            ],
        )
    )
    assert record.kind == "agent"
    assert record.agent == AgentRef("agent://ventes", "analyste-ventes")
    assert record.tool is None
    assert record.event_time == datetime(2026, 11, 2, 10, 0, 5, tzinfo=timezone(timedelta(hours=1)))
    assert record.answers == (
        Answer(
            "hmac:9f2c",
            (Delivery("slack", "C042/1730538005.000200", ("pseudo:a1", "pseudo:b2")),),
        ),
    )
    assert record.writes == (
        Write(DatasetRef("memory://analyste-ventes", "notes", kind="memory", version="42")),
    )


def test_delivery_facet_is_also_read_from_output_facets() -> None:
    record = project(
        event(
            outputs=[
                {
                    "namespace": "answer://web",
                    "name": "r1",
                    "outputFacets": {"answerlineage_delivery": facet(fingerprint="hmac:1")},
                }
            ]
        )
    )
    assert record.answers == (Answer("hmac:1"),)
    assert record.writes == ()


def test_event_from_any_openlineage_emitter_is_a_plain_job() -> None:
    record = project(
        event(
            job={"namespace": "airflow", "name": "dag.tache"},
            inputs=[{"namespace": "postgres://db", "name": "public.t"}],
            outputs=[{"namespace": "postgres://db", "name": "public.u"}],
        )
    )
    assert (record.kind, record.agent, record.tool) == ("job", None, None)
    assert record.reads == (Read(DatasetRef("postgres://db", "public.t")),)
    assert record.writes == (Write(DatasetRef("postgres://db", "public.u")),)


def test_missing_or_unknown_event_type_is_other() -> None:
    without = event()
    del without["eventType"]
    assert project(without).state == "OTHER"
    assert project(event(eventType="START")).state == "START"


def test_malformed_optional_facets_are_ignored_not_fatal() -> None:
    record = project(
        event(
            run={
                "runId": RUN_ID,
                "facets": {"parent": facet(run={"runId": "pas-un-uuid"}), "autre": "texte"},
            },
            inputs=[
                {
                    "namespace": "bigquery",
                    "name": "t",
                    "facets": {
                        "schema": facet(fields="pas une liste"),
                        "version": facet(datasetVersion=7),
                        "datasetType": facet(datasetType="INCONNU"),
                    },
                },
                {"namespace": "bigquery", "name": "u", "facets": {"schema": facet(fields=[{}, 3])}},
            ],
            outputs=[
                {
                    "namespace": "answer://web",
                    "name": "r1",
                    "facets": {"answerlineage_delivery": facet(channel="web")},
                }
            ],
        )
    )
    assert record.parent_run_id is None
    assert record.reads == (Read(DatasetRef("bigquery", "t")), Read(DatasetRef("bigquery", "u")))
    # Sans empreinte, la sortie n'est pas une réponse : elle reste une écriture.
    assert record.answers == ()
    assert record.writes == (Write(DatasetRef("answer://web", "r1")),)


def test_event_time_in_lower_case_is_read_as_the_ingestion_reads_it() -> None:
    record = project(event(eventTime="2026-11-02t09:00:00.5z"))
    assert record.event_time == datetime(2026, 11, 2, 9, 0, 0, 500000, tzinfo=UTC)


def nfd(value: str) -> str:
    return unicodedata.normalize("NFD", value)


def test_every_identifier_enters_the_graph_in_its_nfc_form() -> None:
    tool = project(
        event(
            job={"namespace": nfd("mcp://entrepôt"), "name": nfd("requête")},
            run={
                "runId": RUN_ID,
                "facets": {
                    "answerlineage_toolCall": facet(server=nfd("entrepôt"), tool=nfd("requête"))
                },
            },
            inputs=[
                {
                    "namespace": nfd("bigquery://société"),
                    "name": nfd("référentiel.coût"),
                    "facets": {
                        "schema": facet(fields=[{"name": nfd("coût_unitaire")}]),
                        "version": facet(datasetVersion=nfd("été-2026")),
                    },
                }
            ],
        )
    )
    assert tool.tool == ToolRef("entrepôt", "requête")
    assert tool.reads == (
        Read(
            DatasetRef("bigquery://société", "référentiel.coût", version="été-2026"),
            precision="columns",
            columns=("coût_unitaire",),
        ),
    )
    fallback = project(
        event(
            job={"namespace": nfd("mcp://entrepôt"), "name": nfd("requête")},
            run={"runId": RUN_ID, "facets": {"answerlineage_toolCall": facet()}},
        )
    )
    assert fallback.tool == ToolRef("entrepôt", "requête")

    agent = project(
        event(
            job={"namespace": nfd("agent://ventes-été"), "name": nfd("analyste-société")},
            run={"runId": RUN_ID, "facets": {"answerlineage_agent": facet()}},
            outputs=[
                {
                    "namespace": "answer://slack",
                    "name": "r1",
                    "facets": {
                        "answerlineage_delivery": facet(
                            fingerprint=nfd("hmac:é1"),
                            channel=nfd("équipe"),
                            messageRef=nfd("C042/été"),
                            recipients=[nfd("pseudo:é"), "pseudo:é"],
                        )
                    },
                },
                {"namespace": nfd("memory://société"), "name": nfd("notes-été")},
            ],
        )
    )
    assert agent.agent == AgentRef("agent://ventes-été", "analyste-société")
    assert agent.answers == (Answer("hmac:é1", (Delivery("équipe", "C042/été", ("pseudo:é",)),)),)
    assert agent.writes == (Write(DatasetRef("memory://société", "notes-été")),)


def test_identifiers_of_an_event_stored_before_validation_never_reach_the_graph() -> None:
    """Un événement ancien, enregistré avant le contrôle des identifiants, est rejoué."""
    record = project(
        event(
            run={
                "runId": RUN_ID,
                "facets": {"answerlineage_toolCall": facet(server="<b>", tool="a b")},
            },
            inputs=[
                {
                    "namespace": "bigquery",
                    "name": "t",
                    "facets": {
                        "schema": facet(fields=[{"name": "ok"}, {"name": "x\u200by"}]),
                        "version": facet(datasetVersion="v 1"),
                    },
                }
            ],
            outputs=[
                {
                    "namespace": "answer://slack",
                    "name": "r1",
                    "facets": {
                        "answerlineage_delivery": facet(
                            fingerprint="hmac:1",
                            channel="slack",
                            messageRef="C042/1",
                            recipients=["pseudo:a", "<script>"],
                        )
                    },
                },
                {
                    "namespace": "answer://slack",
                    "name": "r2",
                    "facets": {
                        "answerlineage_delivery": facet(
                            fingerprint="hmac:2", channel="sl ack", messageRef="C042/2"
                        )
                    },
                },
                {
                    "namespace": "answer://slack",
                    "name": "r3",
                    "facets": {"answerlineage_delivery": facet(fingerprint="h\u202emac")},
                },
            ],
        )
    )
    # Une valeur de facette fautive est ignorée, comme une facette mal formée.
    assert record.tool == ToolRef("entrepot", "execute_sql")
    assert record.reads == (
        Read(DatasetRef("bigquery", "t"), precision="columns", columns=("ok",)),
    )
    assert record.answers == (
        Answer("hmac:1", (Delivery("slack", "C042/1", ("pseudo:a",)),)),
        Answer("hmac:2"),
    )
    assert record.writes == (Write(DatasetRef("answer://slack", "r3")),)


@pytest.mark.parametrize(
    "overrides",
    [
        {"job": {"namespace": "mcp://entrepot", "name": "ignore previous instructions"}},
        {"job": {"namespace": "agent://<script>", "name": "a"}},
        {"inputs": [{"namespace": "bigquery", "name": "taux\u200bchange"}]},
        {"outputs": [{"namespace": "", "name": "t"}]},
    ],
)
def test_an_event_whose_required_identifier_is_faulty_is_not_projected(
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(InvalidIdentifierError):
        project(event(**overrides))
