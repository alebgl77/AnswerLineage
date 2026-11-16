"""Contrat entre composants : les relevés de couverture du proxy, l'API d'ingestion les accepte."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_api.ingest.coverage import validate_report
from answerlineage_proxy.app import McpProxy
from answerlineage_proxy.config import ProxySettings
from answerlineage_proxy.coverage import CoverageWindows, Report
from answerlineage_proxy.sink import MemorySink


def reports_of_a_proxy(started_at: datetime, instance: str | None = None) -> list[Report]:
    """Les relevés d'un proxy : fenêtres pleines, vides, et la dernière, close à l'arrêt."""
    windows = CoverageWindows(
        "mcp://entrepot", instance=instance, length=timedelta(minutes=1), started_at=started_at
    )
    for seconds, emitted in ((1, True), (2, False), (70, True), (200, True)):
        moment = started_at + timedelta(seconds=seconds)
        windows.begin(moment)
        windows.settle(moment, emitted=emitted)
    windows.uninspected(started_at + timedelta(seconds=3))
    return windows.close_all(started_at + timedelta(seconds=230))


@pytest.mark.parametrize("instance", [None, "3f9a0c1b7d2e4a65", "proxy-entrepot-0"])
def test_every_report_of_the_proxy_is_accepted_by_the_ingestion(instance: str | None) -> None:
    started_at = datetime.now(UTC) - timedelta(minutes=10, seconds=17.25)
    reports = reports_of_a_proxy(started_at, instance)
    assert len(reports) >= 4
    assert any(report["calls_seen"] == 0 for report in reports)
    for report in reports:
        accepted, errors = validate_report(report)
        assert errors == []
        assert accepted is not None
        assert accepted.emitter == "mcp://entrepot"
        assert accepted.instance == instance


@pytest.mark.anyio
async def test_the_last_window_of_a_stopped_proxy_is_accepted() -> None:
    sink = MemorySink()
    proxy = McpProxy(ProxySettings.create("http://127.0.0.1:1", "meteo"), sink)
    await proxy.aclose()
    assert sink.reports
    for report in sink.reports:
        assert validate_report(report)[1] == []
        assert report["emitter"] == "mcp://meteo"
        # Le proxy tire son instance au démarrage quand elle n'est pas réglée.
        assert report["instance"] == proxy.instance


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(engine))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield client


@pytest.mark.integration
@pytest.mark.anyio
async def test_the_reports_of_the_proxy_reach_the_coverage_unchanged(
    client: httpx.AsyncClient,
) -> None:
    started_at = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)
    reports = reports_of_a_proxy(started_at)
    for report in reports:
        response = await client.post("/api/v1/coverage", json=report)
        assert response.status_code == 201, response.text

    response = await client.get(
        "/api/v1/coverage",
        params={
            "from": reports[0]["window"]["from"],
            "to": reports[-1]["window"]["to"],
        },
    )
    [emitter] = response.json()["emitters"]
    coverage = emitter["coverage"]
    assert coverage["status"] == "measured"
    assert coverage["calls_seen"] == sum(report["calls_seen"] for report in reports) == 4
    assert coverage["events_lost"] == 1
    assert coverage["uninspected"] == 1
    assert coverage["instances"] == 1


@pytest.mark.integration
@pytest.mark.anyio
async def test_the_reports_of_two_instances_of_the_proxy_add_up(client: httpx.AsyncClient) -> None:
    """Deux proxys devant le même serveur : leurs relevés des mêmes fenêtres s'additionnent."""
    started_at = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)
    first, second = reports_of_a_proxy(started_at, "a"), reports_of_a_proxy(started_at, "b")
    for report in (*first, *second):
        response = await client.post("/api/v1/coverage", json=report)
        assert response.status_code == 201, response.text
        assert response.json()["instance"] == report["instance"]

    response = await client.get(
        "/api/v1/coverage",
        params={"from": first[0]["window"]["from"], "to": first[-1]["window"]["to"]},
    )
    [emitter] = response.json()["emitters"]
    coverage = emitter["coverage"]
    assert (coverage["status"], coverage["instances"]) == ("measured", 2)
    assert (coverage["calls_seen"], coverage["events_lost"], coverage["uninspected"]) == (8, 2, 2)
