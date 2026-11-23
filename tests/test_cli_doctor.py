"""``answerlineage doctor`` contre une vraie installation : l'API sur un vrai port, PostgreSQL.

Une installation saine d'abord, sans anomalie ; puis une anomalie à la fois,
provoquée pour de bon : chacune est nommée par son code, avec son remède.
"""

from __future__ import annotations

import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_api.store.engine import create_engine
from answerlineage_cli.doctor import ANOMALIES
from answerlineage_cli.exits import Exit
from answerlineage_workers import queue
from answerlineage_workers.destination import parse_destination
from answerlineage_workers.queue import process_batch
from answerlineage_workers.silence import check_silent_emitters
from answerlineage_workers.webhooks import Webhook, keep_in_service
from api.tests.events import run_event
from cli.tests.fakes import HTML, run
from proxy.tests.conftest import running

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

REPORT = {
    "emitter": "mcp://entrepot",
    "window": {"from": "2026-11-03T06:00:00Z", "to": "2026-11-03T06:01:00Z"},
    "calls_seen": 1,
    "events_emitted": 1,
    "events_lost": 0,
    "uninspected": 0,
}


@pytest.fixture
def explorer(tmp_path: Path) -> Path:
    """Un explorateur construit, réduit à sa page."""
    (tmp_path / "index.html").write_text(HTML, encoding="utf-8")
    return tmp_path


@asynccontextmanager
async def served(engine: AsyncEngine, explorer: Path | None) -> AsyncIterator[str]:
    async with running(create_app(engine, ui_directory=explorer)) as url:
        yield url


async def feed(url: str, *, process: AsyncEngine | None = None) -> None:
    """Un appel d'outil et le relevé de son émetteur, par l'API ; les workers passent ou non."""
    async with httpx.AsyncClient(base_url=url) as client:
        assert (await client.post("/api/v1/lineage", json=run_event())).status_code == 201
        assert (await client.post("/api/v1/coverage", json=REPORT)).status_code == 201
    if process is not None:
        while not (await process_batch(process)).empty:
            pass


async def doctor(monkeypatch: pytest.MonkeyPatch, url: str) -> tuple[int, dict[str, Any]]:
    result = await anyio.to_thread.run_sync(
        lambda: run(["doctor", "--format", "json", "--url", url], None, monkeypatch)
    )
    report: dict[str, Any] = json.loads(result.out)
    return result.code, report


def only(report: dict[str, Any], code: str) -> None:
    """L'anomalie est la seule, nommée par son code, avec son remède écrit d'avance."""
    assert report["anomalies"] == [code]
    (found,) = [check for check in report["checks"] if check["code"] == code]
    assert found["status"] == "anomaly"
    assert found["check"] == ANOMALIES[code].check
    assert found["remedy"] == ANOMALIES[code].remedy


async def test_a_sound_installation_has_no_anomaly(
    monkeypatch: pytest.MonkeyPatch, engine: AsyncEngine, explorer: Path
) -> None:
    async with served(engine, explorer) as url:
        await feed(url, process=engine)
        code, report = await doctor(monkeypatch, url)
        readable = await anyio.to_thread.run_sync(
            lambda: run(["doctor", "--url", url], None, monkeypatch)
        )

    assert code == Exit.OK
    assert report["anomalies"] == []
    assert {check["check"]: check["status"] for check in report["checks"]} == {
        "api": "ok",
        "address": "ok",
        "database": "ok",
        "migrations": "ok",
        "queue": "ok",
        "dead_letters": "ok",
        "emitters": "ok",
        "partitions": "ok",
        # Sans destination réglée aux workers, rien n'est réémis : rien à juger.
        "reemission": "not_checked",
        # Sans webhook réglé aux workers, rien n'est annoncé : rien à juger.
        "webhooks": "not_checked",
        "explorer": "ok",
        "workers": "not_checked",
    }
    assert readable.code == Exit.OK
    assert "Aucune anomalie." in readable.out
    assert f"L'explorateur est servi : {url}/" in readable.out


async def test_an_unreachable_api_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = f"http://127.0.0.1:{probe.getsockname()[1]}"
    code, report = await doctor(monkeypatch, closed)
    assert code == Exit.ANOMALY
    only(report, "api_unreachable")


async def test_a_database_without_schema_is_not_migrated(
    monkeypatch: pytest.MonkeyPatch, empty_database: URL, explorer: Path
) -> None:
    bare = create_engine(empty_database)
    try:
        async with served(bare, explorer) as url:
            code, report = await doctor(monkeypatch, url)
    finally:
        await bare.dispose()
    assert code == Exit.ANOMALY
    only(report, "schema_not_migrated")


async def test_an_unreachable_database_is_named(
    monkeypatch: pytest.MonkeyPatch, explorer: Path
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    lost = create_engine(f"postgresql://al:al@127.0.0.1:{port}/al")
    try:
        async with served(lost, explorer) as url:
            code, report = await doctor(monkeypatch, url)
    finally:
        await lost.dispose()
    assert code == Exit.ANOMALY
    only(report, "database_unavailable")


async def test_a_queue_left_behind_is_named(
    monkeypatch: pytest.MonkeyPatch, engine: AsyncEngine, explorer: Path
) -> None:
    async with served(engine, explorer) as url:
        await feed(url)
        # Aucun worker ne passe : l'événement attend depuis une heure.
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE ingest_queue SET enqueued_at = now() - interval '1 hour'")
            )
        code, report = await doctor(monkeypatch, url)
    assert code == Exit.ANOMALY
    only(report, "queue_stalled")
    (queue,) = [check for check in report["checks"] if check["check"] == "queue"]
    assert queue["message"].startswith("La file d'ingestion n'avance pas : 1 événement en attente")


async def test_an_event_the_projection_refuses_is_named_from_the_dead_letters(
    monkeypatch: pytest.MonkeyPatch, engine: AsyncEngine, explorer: Path
) -> None:
    def refused(_: Any) -> Any:
        raise ValueError("projection en défaut")

    async with served(engine, explorer) as url:
        with monkeypatch.context() as defect:
            # Une projection en défaut met l'événement au rebut dès le premier échec.
            defect.setattr(queue, "project", refused)
            await feed(url, process=engine)
        code, report = await doctor(monkeypatch, url)
    assert code == Exit.ANOMALY
    only(report, "dead_letters_waiting")
    (buried,) = [check for check in report["checks"] if check["check"] == "dead_letters"]
    assert buried["message"].startswith("1 événement au rebut")


async def test_a_muted_emitter_is_named(
    monkeypatch: pytest.MonkeyPatch, engine: AsyncEngine, explorer: Path
) -> None:
    async with served(engine, explorer) as url:
        await feed(url, process=engine)
        later = datetime.now(UTC) + timedelta(minutes=11)
        assert len(await check_silent_emitters(engine, now=later)) == 1
        code, report = await doctor(monkeypatch, url)
    assert code == Exit.ANOMALY
    only(report, "emitters_muted")


async def test_an_absent_explorer_is_named(
    monkeypatch: pytest.MonkeyPatch, engine: AsyncEngine
) -> None:
    async with served(engine, None) as url:
        await feed(url, process=engine)
        code, report = await doctor(monkeypatch, url)
    assert code == Exit.ANOMALY
    only(report, "explorer_missing")


async def test_a_missing_partition_is_named(
    monkeypatch: pytest.MonkeyPatch, engine: AsyncEngine, explorer: Path
) -> None:
    async with served(engine, explorer) as url:
        await feed(url, process=engine)
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO partition_alert VALUES ('event_raw', '2030-01-01', now())")
            )
        code, report = await doctor(monkeypatch, url)
    assert code == Exit.ANOMALY
    only(report, "partitions_missing")


async def test_a_reemission_left_behind_is_named(
    monkeypatch: pytest.MonkeyPatch, engine: AsyncEngine, explorer: Path
) -> None:
    async with served(engine, explorer) as url:
        await feed(url)
        # Un worker qui a une destination projette ; la destination ne prend rien depuis une heure.
        while not (await process_batch(engine, reemit=True)).empty:
            pass
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE reemission_queue SET enqueued_at = now() - interval '1 hour'")
            )
        code, report = await doctor(monkeypatch, url)
    assert code == Exit.ANOMALY
    only(report, "reemission_lagging")
    (late,) = [check for check in report["checks"] if check["check"] == "reemission"]
    assert late["message"].startswith(
        "La réémission vers le consommateur OpenLineage externe est en retard : 1 événement à"
        " réémettre, le plus ancien depuis 1 h 00 min"
    )


async def test_webhooks_left_behind_are_named(
    monkeypatch: pytest.MonkeyPatch, engine: AsyncEngine, explorer: Path
) -> None:
    # Un worker qui a un webhook est passé ; le récepteur ne prend rien depuis dix minutes.
    webhook = Webhook(parse_destination("https://recepteur.interne/hooks"), "x" * 32)
    await keep_in_service(engine, webhook)
    async with served(engine, explorer) as url:
        async with httpx.AsyncClient(base_url=url) as client:
            declared = await client.post(
                "/api/v1/incidents",
                json={
                    "scope": {"datasets": [{"namespace": "bigquery", "name": "finance.taux"}]},
                    "window": {"from": "2026-11-03T06:00:00Z", "to": "2026-11-03T07:00:00Z"},
                    "severity": "medium",
                },
            )
            assert declared.status_code == 201
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE webhook_queue SET enqueued_at = now() - interval '10 minutes'")
            )
        code, report = await doctor(monkeypatch, url)
    assert code == Exit.ANOMALY
    only(report, "webhooks_lagging")
    (late,) = [check for check in report["checks"] if check["check"] == "webhooks"]
    assert late["message"].startswith(
        "Les webhooks des incidents sont en retard : 1 message à envoyer, le plus ancien depuis"
        " 10 min"
    )
