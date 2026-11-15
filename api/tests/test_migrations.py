"""Les migrations créent le schéma et la descente le retire proprement."""

from __future__ import annotations

from typing import Any

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


def test_only_postgresql_is_accepted() -> None:
    with pytest.raises(DatabaseConfigurationError, match="PostgreSQL"):
        normalize_url("sqlite:///lineage.db")
    assert normalize_url("postgresql://u:p@h/db").drivername == "postgresql+psycopg"
