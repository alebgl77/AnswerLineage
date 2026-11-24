"""Confirmer ou écarter un incident proposé : seul un humain fait partir quelque chose.

Confirmer déclare l'incident sur la table de la proposition, avec la gravité
qu'il donne et la fenêtre qu'il donne ou la fenêtre déduite, demande son analyse
et annonce sa déclaration, dans une seule transaction. Écarter n'annonce rien.
Les deux sont idempotents ; une proposition décidée ne l'est pas deux fois
autrement, et une demande fautive est refusée sans rien enregistrer ni recopier.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_api.incidents.models import validate_confirmation
from api.tests.issuer import authentication, signed_in
from api.tests.test_proposals import CUSTOMERS, UNKNOWN, WAREHOUSE, hours, iso, propose, tracked

CANARY = "canari-8e41"
HIGH = {"severity": "high"}


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


async def in_service(engine: AsyncEngine) -> None:
    """Un worker à webhook est passé : les changements d'état sont mis en file."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO webhook_stats (id, in_service_until)"
                " VALUES (true, now() + interval '1 hour')"
            )
        )


async def decided(
    client: httpx.AsyncClient, proposal: UUID, action: str, body: Any = None
) -> dict[str, Any]:
    response = await client.post(f"/api/v1/incident-proposals/{proposal}/{action}", json=body)
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()
    return found


async def expire(engine: AsyncEngine, proposal: UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE incident_proposal SET state = 'expired',"
                " decided_at = GREATEST(now(), proposed_at) WHERE id = :id"
            ),
            {"id": proposal},
        )


# Validation, sans base.


@pytest.mark.parametrize(
    ("raw", "path", "code"),
    [
        ({"severity": CANARY}, "/severity", "literal_error"),
        ({}, "/severity", "missing"),
        ({"severity": "high", "motif": CANARY}, "", "unknown_field"),
        (
            {"severity": "high", "window": {"from": CANARY, "to": CANARY}},
            "/window/from",
            "value_error",
        ),
        (
            {"severity": "high", "window": {"from": "2026-11-02T00:00:00Z"}},
            "/window/to",
            "missing",
        ),
        (
            {
                "severity": "high",
                "window": {"from": "2026-11-03T00:00:00Z", "to": "2026-11-02T00:00:00Z"},
            },
            "/window",
            "window_inverted",
        ),
        (
            {
                "severity": "high",
                "window": {"from": "2026-10-01T00:00:00Z", "to": "2026-11-02T00:00:00Z"},
            },
            "/window",
            "window_too_long",
        ),
        (
            {
                "severity": CANARY,
                "window": {"from": "2026-11-03T00:00:00Z", "to": "2026-11-02T00:00:00Z"},
            },
            "/window",
            "window_inverted",
        ),
        ([CANARY], "", "not_an_object"),
    ],
)
def test_a_faulty_confirmation_is_refused_with_its_reason_and_without_its_value(
    raw: object, path: str, code: str
) -> None:
    asked, errors = validate_confirmation(raw)
    assert asked is None
    assert (path, code) in {(error.path, error.type) for error in errors}
    assert CANARY not in json.dumps([error.model_dump() for error in errors])


def test_a_confirmation_gives_a_severity_and_maybe_a_window() -> None:
    asked, errors = validate_confirmation(HIGH)
    assert errors == [] and asked is not None and asked.window is None
    asked, errors = validate_confirmation(
        {
            "severity": "low",
            "window": {"from": "2026-11-02T00:00:00Z", "to": "2026-11-03T00:00:00Z"},
        }
    )
    assert errors == [] and asked is not None and asked.window is not None


# Confirmer.


@pytest.mark.integration
@pytest.mark.anyio
async def test_the_confirmation_declares_the_incident_on_its_table_and_announces_it(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    await in_service(engine)
    proposal = await propose(engine, "email")
    before = datetime.now().astimezone()

    decision = await decided(client, proposal, "confirm", HIGH)

    assert decision["changed"] is True
    found = decision["proposal"]
    assert (found["state"], found["id"]) == ("confirmed", str(proposal))
    assert datetime.fromisoformat(found["decided_at"]) >= before - timedelta(seconds=5)
    incident = (await client.get(f"/api/v1/incidents/{found['incident_id']}")).json()
    assert incident["state"] == "qualified"
    assert incident["severity"] == "high"
    assert incident["scope"] == {"datasets": [{"namespace": WAREHOUSE, "name": CUSTOMERS}]}
    assert incident["proposal_id"] == str(proposal)
    assert incident["analysis"]["state"] == "pending"
    # La fenêtre déduite : de son début au jour de la confirmation, l'échec n'étant pas fini.
    assert incident["window"]["from"] == iso(hours(1))
    assert datetime.fromisoformat(incident["window"]["to"]) >= before - timedelta(seconds=5)
    # La déclaration est annoncée, à son heure, qui est celle de la confirmation.
    assert await rows(
        engine,
        "SELECT w.event_type, w.occurred_at = i.declared_at FROM webhook_queue w"
        " JOIN incident i ON i.id = w.incident_id",
    ) == [("incident.declared", True)]
    listed = (await client.get("/api/v1/incidents")).json()["incidents"]
    assert [item["proposal_id"] for item in listed] == [str(proposal)]


@pytest.mark.integration
@pytest.mark.anyio
async def test_the_derived_window_ends_where_the_failure_ended(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    proposal = await propose(engine, "email")
    await tracked(engine, "email", 9, 5)
    found = (await decided(client, proposal, "confirm", HIGH))["proposal"]
    incident = (await client.get(f"/api/v1/incidents/{found['incident_id']}")).json()
    assert incident["window"] == {"from": iso(hours(1)), "to": iso(hours(9))}


@pytest.mark.integration
@pytest.mark.anyio
async def test_the_confirmer_may_give_the_window(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    proposal = await propose(engine, "email")
    window = {"from": iso(hours(-5)), "to": iso(hours(20))}
    found = (await decided(client, proposal, "confirm", {"severity": "low", "window": window}))[
        "proposal"
    ]
    incident = (await client.get(f"/api/v1/incidents/{found['incident_id']}")).json()
    assert (incident["window"], incident["severity"]) == (window, "low")


@pytest.mark.integration
@pytest.mark.anyio
async def test_a_derived_window_longer_than_an_incident_must_be_given(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    proposal = await propose(engine, "email")
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE incident_proposal DISABLE TRIGGER USER"))
        await conn.execute(
            text("UPDATE incident_proposal SET window_start = now() - interval '40 days'")
        )
        await conn.execute(text("ALTER TABLE incident_proposal ENABLE TRIGGER USER"))
    response = await client.post(f"/api/v1/incident-proposals/{proposal}/confirm", json=HIGH)
    assert response.status_code == 422
    assert [(error["path"], error["type"]) for error in response.json()["errors"]] == [
        ("/window", "window_too_long")
    ]
    assert await rows(engine, "SELECT state FROM incident_proposal") == [("signaled",)]
    assert await rows(engine, "SELECT count(*) FROM incident") == [(0,)]


@pytest.mark.integration
@pytest.mark.anyio
async def test_confirming_twice_declares_and_announces_once(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    await in_service(engine)
    proposal = await propose(engine, "email")
    first = await decided(client, proposal, "confirm", HIGH)
    again = await decided(client, proposal, "confirm", {"severity": "low"})

    assert again["changed"] is False
    assert again["proposal"] == first["proposal"]
    assert await rows(engine, "SELECT severity FROM incident") == [("high",)]
    assert await rows(engine, "SELECT count(*) FROM webhook_queue") == [(1,)]


# Écarter.


@pytest.mark.integration
@pytest.mark.anyio
async def test_a_dismissal_announces_nothing_and_is_idempotent(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    await in_service(engine)
    proposal = await propose(engine, "email")
    first = await decided(client, proposal, "dismiss")
    again = await decided(client, proposal, "dismiss")

    assert (first["changed"], again["changed"]) == (True, False)
    assert first["proposal"]["state"] == "dismissed"
    assert first["proposal"]["incident_id"] is None
    assert again["proposal"] == first["proposal"]
    for table in ("incident", "incident_analysis", "webhook_queue"):
        assert await rows(engine, f"SELECT count(*) FROM {table}") == [(0,)], table  # noqa: S608


# Refus.


@pytest.mark.integration
@pytest.mark.anyio
@pytest.mark.parametrize(
    ("first", "then", "code"),
    [
        ("dismiss", "confirm", "proposal_dismissed"),
        ("confirm", "dismiss", "proposal_confirmed"),
        ("expire", "confirm", "proposal_expired"),
        ("expire", "dismiss", "proposal_expired"),
    ],
)
async def test_a_decided_proposal_is_not_decided_otherwise(
    client: httpx.AsyncClient, engine: AsyncEngine, first: str, then: str, code: str
) -> None:
    proposal = await propose(engine, "email")
    if first == "expire":
        await expire(engine, proposal)
    else:
        await decided(client, proposal, first, HIGH if first == "confirm" else None)
    before = await rows(engine, "SELECT state, decided_at, incident_id FROM incident_proposal")

    response = await client.post(
        f"/api/v1/incident-proposals/{proposal}/{then}", json=HIGH if then == "confirm" else None
    )
    assert (response.status_code, response.json()) == (409, {"error": code})
    assert await rows(engine, "SELECT state, decided_at, incident_id FROM incident_proposal") == (
        before
    )


@pytest.mark.integration
@pytest.mark.anyio
@pytest.mark.parametrize(
    ("action", "proposal", "body", "status", "code"),
    [
        ("confirm", UNKNOWN, HIGH, 404, "unknown_proposal"),
        ("dismiss", UNKNOWN, None, 404, "unknown_proposal"),
        ("confirm", f"{CANARY}-pas-un-uuid", HIGH, 422, "string_pattern_mismatch"),
        ("dismiss", f"{CANARY}-pas-un-uuid", None, 422, "string_pattern_mismatch"),
        ("confirm", None, {"severity": "high", "motif": CANARY}, 422, "unknown_field"),
        ("confirm", None, {"severity": CANARY}, 422, "literal_error"),
        ("confirm", None, [CANARY], 422, "not_an_object"),
    ],
)
async def test_a_faulty_decision_is_refused_without_its_value_and_records_nothing(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    action: str,
    proposal: str | None,
    body: Any,
    status: int,
    code: str,
) -> None:
    existing = await propose(engine, "email")
    target = str(existing) if proposal is None else proposal
    response = await client.post(f"/api/v1/incident-proposals/{target}/{action}", json=body)
    assert response.status_code == status
    found = response.json()
    codes = [found["error"]] if "error" in found else [error["type"] for error in found["errors"]]
    assert code in codes
    assert CANARY not in response.text
    assert await rows(engine, "SELECT state FROM incident_proposal") == [("signaled",)]
    assert await rows(engine, "SELECT count(*) FROM incident") == [(0,)]


@pytest.mark.integration
@pytest.mark.anyio
async def test_a_confirmation_body_is_read_bounded(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    proposal = await propose(engine, "email")
    response = await client.post(
        f"/api/v1/incident-proposals/{proposal}/confirm",
        content=json.dumps({"severity": "high", "padding": "x" * 8192}),
        headers={"content-type": "application/json"},
    )
    assert (response.status_code, response.json()) == (413, {"error": "body_too_large"})
