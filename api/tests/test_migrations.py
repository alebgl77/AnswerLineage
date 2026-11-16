"""Les migrations créent le schéma et la descente le retire proprement."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import URL

from answerlineage_api.store import schema
from answerlineage_api.store.engine import (
    DatabaseConfigurationError,
    database_url_from_environment,
    normalize_url,
)
from answerlineage_api.store.migrate import downgrade, main, upgrade

pytestmark = pytest.mark.integration

_OBJECTS = text(
    """
    SELECT c.relkind, c.relname
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relname <> 'alembic_version'
      AND c.relname NOT LIKE 'alembic_version%'
    ORDER BY 1, 2
    """
)
_COLUMNS = text(
    """
    SELECT table_name, column_name, udt_name, is_nullable, column_default,
           is_identity, identity_generation
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name <> 'alembic_version'
    ORDER BY 1, 2
    """
)
_CONSTRAINTS = text(
    """
    SELECT conrelid::regclass::text, conname, contype::text, pg_get_constraintdef(oid)
    FROM pg_constraint
    WHERE connamespace = 'public'::regnamespace
      AND conrelid::regclass::text <> 'alembic_version'
    ORDER BY 1, 2
    """
)
_INDEXES = text(
    """
    SELECT tablename, indexname, indexdef
    FROM pg_indexes
    WHERE schemaname = 'public' AND tablename <> 'alembic_version'
    ORDER BY 1, 2
    """
)


def snapshot(url: URL) -> dict[str, list[tuple[Any, ...]]]:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return {
                "objects": [tuple(row) for row in connection.execute(_OBJECTS)],
                "columns": [tuple(row) for row in connection.execute(_COLUMNS)],
                "constraints": [tuple(row) for row in connection.execute(_CONSTRAINTS)],
                "indexes": [tuple(row) for row in connection.execute(_INDEXES)],
            }
    finally:
        engine.dispose()


def test_upgrade_creates_the_schema_and_downgrade_removes_it(empty_database: URL) -> None:
    assert snapshot(empty_database)["objects"] == []

    upgrade(empty_database)
    tables = {name for kind, name in snapshot(empty_database)["objects"] if kind == "r"}
    assert tables == set(schema.metadata.tables)
    assert {"run", "dataset", "read_edge", "write_edge", "answer", "delivery"} <= tables

    downgrade(empty_database, "base")
    assert snapshot(empty_database)["objects"] == []


def test_upgrade_is_repeatable_after_a_downgrade(empty_database: URL) -> None:
    upgrade(empty_database)
    first = snapshot(empty_database)
    downgrade(empty_database, "base")
    upgrade(empty_database)
    assert snapshot(empty_database) == first


def test_migrations_produce_exactly_the_declared_schema(empty_database: URL, database: URL) -> None:
    """Colonnes, contraintes, index : la base migrée égale le schéma déclaré."""
    engine = create_engine(empty_database)
    try:
        schema.metadata.create_all(engine)
    finally:
        engine.dispose()

    declared = snapshot(empty_database)
    migrated = snapshot(database)
    for part in ("objects", "columns", "constraints", "indexes"):
        assert migrated[part] == declared[part], part


def test_every_edge_carries_event_time_and_recorded_time(database: URL) -> None:
    columns = {(table, column) for table, column, *_ in snapshot(database)["columns"]}
    for edge in ("read_edge", "write_edge", "answer", "delivery"):
        assert (edge, "event_time") in columns
        assert (edge, "recorded_at") in columns


def test_command_line_applies_and_reverts_migrations(
    empty_database: URL, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "ANSWERLINEAGE_DATABASE_URL", empty_database.render_as_string(hide_password=False)
    )
    assert main(["upgrade"]) == 0
    assert snapshot(empty_database)["objects"] != []
    assert main(["downgrade", "base"]) == 0
    assert snapshot(empty_database)["objects"] == []


def test_missing_database_url_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANSWERLINEAGE_DATABASE_URL", raising=False)
    with pytest.raises(DatabaseConfigurationError, match="ANSWERLINEAGE_DATABASE_URL"):
        database_url_from_environment()


def test_events_received_before_the_coverage_get_their_emitter_and_kind(
    empty_database: URL,
) -> None:
    """Reprise des événements déjà reçus : émetteur sous sa forme NFC, appel d'outil ou non."""
    upgrade(empty_database, "0005")
    insert = text(
        """
        INSERT INTO event_raw (event_id, run_id, event_type, event_time, producer, payload)
        VALUES (:event_id, gen_random_uuid(), 'COMPLETE', now(), 'https://exemple.fr', :payload)
        """
    ).bindparams(bindparam("payload", type_=JSONB))
    events = {
        "appel": {
            "job": {"namespace": "mcp://société", "name": "execute_sql"},
            "run": {"facets": {"answerlineage_toolCall": {"server": "x"}}},
        },
        "agent": {"job": {"namespace": "agent://ventes", "name": "a"}, "run": {"facets": {}}},
        "sans-facette": {"job": {"namespace": "agent://ventes", "name": "a"}, "run": {}},
    }
    engine = create_engine(empty_database)
    try:
        with engine.begin() as connection:
            for event_id, payload in events.items():
                connection.execute(insert, {"event_id": event_id, "payload": payload})
        upgrade(empty_database)
        with engine.connect() as connection:
            found = connection.execute(
                text("SELECT event_id, emitter, tool_call FROM event_raw ORDER BY event_id")
            ).all()
    finally:
        engine.dispose()
    assert [tuple(row) for row in found] == [
        ("agent", "agent://ventes", False),
        ("appel", "mcp://société", True),
        ("sans-facette", "agent://ventes", False),
    ]


T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
# Réceptions des événements, puis passage du worker qui les a projetés.
R1, R2, R3, R4 = (T0 + timedelta(minutes=minutes) for minutes in (1, 2, 3, 4))
PROJECTED = T0 + timedelta(hours=1)
AGENT_RUN, TOOL_RUN, ORPHAN_RUN = (str(UUID(int=rank)) for rank in (1, 2, 3))
_PRODUCER = "https://exemple.fr"


def _facets(*names: str) -> dict[str, Any]:
    return {name: {"_producer": _PRODUCER, "_schemaURL": _PRODUCER} for name in names}


def _received_events() -> list[tuple[str, str, datetime, datetime, dict[str, Any]]]:
    """Les événements d'une demande, avec un rejeu antidaté de son appel d'outil."""
    agent_job = {"namespace": "agent://ventes", "name": "assistant-commercial"}
    tool_job = {"namespace": "mcp://entrepot", "name": "execute_sql"}
    tool_facets = {
        **_facets("parent"),
        "parent": {"run": {"runId": AGENT_RUN}, "job": agent_job},
        "answerlineage_toolCall": {"server": "entrepot", "tool": "execute_sql"},
    }
    rates = {
        "namespace": "bigquery",
        "name": "finance.ref.taux_change",
        "facets": {"version": {"datasetVersion": "v1"}},
    }
    delivery = {
        "fingerprint": "sha256:f1",
        "channel": "slack",
        "messageRef": "m-1",
        "recipients": ["p-1", "p-2"],
    }
    answer = {
        "namespace": "agent://ventes",
        "name": "assistant-commercial/reponses",
        "outputFacets": {"answerlineage_delivery": delivery},
    }
    notes = {"namespace": "memory://ventes", "name": "notes"}
    agent_facets = _facets("answerlineage_agent")
    return [
        ("debut", AGENT_RUN, T0, R1, {"job": agent_job, "run": {"facets": agent_facets}}),
        ("appel", TOOL_RUN, T0 + timedelta(seconds=5), R2,
         {"job": tool_job, "run": {"facets": tool_facets}, "inputs": [rates], "outputs": [notes]}),
        ("fin", AGENT_RUN, T0 + timedelta(seconds=30), R3,
         {"job": agent_job, "run": {"facets": agent_facets}, "outputs": [answer]}),
        ("antidate", TOOL_RUN, T0 - timedelta(days=1), R4,
         {"job": tool_job, "run": {"facets": tool_facets}, "inputs": [rates]}),
    ]  # fmt: skip


_GRAPH_BEFORE_0007 = """
    INSERT INTO agent (namespace, name, recorded_at)
        VALUES ('agent://ventes', 'assistant-commercial', :p);
    INSERT INTO tool (server, name, recorded_at) VALUES ('entrepot', 'execute_sql', :p);
    INSERT INTO dataset (namespace, name, kind, recorded_at) VALUES
        ('bigquery', 'finance.ref.taux_change', 'table', :p),
        ('memory://ventes', 'notes', 'table', :p),
        ('bigquery', 'ventes.suivi.commandes', 'table', :p);
    INSERT INTO dataset_version (dataset_id, version, recorded_at) VALUES (1, 'v1', :p);
    INSERT INTO run (run_id, kind, agent_id, tool_id, parent_run_id, state, state_time,
                     recorded_at) VALUES
        (:agent_run, 'agent', 1, NULL, NULL, 'COMPLETE', :t0, :p),
        (:tool_run, 'tool', NULL, 1, :agent_run, 'COMPLETE', :t0, :p),
        (:orphan_run, 'job', NULL, NULL, NULL, 'COMPLETE', :t0, :p);
    INSERT INTO read_edge (run_id, dataset_id, dataset_version_id, precision, origin, event_time,
                           recorded_at) VALUES
        (:tool_run, 1, 1, 'table', 'analysis', :backdated, :p),
        (:orphan_run, 3, NULL, 'table', 'analysis', :t0, :p);
    INSERT INTO write_edge (run_id, dataset_id, event_time, recorded_at)
        VALUES (:tool_run, 2, :t0, :p);
    INSERT INTO answer (run_id, fingerprint, event_time, recorded_at)
        VALUES (:agent_run, 'sha256:f1', :answered, :p);
    INSERT INTO delivery (answer_id, channel, message_ref, event_time, recorded_at)
        VALUES (1, 'slack', 'm-1', :answered, :p);
    INSERT INTO recipient (pseudonym, recorded_at) VALUES ('p-1', :p), ('p-2', :p);
    INSERT INTO delivery_recipient (delivery_id, recipient_id, recorded_at)
        VALUES (1, 1, :p), (1, 2, :p)
"""

_RECORDED_AFTER_0007 = {
    "run": "SELECT run_id::text, recorded_at FROM run ORDER BY 1",
    "read_edge": "SELECT run_id::text, event_time, last_event_time, recorded_at FROM read_edge"
    " ORDER BY 1",
    "write_edge": "SELECT recorded_at FROM write_edge",
    "answer": "SELECT recorded_at FROM answer",
    "delivery": "SELECT recorded_at FROM delivery",
    "delivery_recipient": "SELECT recipient_id, event_time, recorded_at FROM delivery_recipient"
    " ORDER BY 1",
    "dataset": "SELECT name, recorded_at FROM dataset ORDER BY 1",
    "dataset_version": "SELECT recorded_at FROM dataset_version",
    "recipient": "SELECT pseudonym, recorded_at FROM recipient ORDER BY 1",
    "agent": "SELECT recorded_at FROM agent",
    "tool": "SELECT recorded_at FROM tool",
}


def test_rows_recorded_before_the_reception_time_get_it_from_their_events(
    empty_database: URL,
) -> None:
    """Reprise : la réception la plus ancienne des événements qui font connaître chaque ligne.

    Une ligne qu'aucun événement reçu ne fait connaître garde son heure d'enregistrement :
    le passage du worker, qui suit toujours la réception.
    """
    upgrade(empty_database, "0006")
    insert = text(
        """
        INSERT INTO event_raw (event_id, run_id, event_type, event_time, producer, payload,
                               emitter, tool_call, recorded_at)
        VALUES (:event_id, :run_id, 'COMPLETE', :event_time, :producer, :payload,
                :emitter, :tool_call, :recorded_at)
        """
    ).bindparams(bindparam("payload", type_=JSONB))
    engine = create_engine(empty_database)
    try:
        with engine.begin() as connection:
            for event_id, run_id, event_time, received, payload in _received_events():
                connection.execute(
                    insert,
                    {
                        "event_id": event_id,
                        "run_id": run_id,
                        "event_time": event_time,
                        "producer": _PRODUCER,
                        "payload": {"eventTime": event_time.isoformat(), **payload},
                        "emitter": payload["job"]["namespace"],
                        "tool_call": "answerlineage_toolCall" in payload["run"]["facets"],
                        "recorded_at": received,
                    },
                )
            for statement in _GRAPH_BEFORE_0007.split(";"):
                connection.execute(
                    text(statement),
                    {
                        "p": PROJECTED,
                        "t0": T0,
                        "backdated": T0 - timedelta(days=1),
                        "answered": T0 + timedelta(seconds=30),
                        "agent_run": AGENT_RUN,
                        "tool_run": TOOL_RUN,
                        "orphan_run": ORPHAN_RUN,
                    },
                )
        upgrade(empty_database, "0007")
        with engine.connect() as connection:
            found = {
                table: [tuple(row) for row in connection.execute(text(sql))]
                for table, sql in _RECORDED_AFTER_0007.items()
            }
    finally:
        engine.dispose()

    assert found["run"] == [(AGENT_RUN, R1), (TOOL_RUN, R2), (ORPHAN_RUN, PROJECTED)]
    assert found["read_edge"] == [
        # Le rejeu antidaté élargit la lecture ; elle est connue depuis l'appel.
        (TOOL_RUN, T0 - timedelta(days=1), T0 + timedelta(seconds=5), R2),
        (ORPHAN_RUN, T0, T0, PROJECTED),
    ]
    assert found["write_edge"] == [(R2,)]
    assert found["answer"] == found["delivery"] == [(R3,)]
    answered = T0 + timedelta(seconds=30)
    assert found["delivery_recipient"] == [(1, answered, R3), (2, answered, R3)]
    assert found["dataset"] == [
        ("finance.ref.taux_change", R2),
        ("notes", R2),
        ("ventes.suivi.commandes", PROJECTED),
    ]
    assert found["dataset_version"] == [(R2,)]
    assert found["recipient"] == [("p-1", R3), ("p-2", R3)]
    assert (found["agent"], found["tool"]) == ([(R1,)], [(R2,)])


def test_only_postgresql_is_accepted() -> None:
    with pytest.raises(DatabaseConfigurationError, match="PostgreSQL"):
        normalize_url("sqlite:///lineage.db")
    assert normalize_url("postgresql://u:p@h/db").drivername == "postgresql+psycopg"
