"""Mettre un incident en quarantaine, la lever, et diffuser les périmètres aux proxys.

La quarantaine est un objet rattaché à l'incident : activée puis levée par deux
actions explicites et idempotentes, ses heures posées par la base, ses états et
ses transitions tenus par la base. Chaque activation et chaque levée s'annonce
par webhook, dans sa transaction. Les proxys lisent les périmètres en
quarantaine active sous une version qui leur évite de retélécharger ce qui n'a
pas changé. Une demande fautive est refusée sans rien enregistrer ni recopier.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_api.incidents import quarantine
from answerlineage_api.incidents.models import ActiveQuarantines, validate_quarantine
from answerlineage_api.incidents.quarantine import Diffusion, version_of
from answerlineage_api.store import schema
from api.tests.issuer import authentication, signed_in
from api.tests.test_incidents import DECLARATION, declaration, first_dataset

CANARY = "canari-6b2f"
UNKNOWN = "0193a9c5-7a10-4c3e-9d5e-2f1b6c8a0000"
WARN = {"mode": "warn"}


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(engine, authentication=authentication()))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://localhost", headers=signed_in()
    ) as client:
        yield client


async def rows(engine: AsyncEngine, sql: str, **params: Any) -> list[tuple[Any, ...]]:
    async with engine.connect() as conn:
        return [tuple(row) for row in await conn.execute(text(sql), params)]


async def declared(client: httpx.AsyncClient, name: str = "ventes.suivi.commandes") -> str:
    response = await client.post("/api/v1/incidents", json=declaration(first_dataset("name", name)))
    assert response.status_code == 201, response.text
    identifier: str = response.json()["id"]
    return identifier


async def quarantined(client: httpx.AsyncClient, incident_id: str) -> dict[str, Any]:
    response = await client.post(f"/api/v1/incidents/{incident_id}/quarantine", json=WARN)
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()
    return found


async def lifted(client: httpx.AsyncClient, incident_id: str) -> dict[str, Any]:
    response = await client.post(f"/api/v1/incidents/{incident_id}/quarantine/lift")
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()
    return found


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value)


async def in_service(engine: AsyncEngine) -> None:
    """Un worker à webhook est passé : les changements d'état sont mis en file."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO webhook_stats (id, in_service_until)"
                " VALUES (true, now() + interval '1 hour')"
            )
        )


# Validation, sans base.


@pytest.mark.parametrize(
    ("raw", "path", "code"),
    [
        ({"mode": CANARY}, "/mode", "literal_error"),
        ({"mode": "block"}, "/mode", "literal_error"),
        ({}, "/mode", "missing"),
        ({"mode": 1}, "/mode", "literal_error"),
        ({"mode": "warn", f"motif-{CANARY}": CANARY}, "", "unknown_field"),
        ([CANARY], "", "not_an_object"),
    ],
)
def test_a_faulty_request_is_refused_with_its_reason_and_without_its_value(
    raw: object, path: str, code: str
) -> None:
    asked, errors = validate_quarantine(raw)
    assert asked is None
    assert (path, code) in {(error.path, error.type) for error in errors}
    assert CANARY not in json.dumps([error.model_dump() for error in errors])


def test_the_only_mode_today_is_warn() -> None:
    asked, errors = validate_quarantine({"mode": "warn"})
    assert asked is not None and asked.mode == "warn" and errors == []


def test_the_version_follows_the_list_and_nothing_else() -> None:
    assert version_of([]) == version_of([])
    assert len(version_of([])) == 64
    diffusion = Diffusion(ActiveQuarantines(version=version_of([]), quarantines=[]))
    etag = diffusion.etag
    assert etag == f'"{version_of([])}"'
    matching: tuple[str, ...] = (etag, f"W/{etag}", f'"autre", {etag}', "*")
    for header in matching:
        assert diffusion.matches(header), header
    others: tuple[str | None, ...] = (
        None,
        '"autre"',
        version_of([]),
        etag + " " * quarantine.MAX_IF_NONE_MATCH,
    )
    for other in others:
        assert not diffusion.matches(other), other


# Activer et lever.


@pytest.mark.integration
@pytest.mark.anyio
async def test_an_incident_put_in_quarantine_is_active_in_its_mode_from_the_base_clock(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    incident_id = await declared(client)
    before = (await client.get(f"/api/v1/incidents/{incident_id}")).json()
    assert before["quarantine"] is None

    change = await quarantined(client, incident_id)

    quarantine_now = change["quarantine"]
    assert change == {
        "incident_id": incident_id,
        "changed": True,
        "quarantine": {
            "state": "active",
            "mode": "warn",
            "activated_at": quarantine_now["activated_at"],
            "lifted_at": None,
        },
    }
    assert quarantine_now["activated_at"].endswith("Z")
    # L'heure est celle de la base, à l'activation.
    assert await rows(
        engine,
        "SELECT state, mode, activated_at <= now(), lifted_at FROM incident_quarantine",
    ) == [("active", "warn", True, None)]
    read = (await client.get(f"/api/v1/incidents/{incident_id}")).json()
    listed = (await client.get("/api/v1/incidents")).json()["incidents"][0]
    assert read["quarantine"] == listed["quarantine"] == quarantine_now


@pytest.mark.integration
@pytest.mark.anyio
async def test_activating_twice_or_lifting_twice_changes_nothing_the_second_time(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    incident_id = await declared(client)
    first = await quarantined(client, incident_id)
    again = await quarantined(client, incident_id)
    assert again == {**first, "changed": False}

    lift = await lifted(client, incident_id)
    assert lift["changed"] is True
    assert lift["quarantine"]["state"] == "lifted"
    assert lift["quarantine"]["activated_at"] == first["quarantine"]["activated_at"]
    assert instant(lift["quarantine"]["lifted_at"]) >= instant(lift["quarantine"]["activated_at"])
    assert await lifted(client, incident_id) == {**lift, "changed": False}

    assert await rows(engine, "SELECT state FROM incident_quarantine") == [("lifted",)]
    read = (await client.get(f"/api/v1/incidents/{incident_id}")).json()
    assert read["quarantine"] == lift["quarantine"]


@pytest.mark.integration
@pytest.mark.anyio
async def test_a_lifted_quarantine_never_returns_a_new_activation_opens_another(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    incident_id = await declared(client)
    first = await quarantined(client, incident_id)
    await lifted(client, incident_id)
    second = await quarantined(client, incident_id)

    assert second["changed"] is True
    assert second["quarantine"]["state"] == "active"
    assert instant(second["quarantine"]["activated_at"]) > instant(
        first["quarantine"]["activated_at"]
    )
    assert await rows(engine, "SELECT state FROM incident_quarantine ORDER BY id") == [
        ("lifted",),
        ("active",),
    ]
    read = (await client.get(f"/api/v1/incidents/{incident_id}")).json()
    assert read["quarantine"] == second["quarantine"]


@pytest.mark.integration
@pytest.mark.anyio
async def test_an_unknown_or_malformed_incident_is_refused_without_its_value(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    for path in (
        f"/api/v1/incidents/{UNKNOWN}/quarantine",
        f"/api/v1/incidents/{UNKNOWN}/quarantine/lift",
    ):
        unknown = await client.post(path, json=WARN)
        assert (unknown.status_code, unknown.json()) == (404, {"error": "unknown_incident"}), path
    for path in (
        f"/api/v1/incidents/{CANARY}/quarantine",
        f"/api/v1/incidents/{CANARY}/quarantine/lift",
    ):
        malformed = await client.post(path, json=WARN)
        assert malformed.status_code == 422, path
        assert CANARY not in malformed.text
    assert await rows(engine, "SELECT count(*) FROM incident_quarantine") == [(0,)]


@pytest.mark.integration
@pytest.mark.anyio
async def test_an_unknown_mode_a_free_text_or_a_broken_body_is_refused_and_nothing_recorded(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    incident_id = await declared(client)
    path = f"/api/v1/incidents/{incident_id}/quarantine"
    for body, field, code in (
        ({"mode": CANARY}, "/mode", "literal_error"),
        ({"mode": "warn", "motif": CANARY}, "", "unknown_field"),
        ({}, "/mode", "missing"),
    ):
        refused = await client.post(path, json=body)
        assert refused.status_code == 422
        assert (field, code) in {
            (error["path"], error["type"]) for error in refused.json()["errors"]
        }
        assert CANARY not in refused.text

    broken = await client.post(path, content=b"{", headers={"content-type": "application/json"})
    assert (broken.status_code, broken.json()) == (400, {"error": "invalid_json"})
    large = await client.post(path, json={"mode": "warn", "x": "a" * 5_000})
    assert (large.status_code, large.json()) == (413, {"error": "body_too_large"})
    assert await rows(engine, "SELECT count(*) FROM incident_quarantine") == [(0,)]


@pytest.mark.integration
@pytest.mark.anyio
async def test_lifting_an_incident_never_put_in_quarantine_is_refused(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    incident_id = await declared(client)
    response = await client.post(f"/api/v1/incidents/{incident_id}/quarantine/lift")
    assert (response.status_code, response.json()) == (409, {"error": "not_quarantined"})
    assert await rows(engine, "SELECT count(*) FROM incident_quarantine") == [(0,)]


@pytest.mark.integration
@pytest.mark.anyio
async def test_the_active_quarantines_are_bounded_beyond_the_bound_the_activation_is_refused(
    client: httpx.AsyncClient, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(quarantine, "MAX_ACTIVE_QUARANTINES", 2)
    first, second, third = [await declared(client, f"ventes.t{rank}") for rank in range(3)]
    await quarantined(client, first)
    await quarantined(client, second)

    refused = await client.post(f"/api/v1/incidents/{third}/quarantine", json=WARN)
    assert (refused.status_code, refused.json()) == (409, {"error": "too_many_quarantines"})
    # Un incident déjà en quarantaine la garde, même à la borne.
    assert (await quarantined(client, first))["changed"] is False

    await lifted(client, first)
    assert (await quarantined(client, third))["changed"] is True
    assert await rows(
        engine, "SELECT count(*) FROM incident_quarantine WHERE state = 'active'"
    ) == [(2,)]


# La base tient les états et les transitions.


@pytest.mark.integration
@pytest.mark.anyio
async def test_the_base_refuses_any_state_or_transition_outside_the_closed_list(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    incident_id = await declared(client)
    await quarantined(client, incident_id)

    refusals = {
        "ck_incident_quarantine_mode": "UPDATE incident_quarantine SET mode = 'block'",
        "ck_incident_quarantine_state": "UPDATE incident_quarantine SET state = 'paused'",
        "ck_incident_quarantine_lifted": "UPDATE incident_quarantine SET state = 'lifted'",
        "ck_incident_quarantine_lifted_after": (
            "UPDATE incident_quarantine SET state = 'lifted',"
            " lifted_at = activated_at - interval '1 second'"
        ),
        "uq_incident_quarantine_active": (
            "INSERT INTO incident_quarantine (incident_id, mode) VALUES (:id, 'warn')"
        ),
        # Une quarantaine ne naît pas levée.
        "ck_incident_quarantine_transition": (
            "INSERT INTO incident_quarantine (incident_id, mode, state, lifted_at)"
            " VALUES (:id, 'warn', 'lifted', now())"
        ),
    }
    for name, statement in refusals.items():
        with pytest.raises(IntegrityError) as refused:
            async with engine.begin() as conn:
                await conn.execute(text(statement), {"id": UUID(incident_id)})
        assert constraint(refused.value) == name, statement

    await lifted(client, incident_id)
    # Une levée est définitive : ni retour à l'état actif, ni heure, ni mode changés.
    for statement in (
        "UPDATE incident_quarantine SET state = 'active', lifted_at = NULL",
        "UPDATE incident_quarantine SET lifted_at = lifted_at + interval '1 second'",
        "UPDATE incident_quarantine SET activated_at = activated_at - interval '1 second'",
    ):
        with pytest.raises(IntegrityError) as refused:
            async with engine.begin() as conn:
                await conn.execute(text(statement))
        assert constraint(refused.value) == "ck_incident_quarantine_transition", statement


def constraint(error: IntegrityError) -> str | None:
    """La contrainte que la base nomme dans son refus."""
    name: str | None = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
    return name


@pytest.mark.integration
def test_the_migration_holds_the_transitions_as_the_declared_schema_does(
    empty_database: URL, database: URL
) -> None:
    declared_engine = create_engine(empty_database)
    try:
        schema.metadata.create_all(declared_engine)
    finally:
        declared_engine.dispose()

    def transitions(url: URL) -> list[Any]:
        engine = create_engine(url)
        try:
            with engine.connect() as connection:
                return list(
                    connection.execute(
                        text(
                            "SELECT proname, prosrc FROM pg_proc"
                            " WHERE proname = 'incident_quarantine_transition'"
                        )
                    )
                )
        finally:
            engine.dispose()

    migrated, declared = transitions(database), transitions(empty_database)
    assert len(migrated) == 1
    assert [tuple(row) for row in migrated] == [tuple(row) for row in declared]


# Les annonces.


@pytest.mark.integration
@pytest.mark.anyio
async def test_each_activation_and_each_lift_is_announced_in_its_transaction_once(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    await in_service(engine)
    incident_id = await declared(client)
    first = await quarantined(client, incident_id)
    await quarantined(client, incident_id)
    lift = await lifted(client, incident_id)
    await lifted(client, incident_id)

    found = await rows(
        engine,
        """
        SELECT w.event_type, w.quarantine_state, w.quarantine_mode, w.analysis_state,
               w.occurred_at = CASE w.event_type
                                   WHEN 'incident.declared' THEN i.declared_at
                                   WHEN 'incident.quarantined' THEN q.activated_at
                                   ELSE q.lifted_at END AS at_the_change,
               w.occurred_at
        FROM webhook_queue w
        JOIN incident i ON i.id = w.incident_id
        JOIN incident_quarantine q ON q.incident_id = w.incident_id
        ORDER BY w.id
        """,
    )
    assert [row[:5] for row in found] == [
        ("incident.declared", None, None, "pending", True),
        ("incident.quarantined", "active", "warn", "pending", True),
        ("incident.quarantine_lifted", "lifted", "warn", "pending", True),
    ]
    assert found[1][5] == instant(first["quarantine"]["activated_at"])
    assert found[2][5] == instant(lift["quarantine"]["lifted_at"])


@pytest.mark.integration
@pytest.mark.anyio
async def test_an_activation_or_a_lift_rolled_back_leaves_nothing_announced_nor_recorded(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    await in_service(engine)
    incident_id = UUID(await declared(client))

    class AbandonedError(Exception):
        pass

    with pytest.raises(AbandonedError):
        async with engine.begin() as conn:
            change = await quarantine.activate(conn, incident_id, "warn")
            assert change is not None and change.changed
            raise AbandonedError
    assert await rows(engine, "SELECT count(*) FROM incident_quarantine") == [(0,)]
    assert await rows(engine, "SELECT event_type FROM webhook_queue") == [("incident.declared",)]

    await quarantined(client, str(incident_id))
    with pytest.raises(AbandonedError):
        async with engine.begin() as conn:
            change = await quarantine.lift(conn, incident_id)
            assert change is not None and change.changed
            raise AbandonedError
    assert await rows(engine, "SELECT state FROM incident_quarantine") == [("active",)]
    assert await rows(engine, "SELECT event_type FROM webhook_queue ORDER BY id") == [
        ("incident.declared",),
        ("incident.quarantined",),
    ]


# La diffusion.


@pytest.mark.integration
@pytest.mark.anyio
async def test_the_proxies_read_the_active_perimeters_under_a_version_that_spares_a_download(
    client: httpx.AsyncClient,
) -> None:
    empty = await client.get("/api/v1/quarantine")
    assert empty.status_code == 200
    assert empty.json() == {"version": version_of([]), "quarantines": []}
    assert empty.headers["etag"] == f'"{version_of([])}"'

    datasets = [
        {"namespace": "bigquery", "name": "ventes.suivi.commandes"},
        {"namespace": "bigquery", "name": "finance.ref.taux_change"},
    ]
    both = (
        await client.post(
            "/api/v1/incidents",
            json=declaration(lambda found: found["scope"].__setitem__("datasets", datasets)),
        )
    ).json()["id"]
    lifted_one, never = await declared(client, "rh.paie"), await declared(client, "rh.conges")
    first = await quarantined(client, both)
    await quarantined(client, lifted_one)
    await lifted(client, lifted_one)
    assert never

    found = await client.get("/api/v1/quarantine")
    assert found.status_code == 200
    body = found.json()
    assert body["quarantines"] == [
        {
            "incident_id": both,
            "mode": "warn",
            "activated_at": first["quarantine"]["activated_at"],
            "datasets": sorted(datasets, key=lambda item: (item["namespace"], item["name"])),
        }
    ]
    assert found.headers["etag"] == f'"{body["version"]}"'
    assert body["version"] != version_of([])

    unchanged = await client.get(
        "/api/v1/quarantine", headers={"if-none-match": found.headers["etag"]}
    )
    assert (unchanged.status_code, unchanged.content) == (304, b"")
    assert unchanged.headers["etag"] == found.headers["etag"]

    await lifted(client, both)
    changed = await client.get(
        "/api/v1/quarantine", headers={"if-none-match": found.headers["etag"]}
    )
    assert changed.status_code == 200
    assert changed.json() == {"version": version_of([]), "quarantines": []}


@pytest.mark.integration
@pytest.mark.anyio
async def test_a_hostile_if_none_match_is_neither_echoed_nor_an_error(
    client: httpx.AsyncClient,
) -> None:
    for header in (CANARY, '"' + CANARY + '"', "W/" + CANARY * 300):
        response = await client.get("/api/v1/quarantine", headers={"if-none-match": header})
        assert response.status_code == 200
        assert CANARY not in response.text
        assert CANARY not in str(response.headers)


@pytest.mark.integration
@pytest.mark.anyio
async def test_the_scope_of_a_quarantine_is_that_of_its_incident(
    client: httpx.AsyncClient,
) -> None:
    incident_id = await declared(client)
    await quarantined(client, incident_id)
    incident = (await client.get(f"/api/v1/incidents/{incident_id}")).json()
    (diffused,) = (await client.get("/api/v1/quarantine")).json()["quarantines"]
    assert diffused["datasets"] == incident["scope"]["datasets"] == DECLARATION["scope"]["datasets"]
