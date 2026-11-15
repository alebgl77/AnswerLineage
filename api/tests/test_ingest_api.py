"""US-0301 : l'API d'ingestion valide et enregistre des RunEvents OpenLineage par lots."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import API_VERSION, create_app
from api.tests.events import minimal_event, mutated, run_event

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

BATCH = "/api/v1/lineage/batch"
SINGLE = "/api/v1/lineage"


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(engine))
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
        yield client


async def stored(engine: AsyncEngine) -> list[tuple[Any, ...]]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT event_id, run_id::text, event_type, producer, payload "
                "FROM event_raw ORDER BY event_time"
            )
        )
        return [tuple(row) for row in result]


async def test_batch_of_500_valid_events_is_accepted_and_recorded(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    events = [run_event(index) for index in range(500)]
    response = await client.post(BATCH, json=events)

    assert response.status_code == 202
    assert response.json() == {"accepted": 500, "duplicates": 0, "rejected": []}
    rows = await stored(engine)
    assert len(rows) == 500
    event_id, run_id, event_type, producer, payload = rows[0]
    assert len(event_id) == 64
    assert run_id == events[0]["run"]["runId"]
    assert (event_type, producer) == ("COMPLETE", events[0]["producer"])
    # L'événement est conservé tel quel, rejouable.
    assert payload == events[0]


async def test_invalid_event_is_rejected_with_its_faulty_path_and_the_rest_is_accepted(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    events = [run_event(0), mutated(["run", "runId"], "pas-un-uuid"), run_event(2), "pas un objet"]
    response = await client.post(BATCH, json=events)

    assert response.status_code == 202
    result = response.json()
    assert (result["accepted"], result["duplicates"]) == (2, 0)
    assert [rejection["index"] for rejection in result["rejected"]] == [1, 3]
    (error,) = result["rejected"][0]["errors"]
    assert error["path"] == "/run/runId"
    assert error["type"] and error["message"]
    assert result["rejected"][1]["errors"][0]["type"] == "not_an_object"
    assert [row[1] for row in await stored(engine)] == [
        events[0]["run"]["runId"],
        events[2]["run"]["runId"],
    ]


async def test_batch_with_no_valid_event_answers_422(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    response = await client.post(BATCH, json=[mutated(["job"]), mutated(["eventTime"], 12)])
    assert response.status_code == 422
    result = response.json()
    assert result["accepted"] == 0
    assert [r["errors"][0]["path"] for r in result["rejected"]] == ["/job", "/eventTime"]
    assert await stored(engine) == []


async def test_empty_batch_is_accepted(client: httpx.AsyncClient) -> None:
    response = await client.post(BATCH, json=[])
    assert response.status_code == 202
    assert response.json() == {"accepted": 0, "duplicates": 0, "rejected": []}


async def test_replayed_events_are_recorded_once(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    events = [run_event(0), run_event(1), run_event(0)]
    first = await client.post(BATCH, json=events)
    assert first.json() == {"accepted": 3, "duplicates": 1, "rejected": []}
    second = await client.post(BATCH, json=[*events, run_event(2)])
    assert second.status_code == 202
    assert second.json() == {"accepted": 4, "duplicates": 3, "rejected": []}
    assert len(await stored(engine)) == 3


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"{", "invalid_json"),
        (b"", "invalid_json"),
        (b'[{"x": NaN}]', "invalid_json"),
        (b'{"events": []}', "batch_must_be_an_array"),
        (b'"texte"', "batch_must_be_an_array"),
        (b"[" * 100_000, "invalid_json"),
    ],
)
async def test_body_that_is_not_a_json_array_is_refused(
    client: httpx.AsyncClient, content: bytes, code: str
) -> None:
    response = await client.post(BATCH, content=content)
    assert response.status_code == 400
    assert response.json() == {"error": code}


async def test_batch_larger_than_the_limit_is_refused(engine: AsyncEngine) -> None:
    transport = httpx.ASGITransport(app=create_app(engine, max_batch_events=3))
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
        allowed = await client.post(BATCH, json=[run_event(i) for i in range(3)])
        refused = await client.post(BATCH, json=[run_event(i) for i in range(10, 14)])
    assert allowed.status_code == 202
    assert (refused.status_code, refused.json()) == (413, {"error": "batch_too_large"})
    assert len(await stored(engine)) == 3


async def test_text_that_the_database_cannot_store_is_rejected_not_crashed(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    nul = json.dumps([run_event(0), mutated(["job", "name"], "nul\x00caché")]).encode()
    surrogate = json.dumps([mutated(["job", "name"], "PLACEHOLDER")]).replace(
        "PLACEHOLDER", "\\ud800"
    )
    response = await client.post(BATCH, content=nul)
    assert response.status_code == 202
    (rejection,) = response.json()["rejected"]
    assert (rejection["index"], rejection["errors"][0]["type"]) == (1, "unstorable_text")
    lone = await client.post(BATCH, content=surrogate.encode())
    assert lone.status_code == 422
    assert lone.json()["rejected"][0]["errors"][0]["type"] == "unstorable_text"
    assert len(await stored(engine)) == 1


async def test_standard_openlineage_endpoint_accepts_one_event(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Un émetteur OpenLineage conforme poste un événement sur /api/v1/lineage."""
    event = minimal_event()
    created = await client.post(SINGLE, json=event)
    assert created.status_code == 201
    assert created.json()["duplicate"] is False
    replayed = await client.post(SINGLE, json=event)
    assert replayed.status_code == 201
    assert replayed.json() == {"event_id": created.json()["event_id"], "duplicate": True}
    ((_, _, event_type, _, payload),) = await stored(engine)
    assert event_type == "OTHER"
    assert payload == event


async def test_standard_endpoint_rejects_an_invalid_event_with_its_faulty_path(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    response = await client.post(SINGLE, json=mutated(["inputs", 0, "name"]))
    assert response.status_code == 422
    assert [error["path"] for error in response.json()["errors"]] == ["/inputs/0/name"]
    assert (await client.post(SINGLE, content=b"pas du json")).json() == {"error": "invalid_json"}
    assert (await client.post(SINGLE, json=[run_event()])).status_code == 422
    assert await stored(engine) == []


async def test_api_is_versioned_and_described_by_an_openapi_document(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/v1/openapi.json")
    assert response.status_code == 200
    document = response.json()
    assert document["openapi"].startswith("3.")
    assert document["info"]["version"] == API_VERSION
    assert set(document["paths"]) == {BATCH, SINGLE}

    batch = document["paths"][BATCH]["post"]
    assert set(batch["responses"]) == {"202", "400", "413", "422"}
    assert batch["requestBody"]["content"]["application/json"]["schema"] == {
        "type": "array",
        "items": {"$ref": "#/components/schemas/RunEvent"},
    }
    single = document["paths"][SINGLE]["post"]
    assert set(single["responses"]) == {"201", "400", "422"}

    schemas = document["components"]["schemas"]
    run_event_schema = schemas["RunEvent"]
    assert {"eventTime", "producer", "schemaURL", "run", "job"} <= set(run_event_schema["required"])
    assert {"Run", "Job", "Facet", "InputDataset", "OutputDataset", "BatchResult"} <= set(schemas)
    assert "$defs" not in json.dumps(document)
    # Aucune page de documentation servie par l'API : elle chargerait des scripts tiers.
    assert (await client.get("/docs")).status_code == 404


async def test_application_opens_and_closes_its_own_database_connection(
    database: URL, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSWERLINEAGE_DATABASE_URL", database.render_as_string(hide_password=False))
    app = create_app()
    assert app.state.engine is None
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
            response = await client.post(SINGLE, json=minimal_event())
        assert response.status_code == 201
    assert app.state.engine is None


async def test_provided_engine_is_left_open(engine: AsyncEngine) -> None:
    app = create_app(engine)
    async with app.router.lifespan_context(app):
        pass
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT 1"))).scalar_one() == 1
