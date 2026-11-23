"""Les outils à décrire : lus dans les événements reçus, sans toucher au graphe."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from api.tests.events import facet, run_event
from api.tests.issuer import authentication, signed_in

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

BATCH = "/api/v1/lineage/batch"
UNDESCRIBED = "/api/v1/ingestion/undescribed-tools"


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(engine, authentication=authentication()))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://localhost", headers=signed_in()
    ) as client:
        yield client


def call(index: int, tool: str, *, server: str = "entrepot", marked: bool = True) -> dict[str, Any]:
    """L'appel d'un outil, marqué « non décrit » par son point de capture, ou non."""
    event = run_event(index)
    event["job"] = {"namespace": f"mcp://{server}", "name": tool}
    event["inputs"] = []
    fields: dict[str, Any] = {"server": server, "tool": tool}
    if marked:
        fields |= {"readResolution": "unresolved", "unresolvedReason": "undescribed_tool"}
    event["run"]["facets"] = {"answerlineage_toolCall": facet(**fields)}
    return event


async def listed(client: httpx.AsyncClient, **query: Any) -> list[dict[str, Any]]:
    response = await client.get(UNDESCRIBED, params=query)
    assert response.status_code == 200, response.text
    tools: list[dict[str, Any]] = response.json()["tools"]
    return tools


async def test_a_tool_called_without_rule_is_listed_with_its_emitter_and_its_calls(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    events = [
        call(0, "get_weather"),
        call(1, "get_weather"),
        call(2, "get_weather", server="meteo"),
        call(3, "get_time"),
        # Décrit, ou pas un appel d'outil : absent de la liste.
        call(4, "get_price", marked=False),
        {**call(5, "agent"), "run": {"runId": run_event(5)["run"]["runId"], "facets": {}}},
    ]
    response = await client.post(BATCH, json=events)
    assert response.json()["accepted"] == len(events)
    async with engine.begin() as conn:
        # get_time a été vu le plus récemment ; les autres, avant.
        await conn.execute(
            text(
                "UPDATE event_raw SET recorded_at = recorded_at - interval '1 hour'"
                " WHERE payload #>> '{job,name}' <> 'get_time'"
            )
        )
    tools = await listed(client)
    assert [(tool["emitter"], tool["tool"], tool["calls"]) for tool in tools] == [
        ("mcp://entrepot", "get_time", 1),
        ("mcp://entrepot", "get_weather", 2),
        ("mcp://meteo", "get_weather", 1),
    ]
    assert tools[0]["last_seen"] > tools[1]["last_seen"]
    assert [tool["tool"] for tool in await listed(client, limit=1)] == ["get_time"]


async def test_only_the_calls_received_in_the_period_count(
    client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    await client.post(BATCH, json=[call(0, "ancien"), call(1, "recent")])
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE event_raw SET recorded_at = now() - interval '10 days'"
                " WHERE payload #>> '{job,name}' = 'ancien'"
            )
        )
    assert [tool["tool"] for tool in await listed(client)] == ["recent"]
    assert [tool["tool"] for tool in await listed(client, days=11)] == ["recent", "ancien"]


async def test_a_name_is_listed_under_its_normalized_form(client: httpx.AsyncClient) -> None:
    decomposed = "re" + chr(0x301) + "sume" + chr(0x301)
    await client.post(BATCH, json=[call(0, decomposed)])
    assert [tool["tool"] for tool in await listed(client)] == ["résumé"]


async def test_no_tool_to_describe_is_an_empty_list(client: httpx.AsyncClient) -> None:
    assert await listed(client) == []


@pytest.mark.parametrize("query", [{"days": 0}, {"days": 32}, {"limit": 0}, {"limit": 1001}])
async def test_a_parameter_out_of_its_bounds_is_rejected_without_echo(
    client: httpx.AsyncClient, query: dict[str, int]
) -> None:
    response = await client.get(UNDESCRIBED, params=query)
    assert response.status_code == 422
    (name,) = query
    assert [error["path"] for error in response.json()["errors"]] == [f"/query/{name}"]
