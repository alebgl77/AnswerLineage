"""Un RunEvent OpenLineage se projette dans le graphe : run, lectures, réponse, diffusion."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

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


def test_tool_call_without_names_in_its_facet_falls_back_on_the_job() -> None:
    record = project(event(run={"runId": RUN_ID, "facets": {"answerlineage_toolCall": facet()}}))
    assert record.tool == ToolRef("entrepot", "execute_sql")


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
