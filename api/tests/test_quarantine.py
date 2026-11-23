"""Mettre un incident en quarantaine, la lever, et diffuser les périmètres aux proxys.

La quarantaine est un objet rattaché à l'incident : activée puis levée par deux
actions explicites et idempotentes, ses heures posées par la base, ses états et
ses transitions tenus par la base. Chaque activation et chaque levée s'annonce
par webhook, dans sa transaction. Les proxys lisent les périmètres en
quarantaine active sous une version qui leur évite de retélécharger ce qui n'a
pas changé. Une demande fautive est refusée sans rien enregistrer ni recopier.

Avec un secret, l'API signe et date chaque diffusion, ``200`` comme ``304`` ; sous
deux pendant une rotation ; sans secret, elle ne signe rien. Un secret invalide
l'arrête au démarrage, sans être cité.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from collections.abc import AsyncIterator, Callable, Sequence
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
from answerlineage_api.incidents.models import (
    MAX_DIFFUSED_DERIVED,
    ActiveQuarantines,
    validate_quarantine,
)
from answerlineage_api.incidents.quarantine import Diffusion, version_of
from answerlineage_api.incidents.signature import (
    ENV_PREVIOUS_SECRET,
    ENV_SECRET,
    MAX_SECRET_LENGTH,
    MIN_SECRET_LENGTH,
    QuarantineSigning,
    signing_from_settings,
)
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


def a_secret() -> str:
    return "partage-" + secrets.token_urlsafe(32)


# Le secret de la diffusion et son ancien, pendant une rotation, tirés pour la session.
SIGNING_SECRETS = (a_secret(), a_secret())


@pytest.fixture
async def signing_client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    signing = QuarantineSigning.create(*SIGNING_SECRETS)
    app = create_app(engine, authentication=authentication(), quarantine_signing=signing)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost", headers=signed_in()
    ) as client:
        yield client


def expected_signature(secret: str, timestamp: int, payload: bytes) -> str:
    """La signature que le format publié décrit, calculée ici sans la fonction de l'API."""
    message = b"answerlineage/quarantine/v1\x00" + str(timestamp).encode() + b"." + payload
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def signed_under(header: str, keys: Sequence[str], payload: bytes, since: float) -> bool:
    """Vrai quand l'en-tête porte une heure récente, puis une signature par secret, dans
    l'ordre des secrets."""
    found = re.fullmatch(r"t=([1-9][0-9]*)((?:,v1=[0-9a-f]{64})+)", header)
    assert found is not None, "en-tête illisible"
    timestamp = int(found.group(1))
    assert int(since) <= timestamp <= time.time()
    signatures = found.group(2).split(",v1=")[1:]
    return signatures == [expected_signature(key, timestamp, payload) for key in keys]


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


def test_the_secrets_of_the_diffusion_come_from_the_environment() -> None:
    current, previous = a_secret(), a_secret()
    assert signing_from_settings(None, {}) is None
    alone = signing_from_settings(None, {ENV_SECRET: current})
    assert alone is not None and alone.secrets == (current,)
    rotating = signing_from_settings(None, {ENV_SECRET: current, ENV_PREVIOUS_SECRET: previous})
    assert rotating is not None and rotating.secrets == (current, previous)
    given = QuarantineSigning.create(current)
    assert signing_from_settings(given, {ENV_SECRET: previous}) is given
    # Un secret ne s'affiche jamais, pas même dans une représentation.
    for shown in (repr(rotating), str(rotating)):
        assert current not in shown and previous not in shown


# Des réglages refusés, faits autour d'un canari qu'aucun message ne doit citer.
INVALID: dict[str, Callable[[str], dict[str, str]]] = {
    "court": lambda canary: {ENV_SECRET: canary},
    "un-de-moins": lambda canary: {ENV_SECRET: (canary * 2)[: MIN_SECRET_LENGTH - 1]},
    "trop-long": lambda canary: {ENV_SECRET: canary * (MAX_SECRET_LENGTH // len(canary) + 1)},
    "espaces": lambda canary: {ENV_SECRET: f"{canary} {canary} {canary}"},
    "hors-ascii": lambda canary: {ENV_SECRET: f"{canary}é{canary}{canary}"},
    "precedent-seul": lambda canary: {ENV_PREVIOUS_SECRET: canary * 2},
    "precedent-invalide": lambda canary: {ENV_SECRET: canary * 2, ENV_PREVIOUS_SECRET: canary},
}


@pytest.mark.parametrize("case", sorted(INVALID))
def test_an_invalid_secret_stops_the_api_at_startup_without_being_cited(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    canary = "canari" + secrets.token_hex(8)
    for variable, value in INVALID[case](canary).items():
        monkeypatch.setenv(variable, value)
    with pytest.raises(ValueError, match="ANSWERLINEAGE_QUARANTINE_") as refused:
        create_app(authentication=authentication())
    assert canary not in str(refused.value)


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
    # Sur l'incident, la quarantaine porte aussi sa zone : nulle avant le premier calcul.
    assert read["quarantine"] == listed["quarantine"] == {**quarantine_now, "zone": None}


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
    assert read["quarantine"] == {**lift["quarantine"], "zone": None}


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
    assert read["quarantine"] == {**second["quarantine"], "zone": None}


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
async def test_the_diffusion_is_signed_and_dated_200_and_304_alike_under_each_secret(
    signing_client: httpx.AsyncClient,
) -> None:
    incident_id = await declared(signing_client)
    await quarantined(signing_client, incident_id)

    since = time.time()
    found = await signing_client.get("/api/v1/quarantine")
    assert found.status_code == 200
    # La charge d'un 200 est son corps, tel qu'il part ; sous le secret, puis l'ancien.
    assert signed_under(
        found.headers["answerlineage-signature"], SIGNING_SECRETS, found.content, since
    )

    since = time.time()
    unchanged = await signing_client.get(
        "/api/v1/quarantine", headers={"if-none-match": found.headers["etag"]}
    )
    assert (unchanged.status_code, unchanged.content) == (304, b"")
    # Celle d'un 304, qui n'a pas de corps, est la version que le proxy tient.
    version = found.json()["version"].encode("ascii")
    assert signed_under(
        unchanged.headers["answerlineage-signature"], SIGNING_SECRETS, version, since
    )

    await lifted(signing_client, incident_id)
    changed = await signing_client.get(
        "/api/v1/quarantine", headers={"if-none-match": found.headers["etag"]}
    )
    assert changed.status_code == 200
    assert signed_under(
        changed.headers["answerlineage-signature"], SIGNING_SECRETS, changed.content, since
    )
    assert changed.json()["quarantines"] == []


@pytest.mark.integration
@pytest.mark.anyio
async def test_without_a_secret_the_diffusion_carries_no_signature(
    client: httpx.AsyncClient,
) -> None:
    found = await client.get("/api/v1/quarantine")
    unchanged = await client.get(
        "/api/v1/quarantine", headers={"if-none-match": found.headers["etag"]}
    )
    assert (found.status_code, unchanged.status_code) == (200, 304)
    assert "answerlineage-signature" not in found.headers
    assert "answerlineage-signature" not in unchanged.headers


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


# La zone : les jeux dérivés du périmètre, calculés par les workers, diffusés avec lui.

ZONE_TIME = "2026-11-16T09:00:00.125000Z"


async def zoned(
    engine: AsyncEngine,
    incident_id: str,
    derived: Sequence[tuple[str, str, int]],
    *,
    truncated: bool = False,
    computed_at: str = ZONE_TIME,
    checked_at: str | None = None,
) -> None:
    """La zone de la dernière quarantaine de l'incident, telle qu'un worker l'écrit."""
    async with engine.begin() as conn:
        quarantine_id = (
            await conn.execute(
                text(
                    "SELECT id FROM incident_quarantine WHERE incident_id = :id"
                    " ORDER BY id DESC LIMIT 1"
                ),
                {"id": UUID(incident_id)},
            )
        ).scalar_one()
        await conn.execute(
            text("DELETE FROM incident_quarantine_derived WHERE quarantine_id = :id"),
            {"id": quarantine_id},
        )
        await conn.execute(
            text(
                """
                INSERT INTO incident_quarantine_zone
                    (quarantine_id, computed_at, checked_at, truncated, derived)
                VALUES (:id, :computed_at, :checked_at, :truncated, :derived)
                ON CONFLICT (quarantine_id) DO UPDATE SET
                    computed_at = EXCLUDED.computed_at, checked_at = EXCLUDED.checked_at,
                    truncated = EXCLUDED.truncated, derived = EXCLUDED.derived
                """
            ),
            {
                "id": quarantine_id,
                "computed_at": instant(computed_at),
                "checked_at": instant(checked_at or computed_at),
                "truncated": truncated,
                "derived": len(derived),
            },
        )
        if derived:
            await conn.execute(
                text(
                    "INSERT INTO incident_quarantine_derived"
                    " (quarantine_id, namespace, name, depth)"
                    " VALUES (:id, :namespace, :name, :depth)"
                ),
                [
                    {"id": quarantine_id, "namespace": namespace, "name": name, "depth": depth}
                    for namespace, name, depth in derived
                ],
            )


def views(prefix: str, count: int = schema.MAX_ZONE_DERIVED) -> list[tuple[str, str, int]]:
    return [("bigquery", f"{prefix}.vue_{rank:02d}", 1 + rank % 3) for rank in range(count)]


@pytest.mark.integration
@pytest.mark.anyio
async def test_the_zone_of_a_quarantine_is_diffused_with_it_and_changes_its_version(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    incident_id = await declared(client)
    await quarantined(client, incident_id)
    before = await client.get("/api/v1/quarantine")
    (alone,) = before.json()["quarantines"]
    # Tant que les workers ne l'ont pas calculée, la zone est absente, pas vide.
    assert not {"derived", "derived_truncated", "zone_computed_at"} & set(alone)

    await zoned(
        engine,
        incident_id,
        [
            ("bigquery", "finance.marts.chiffre_affaires", 2),
            ("bigquery", "ventes.vues.commandes_du_jour", 1),
            ("bigquery", "direction.tableau.synthese", 3),
            ("bigquery", "compta.vues.factures", 1),
        ],
    )
    found = await client.get(
        "/api/v1/quarantine", headers={"if-none-match": before.headers["etag"]}
    )

    assert found.status_code == 200
    (diffused,) = found.json()["quarantines"]
    assert diffused == {
        **alone,
        "derived": [
            {"namespace": "bigquery", "name": "compta.vues.factures", "depth": 1},
            {"namespace": "bigquery", "name": "ventes.vues.commandes_du_jour", "depth": 1},
            {"namespace": "bigquery", "name": "finance.marts.chiffre_affaires", "depth": 2},
            {"namespace": "bigquery", "name": "direction.tableau.synthese", "depth": 3},
        ],
        "derived_truncated": False,
        "zone_computed_at": ZONE_TIME,
    }
    # La version est l'empreinte de la liste, zone comprise.
    assert found.json()["version"] == version_of(
        ActiveQuarantines.model_validate(found.json()).quarantines
    )
    assert found.headers["etag"] != before.headers["etag"]

    # Un calcul qui retrouve la même zone ne change que son dernier calcul : rien à retélécharger.
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE incident_quarantine_zone SET checked_at = now()"))
    unchanged = await client.get(
        "/api/v1/quarantine", headers={"if-none-match": found.headers["etag"]}
    )
    assert unchanged.status_code == 304

    # La borne d'une quarantaine a écarté des jeux : la diffusion le dit.
    await zoned(engine, incident_id, views("ventes"), truncated=True)
    (cut,) = (await client.get("/api/v1/quarantine")).json()["quarantines"]
    assert len(cut["derived"]) == schema.MAX_ZONE_DERIVED
    assert cut["derived_truncated"] is True


@pytest.mark.integration
@pytest.mark.anyio
async def test_the_incident_says_how_much_of_its_zone_the_proxies_receive(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    incident_id = await declared(client)
    await quarantined(client, incident_id)
    await zoned(
        engine,
        incident_id,
        [("bigquery", "ventes.vues.commandes_du_jour", 1)],
        checked_at="2026-11-16T09:05:00Z",
    )

    read = (await client.get(f"/api/v1/incidents/{incident_id}")).json()
    listed = (await client.get("/api/v1/incidents")).json()["incidents"][0]

    assert (
        read["quarantine"]["zone"]
        == listed["quarantine"]["zone"]
        == {
            "computed_at": ZONE_TIME,
            "checked_at": "2026-11-16T09:05:00Z",
            "derived": 1,
            "truncated": False,
        }
    )
    # Levée, la quarantaine garde sa dernière zone, qui n'est plus diffusée.
    await lifted(client, incident_id)
    after = (await client.get(f"/api/v1/incidents/{incident_id}")).json()
    assert after["quarantine"]["state"] == "lifted"
    assert after["quarantine"]["zone"] == read["quarantine"]["zone"]
    assert (await client.get("/api/v1/quarantine")).json()["quarantines"] == []


@pytest.mark.integration
@pytest.mark.anyio
async def test_the_diffusion_holds_its_bound_of_derived_datasets_the_oldest_quarantines_first(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    full = MAX_DIFFUSED_DERIVED // schema.MAX_ZONE_DERIVED
    incidents = [await declared(client, f"ventes.t{rank:02d}") for rank in range(full + 1)]
    for rank, incident_id in enumerate(incidents):
        await quarantined(client, incident_id)
        await zoned(engine, incident_id, views(f"zone{rank:02d}"))

    found = (await client.get("/api/v1/quarantine")).json()["quarantines"]

    assert [item["incident_id"] for item in found] == incidents
    assert sum(len(item["derived"]) for item in found) == MAX_DIFFUSED_DERIVED
    assert all(len(item["derived"]) == schema.MAX_ZONE_DERIVED for item in found[:full])
    assert not any(item["derived_truncated"] for item in found[:full])
    # La plus récente ne reçoit plus rien : sa zone se dit tronquée, ici comme sur l'incident.
    assert (found[-1]["derived"], found[-1]["derived_truncated"]) == ([], True)
    last = (await client.get(f"/api/v1/incidents/{incidents[-1]}")).json()["quarantine"]["zone"]
    assert (last["derived"], last["truncated"]) == (0, True)
    first = (await client.get(f"/api/v1/incidents/{incidents[0]}")).json()["quarantine"]["zone"]
    assert (first["derived"], first["truncated"]) == (schema.MAX_ZONE_DERIVED, False)

    # La plus ancienne levée, la place revient à la suivante.
    await lifted(client, incidents[0])
    after = (await client.get("/api/v1/quarantine")).json()["quarantines"]
    assert len(after) == full
    assert (len(after[-1]["derived"]), after[-1]["derived_truncated"]) == (
        schema.MAX_ZONE_DERIVED,
        False,
    )


@pytest.mark.integration
def test_the_base_holds_the_bounds_of_a_zone(database: URL) -> None:
    engine = create_engine(database)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO incident (id, state, severity, window_start, window_end)"
                    " VALUES (:id, 'qualified', 'high', now() - interval '1 hour', now())"
                ),
                {"id": UUID(UNKNOWN)},
            )
            quarantine_id = conn.execute(
                text(
                    "INSERT INTO incident_quarantine (incident_id, mode) VALUES (:id, 'warn')"
                    " RETURNING id"
                ),
                {"id": UUID(UNKNOWN)},
            ).scalar_one()
        zone = "INSERT INTO incident_quarantine_zone VALUES (:id, now(), now(), false, 1)"
        refused = {
            "ck_incident_quarantine_zone_derived": (
                "INSERT INTO incident_quarantine_zone VALUES (:id, now(), now(), false, 51)",
            ),
            "ck_incident_quarantine_zone_checked_after": (
                "INSERT INTO incident_quarantine_zone"
                " VALUES (:id, now(), now() - interval '1 second', false, 0)",
            ),
            "ck_incident_quarantine_derived_depth": (
                zone,
                "INSERT INTO incident_quarantine_derived VALUES (:id, 'bigquery', 'v', 4)",
            ),
            "fk_incident_quarantine_derived_zone": (
                "INSERT INTO incident_quarantine_derived VALUES (:id, 'bigquery', 'v', 1)",
            ),
        }
        for constraint, statements in refused.items():
            with engine.begin() as conn, pytest.raises(IntegrityError, match=constraint):
                for statement in statements:
                    conn.execute(text(statement), {"id": quarantine_id})
    finally:
        engine.dispose()
