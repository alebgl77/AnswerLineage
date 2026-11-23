"""Les migrations créent le schéma et la descente le retire proprement."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError

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


# Les tables déclarées, partitionnées ou non, sans leurs partitions.
_TABLES = text(
    """
    SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition
      AND c.relname <> 'alembic_version'
    """
)
_PARTITIONS = text(
    """
    SELECT p.relname, c.relname FROM pg_inherits i
    JOIN pg_class c ON c.oid = i.inhrelid JOIN pg_class p ON p.oid = i.inhparent
    WHERE p.relkind = 'p'
    ORDER BY 1, 2
    """
)


def tables_and_partitions(url: URL) -> tuple[set[str], dict[str, list[str]]]:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            tables = set(connection.execute(_TABLES).scalars())
            partitions: dict[str, list[str]] = {}
            for parent, partition in connection.execute(_PARTITIONS):
                partitions.setdefault(parent, []).append(partition)
            month = connection.execute(text("SELECT timezone('UTC', now())")).scalar_one()
    finally:
        engine.dispose()
    return tables, {"current": [f"{month:%Y_%m}"], **partitions}


def test_upgrade_creates_the_schema_and_downgrade_removes_it(empty_database: URL) -> None:
    assert snapshot(empty_database)["objects"] == []

    upgrade(empty_database)
    tables, partitions = tables_and_partitions(empty_database)
    assert tables == set(schema.metadata.tables)
    assert {"run", "dataset", "read_edge", "write_edge", "answer", "delivery"} <= tables
    # Le mois courant et les deux suivants, pour chaque table partitionnée.
    (current,) = partitions.pop("current")
    assert set(partitions) == {"coverage_report", "event_raw", "ingest_duplicate"}
    for parent, found in partitions.items():
        assert len(found) == 3 and found[0] == f"{parent}_{current}", parent

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


_RECEIVED_BEFORE_0008 = """
    INSERT INTO event_raw (event_id, run_id, event_type, event_time, producer, payload,
                           emitter, tool_call, recorded_at)
    VALUES ('ancien', :run, 'COMPLETE', :old, 'https://exemple.fr', '{}', 'mcp://e', true, :old),
           ('recent', :run, 'COMPLETE', now(), 'https://exemple.fr', '{}', 'mcp://e', true,
            now());
    INSERT INTO ingest_queue (event_id) VALUES ('ancien');
    INSERT INTO coverage_report (emitter, window_start, window_end, calls_seen, events_emitted,
                                 events_lost, uninspected, recorded_at)
    VALUES ('mcp://e', :old, :old + interval '1 minute', 3, 3, 0, 0, :old);
    INSERT INTO ingest_duplicate (emitter, day, duplicates) VALUES ('mcp://e', :old_day, 2)
"""
_KEPT = {
    "event_raw": "SELECT event_id, recorded_at FROM event_raw ORDER BY 1",
    "ingest_queue": "SELECT event_id FROM ingest_queue",
    "coverage_report": "SELECT emitter, calls_seen, recorded_at FROM coverage_report",
    "ingest_duplicate": "SELECT emitter, day, duplicates FROM ingest_duplicate",
}


def test_rows_received_before_the_partitions_keep_their_place(empty_database: URL) -> None:
    """Événements, file, relevés, doublons : rien ne se perd à la montée ni à la descente."""
    old = datetime(2025, 6, 10, 8, 30, tzinfo=UTC)
    upgrade(empty_database, "0007")
    engine = create_engine(empty_database)
    try:
        with engine.begin() as connection:
            for statement in _RECEIVED_BEFORE_0008.split(";"):
                connection.execute(
                    text(statement), {"run": AGENT_RUN, "old": old, "old_day": old.date()}
                )
        with engine.connect() as connection:
            before = {
                table: [tuple(row) for row in connection.execute(text(sql))]
                for table, sql in _KEPT.items()
            }

        upgrade(empty_database, "0008")
        _, partitions = tables_and_partitions(empty_database)
        with engine.connect() as connection:
            after = {
                table: [tuple(row) for row in connection.execute(text(sql))]
                for table, sql in _KEPT.items()
            }
            queued = connection.execute(text("SELECT recorded_month FROM ingest_queue")).all()
        assert after == before
        # Le mois des lignes reçues a sa partition, en plus des mois à venir.
        for parent in ("coverage_report", "event_raw", "ingest_duplicate"):
            assert f"{parent}_2025_06" in partitions[parent]
        assert queued == [(date(2025, 6, 1),)]

        downgrade(empty_database, "0007")
        with engine.connect() as connection:
            assert {
                table: [tuple(row) for row in connection.execute(text(sql))]
                for table, sql in _KEPT.items()
            } == before
    finally:
        engine.dispose()
    assert tables_and_partitions(empty_database)[1].keys() == {"current"}
    # La file retrouve sa clé étrangère vers les événements.
    constraints = {name for _, name, *_ in snapshot(empty_database)["constraints"]}
    assert "fk_ingest_queue_event_id_event_raw" in constraints


_REPORT_COLUMNS = (
    "emitter, window_start, window_end, calls_seen, events_emitted, events_lost, uninspected"
)


def test_reports_received_before_their_instance_keep_their_place(empty_database: URL) -> None:
    """Un relevé reçu avant l'instance prend l'instance vide ; la descente réunit les instances
    d'une fenêtre, en additionnant leurs compteurs."""
    window = {
        "start": datetime(2026, 11, 2, 9, 0, tzinfo=UTC),
        "end": datetime(2026, 11, 2, 9, 1, tzinfo=UTC),
    }
    insert = text(
        f"INSERT INTO coverage_report (instance, {_REPORT_COLUMNS})"  # noqa: S608 — constantes.
        " VALUES (:instance, 'mcp://entrepot', :start, :end, :seen, :seen, 0, 1)"
    )
    upgrade(empty_database, "0008")
    engine = create_engine(empty_database)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"INSERT INTO coverage_report ({_REPORT_COLUMNS})"  # noqa: S608 — constantes.
                    " VALUES ('mcp://entrepot', :start, :end, 10, 9, 1, 2)"
                ),
                window,
            )
        upgrade(empty_database, "0009")
        with engine.begin() as connection:
            kept = connection.execute(
                text("SELECT instance, calls_seen, events_lost FROM coverage_report")
            ).all()
            assert [tuple(row) for row in kept] == [("", 10, 1)]
            connection.execute(insert, {**window, "instance": "a", "seen": 5})
        with (
            pytest.raises(IntegrityError, match="ck_coverage_report_instance"),
            engine.begin() as connection,
        ):
            connection.execute(insert, {**window, "instance": "i" * 256, "seen": 1})

        downgrade(empty_database, "0008")
        with engine.connect() as connection:
            merged = connection.execute(
                text(f"SELECT {_REPORT_COLUMNS} FROM coverage_report")  # noqa: S608
            ).all()
        assert [tuple(row) for row in merged] == [
            ("mcp://entrepot", window["start"], window["end"], 15, 14, 1, 3)
        ]
    finally:
        engine.dispose()


def test_the_dead_letters_lose_nothing_on_the_way_up_or_down(empty_database: URL) -> None:
    """Un événement déjà remis à plus tard prend sa mise en file pour premier échec ; la
    descente remet en file les événements du rebut, avec leurs essais."""
    raw = text(
        """
        INSERT INTO event_raw (event_id, run_id, event_type, event_time, producer, payload,
                               emitter, tool_call)
        VALUES (:id, gen_random_uuid(), 'COMPLETE', now(), 'p', '{}', 'mcp://entrepot', true)
        """
    )
    queued = text(
        "INSERT INTO ingest_queue (event_id, recorded_month, attempts, last_error)"
        " SELECT event_id, recorded_month, :attempts, :error FROM event_raw WHERE event_id = :id"
    )
    upgrade(empty_database, "0010")
    engine = create_engine(empty_database)
    try:
        with engine.begin() as connection:
            for event_id, attempts in (("neuf", 0), ("remis", 2), ("rebut", 0)):
                connection.execute(raw, {"id": event_id})
                connection.execute(
                    queued,
                    {"id": event_id, "attempts": attempts, "error": "X" if attempts else None},
                )
        upgrade(empty_database, "0011")
        with engine.begin() as connection:
            kept = connection.execute(
                text(
                    "SELECT event_id, first_failed_at = enqueued_at FROM ingest_queue"
                    " ORDER BY event_id"
                )
            ).all()
            assert [tuple(row) for row in kept] == [
                ("neuf", None),
                ("rebut", None),
                ("remis", True),
            ]
            connection.execute(
                text(
                    """
                    WITH moved AS (DELETE FROM ingest_queue WHERE event_id = 'rebut'
                                   RETURNING event_id, recorded_month)
                    INSERT INTO dead_letter (event_id, recorded_month, emitter, reason, attempts,
                                             first_failed_at, last_failed_at)
                    SELECT event_id, recorded_month, 'mcp://entrepot', 'invalid_event', 3,
                           now(), now()
                    FROM moved
                    """
                )
            )
        with (
            pytest.raises(IntegrityError, match="ck_dead_letter_reason"),
            engine.begin() as connection,
        ):
            connection.execute(text("UPDATE dead_letter SET reason = 'ValueError'"))

        downgrade(empty_database, "0010")
        with engine.connect() as connection:
            back = connection.execute(
                text("SELECT event_id, attempts, last_error FROM ingest_queue ORDER BY event_id")
            ).all()
        assert [tuple(row) for row in back] == [
            ("neuf", 0, None),
            ("rebut", 3, "invalid_event"),
            ("remis", 2, "X"),
        ]
    finally:
        engine.dispose()


# Un pseudonyme gardé, et ce qu'un émetteur a pu envoyer avant que la forme soit exigée.
_PSEUDONYM = "hmac-sha256:" + "ab" * 32
_IN_CLEAR = ("camille.martin@exemple.fr", "p-1")


_RECIPIENTS_BEFORE_0017 = """
    INSERT INTO run (run_id, kind, state, state_time)
        VALUES (:run, 'agent', 'COMPLETE', now());
    INSERT INTO answer (run_id, fingerprint, event_time) VALUES (:run, 'sha256:f1', now());
    INSERT INTO delivery (answer_id, channel, message_ref, event_time)
        VALUES (1, 'slack', 'm-1', now());
    INSERT INTO recipient (pseudonym) VALUES (:kept), (:clear), (:short);
    INSERT INTO delivery_recipient (delivery_id, recipient_id, event_time)
        VALUES (1, 1, now()), (1, 2, now()), (1, 3, now());
    INSERT INTO incident (id, state, severity, window_start, window_end)
        VALUES (:run, 'qualified', 'high', now() - interval '1 hour', now());
    INSERT INTO incident_answer (incident_id, run_id, fingerprint, produced_at)
        VALUES (:run, :run, 'sha256:f1', now());
    INSERT INTO incident_recipient (incident_id, run_id, fingerprint, pseudonym)
        VALUES (:run, :run, 'sha256:f1', :kept), (:run, :run, 'sha256:f1', :clear)
"""


def _delivery_event(*recipients: object, beside: object = None) -> dict[str, Any]:
    """Le payload d'un événement d'agent qui diffuse sa réponse à ces destinataires."""
    delivery = {"fingerprint": "sha256:f1", "channel": "slack", "messageRef": "m-1"}
    return {
        "outputs": [
            "pas un objet",
            {
                "namespace": "agent://ventes",
                "name": "assistant-commercial/reponses",
                "facets": {"answerlineage_delivery": {**delivery, "recipients": beside}},
                "outputFacets": {
                    "answerlineage_delivery": {**delivery, "recipients": list(recipients)}
                },
            },
        ]
    }


def test_recipients_that_are_not_pseudonyms_leave_the_base_on_the_way_up(
    empty_database: URL,
) -> None:
    """Une valeur reçue avant que la forme du pseudonyme soit exigée est peut-être une identité
    en clair : la montée la retire des événements bruts, du graphe et des incidents."""
    upgrade(empty_database, "0016")
    engine = create_engine(empty_database)
    raw = text(
        """
        INSERT INTO event_raw (event_id, run_id, event_type, event_time, producer, payload,
                               emitter, tool_call)
        VALUES (:id, :run, 'COMPLETE', now(), 'p', :payload, 'agent://ventes', false)
        """
    ).bindparams(bindparam("payload", type_=JSONB))
    events = {
        "mêlé": _delivery_event(_PSEUDONYM, _IN_CLEAR[0], 33612345678, None, beside=_IN_CLEAR[1]),
        "nul": _delivery_event(_PSEUDONYM, beside=None),
        "sain": _delivery_event(_PSEUDONYM),
        "sans sortie": {"outputs": "aucune"},
    }
    try:
        with engine.begin() as connection:
            for event_id, payload in events.items():
                connection.execute(raw, {"id": event_id, "run": AGENT_RUN, "payload": payload})
            values = {
                "run": AGENT_RUN,
                "kept": _PSEUDONYM,
                "clear": _IN_CLEAR[0],
                "short": _IN_CLEAR[1],
            }
            for statement in _RECIPIENTS_BEFORE_0017.split(";"):
                connection.execute(text(statement), values)
        upgrade(empty_database, "0017")
        with engine.connect() as connection:
            payloads: dict[str, Any] = dict(
                connection.execute(text("SELECT event_id, payload FROM event_raw")).all()
            )
            graph = connection.execute(
                text(
                    "SELECT c.pseudonym FROM delivery_recipient l"
                    " JOIN recipient c ON c.id = l.recipient_id ORDER BY 1"
                )
            ).scalars()
            assert list(graph) == [_PSEUDONYM]
            assert list(connection.execute(text("SELECT pseudonym FROM recipient")).scalars()) == [
                _PSEUDONYM
            ]
            assert list(
                connection.execute(text("SELECT pseudonym FROM incident_recipient")).scalars()
            ) == [_PSEUDONYM]
        mixed = payloads["mêlé"]["outputs"][1]
        assert mixed["outputFacets"]["answerlineage_delivery"]["recipients"] == [_PSEUDONYM]
        assert mixed["facets"]["answerlineage_delivery"]["recipients"] == []
        assert mixed["outputFacets"]["answerlineage_delivery"]["fingerprint"] == "sha256:f1"
        assert payloads["mêlé"]["outputs"][0] == "pas un objet"
        for unchanged in ("nul", "sain", "sans sortie"):
            assert payloads[unchanged] == events[unchanged]
        dumped = str(payloads)
        for clear in _IN_CLEAR:
            assert clear not in dumped
        assert "33612345678" not in dumped

        downgrade(empty_database, "0016")
        with engine.connect() as connection:
            assert list(connection.execute(text("SELECT pseudonym FROM recipient")).scalars()) == [
                _PSEUDONYM
            ]
    finally:
        engine.dispose()


_REVEALED = text(
    """
    INSERT INTO recipient_revelation (revealed_at, author_issuer, author_subject, reason,
                                      pseudonym, candidates, result)
    VALUES (:at, 'https://idp.exemple.fr', 'personne-1', :reason, :pseudonym, :candidates,
            'matched')
    RETURNING id, revealed_at
    """
)


def test_the_revelation_journal_is_appended_to_never_changed(database: URL) -> None:
    """Critère : le journal est en ajout seul. L'heure est celle de la base, et ni une
    modification, ni une suppression, ni une troncature ne passent, même pour le propriétaire
    de la table, à qui ces droits sont aussi retirés."""
    engine = create_engine(database)
    entry = {"at": T0, "reason": "recall", "pseudonym": _PSEUDONYM, "candidates": 3}
    try:
        with engine.begin() as connection:
            before = connection.execute(text("SELECT now()")).scalar_one()
            _, revealed_at = connection.execute(_REVEALED, entry).one()
        # L'heure donnée à l'insertion est ignorée : c'est celle de la transaction.
        assert revealed_at == before
        for change in (
            "UPDATE recipient_revelation SET reason = 'legal_obligation'",
            "UPDATE recipient_revelation SET revealed_at = now() - interval '1 day'",
            "DELETE FROM recipient_revelation",
            "TRUNCATE recipient_revelation",
        ):
            with pytest.raises(IntegrityError) as refused, engine.begin() as connection:
                connection.execute(text(change))
            diagnostic = getattr(refused.value.orig, "diag", None)
            assert getattr(diagnostic, "constraint_name", None) == (
                "ck_recipient_revelation_append_only"
            )
        with engine.connect() as connection:
            kept = connection.execute(text("SELECT count(*) FROM recipient_revelation"))
            assert kept.scalar() == 1
            granted = connection.execute(
                text(
                    "SELECT a.privilege_type FROM pg_class c, aclexplode(c.relacl) a"
                    " WHERE c.relname = 'recipient_revelation' AND a.grantee = c.relowner"
                )
            ).scalars()
            assert {"SELECT", "INSERT"} <= set(granted)
            withdrawn = connection.execute(
                text(
                    "SELECT a.privilege_type FROM pg_class c, aclexplode(c.relacl) a"
                    " WHERE c.relname = 'recipient_revelation'"
                    " AND a.privilege_type IN ('UPDATE', 'DELETE', 'TRUNCATE')"
                )
            ).scalars()
            assert list(withdrawn) == []
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("change", "constraint"),
    [
        ({"reason": "curiosite"}, "ck_recipient_revelation_reason"),
        ({"pseudonym": "camille.martin@exemple.fr"}, "ck_recipient_revelation_pseudonym"),
        ({"candidates": 0}, "ck_recipient_revelation_candidates"),
        ({"candidates": 101}, "ck_recipient_revelation_candidates"),
    ],
)
def test_the_revelation_journal_holds_only_closed_lists_and_pseudonyms(
    database: URL, change: dict[str, Any], constraint: str
) -> None:
    engine = create_engine(database)
    entry = {"at": T0, "reason": "recall", "pseudonym": _PSEUDONYM, "candidates": 3, **change}
    try:
        with pytest.raises(IntegrityError, match=constraint), engine.begin() as connection:
            connection.execute(_REVEALED, entry)
    finally:
        engine.dispose()


def test_the_partition_key_follows_the_reception(database: URL) -> None:
    """Le mois de partition est celui de la réception : un autre est refusé."""
    engine = create_engine(database)
    try:
        with (
            pytest.raises(IntegrityError, match="ck_event_raw_recorded_month"),
            engine.begin() as c,
        ):
            c.execute(
                text(
                    """
                    INSERT INTO event_raw (event_id, run_id, event_type, event_time, producer,
                                           payload, emitter, tool_call, recorded_month)
                    VALUES ('x', gen_random_uuid(), 'COMPLETE', now(), 'p', '{}', 'e', true,
                            CAST(date_trunc('month', now() + interval '1 month') AS date))
                    """
                )
            )
    finally:
        engine.dispose()


def test_only_postgresql_is_accepted() -> None:
    with pytest.raises(DatabaseConfigurationError, match="PostgreSQL"):
        normalize_url("sqlite:///lineage.db")
    assert normalize_url("postgresql://u:p@h/db").drivername == "postgresql+psycopg"
