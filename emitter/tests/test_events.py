"""Les briques des événements : un RunEvent OpenLineage, ses facettes, son heure.

Les émetteurs construisent leurs événements avec elles ; chaque facette propre
se réclame d'un schéma publié dans ``spec/facets/``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from answerlineage_emitter import __version__
from answerlineage_emitter.events import (
    AGENT_FACET,
    AGENT_FACET_SCHEMA,
    DELIVERY_FACET,
    DELIVERY_FACET_SCHEMA,
    KEYS_FACET_SCHEMA,
    PARENT_FACET_SCHEMA,
    QOS_FACET_SCHEMA,
    RUN_EVENT_SCHEMA,
    SCHEMA_FACET_SCHEMA,
    TOOL_CALL_FACET,
    TOOL_CALL_FACET_SCHEMA,
    TOOL_CALL_FACET_SCHEMA_READ_KEYS,
    TOOL_CALL_FACET_SCHEMA_RUN_ID_TAG,
    facet,
    format_event_time,
    parent_facet,
    producer,
    run_event,
    schema_facet,
)

ROOT = Path(__file__).resolve().parents[2]
RUN = UUID("0193a9c5-7a10-7c3e-9d5e-2f1b6c8a4e77")
PRODUCER = "https://exemple.org/mon-emetteur"


def test_the_producer_names_the_component_at_its_version() -> None:
    assert producer("proxy") == (
        f"https://github.com/alebgl77/AnswerLineage/tree/{__version__}/proxy"
    )


def test_the_event_time_is_written_in_utc_to_the_microsecond() -> None:
    moment = datetime(2026, 11, 2, 10, 0, 0, 5, tzinfo=timezone(timedelta(hours=1)))
    assert format_event_time(moment) == "2026-11-02T09:00:00.000005Z"


def test_a_facet_carries_its_producer_and_schema_before_its_fields() -> None:
    built = facet(PRODUCER, TOOL_CALL_FACET_SCHEMA, server="entrepot")
    assert list(built) == ["_producer", "_schemaURL", "server"]


def test_the_schema_facet_lists_the_columns_read() -> None:
    assert schema_facet(PRODUCER, ("devise", "taux")) == {
        "_producer": PRODUCER,
        "_schemaURL": SCHEMA_FACET_SCHEMA,
        "fields": [{"name": "devise"}, {"name": "taux"}],
    }


def test_a_run_event_has_the_shape_of_the_openlineage_run_event() -> None:
    facets = {TOOL_CALL_FACET: facet(PRODUCER, TOOL_CALL_FACET_SCHEMA, server="entrepot")}
    job = {"namespace": "mcp://entrepot", "name": "execute_sql"}
    event = run_event(
        producer=PRODUCER,
        event_type="COMPLETE",
        event_time=datetime(2026, 11, 2, 9, 0, tzinfo=UTC),
        run_id=RUN,
        run_facets=facets,
        job=job,
    )
    assert event == {
        "eventType": "COMPLETE",
        "eventTime": "2026-11-02T09:00:00.000000Z",
        "producer": PRODUCER,
        "schemaURL": RUN_EVENT_SCHEMA,
        "run": {"runId": str(RUN), "facets": facets},
        "job": job,
        "inputs": [],
        "outputs": [],
    }
    assert list(event) == [
        "eventType",
        "eventTime",
        "producer",
        "schemaURL",
        "run",
        "job",
        "inputs",
        "outputs",
    ]


def test_the_own_facet_schemas_are_published_files_of_the_repository() -> None:
    prefix = "https://raw.githubusercontent.com/alebgl77/AnswerLineage/main/"
    for address in (
        TOOL_CALL_FACET_SCHEMA,
        TOOL_CALL_FACET_SCHEMA_READ_KEYS,
        TOOL_CALL_FACET_SCHEMA_RUN_ID_TAG,
        KEYS_FACET_SCHEMA,
        QOS_FACET_SCHEMA,
        AGENT_FACET_SCHEMA,
        DELIVERY_FACET_SCHEMA,
    ):
        path, _, pointer = address.removeprefix(prefix).partition("#")
        assert (ROOT / path).is_file(), address
        assert pointer == "/$defs/" + Path(path).stem
    assert AGENT_FACET == "answerlineage_agent"
    assert DELIVERY_FACET == "answerlineage_delivery"


def test_the_parent_facet_names_the_run_and_the_job_of_the_parent() -> None:
    job = {"namespace": "agent://finance", "name": "analyste-change"}
    assert parent_facet(PRODUCER, RUN, job) == {
        "_producer": PRODUCER,
        "_schemaURL": PARENT_FACET_SCHEMA,
        "run": {"runId": str(RUN)},
        "job": job,
    }
