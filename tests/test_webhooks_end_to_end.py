"""Un webhook réglé : chaque changement d'état d'un incident part signé en moins d'une minute.

L'incident est déclaré par l'API ; la boucle d'un worker, réglée comme en
production pour ses webhooks, en fait l'analyse et annonce les deux changements
d'état à un récepteur servi sur un vrai port. Le récepteur vérifie chaque
signature avec la fonction de référence, et refuse ensuite une signature
rejouée hors de la fenêtre, une signature fausse et un corps altéré.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections.abc import AsyncIterator, MutableMapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_workers.destination import parse_destination
from answerlineage_workers.queue import run_worker
from answerlineage_workers.webhook_signature import (
    DEFAULT_TOLERANCE,
    HEADER,
    OUTSIDE_TOLERANCE,
    SIGNATURE_MISMATCH,
    SignatureError,
    verify,
)
from answerlineage_workers.webhooks import Webhook
from api.tests.issuer import authentication, signed_in
from workers.tests.catalog import serving
from workers.tests.test_incidents import RATES, answering, hours, reading, record

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

# Le critère : un appel signé part en moins d'une minute après le changement d'état.
DEADLINE_SECONDS = 60.0


@dataclass
class Receiver:
    """Un récepteur de webhooks : il vérifie chaque signature avant de lire le message."""

    secret: str
    # Décalage de son horloge, en secondes : un message reçu plus tard qu'il n'a été émis.
    late_by: float = 0.0
    accepted: list[tuple[float, dict[str, Any]]] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    requests: list[tuple[str, bytes]] = field(default_factory=list)

    async def __call__(self, scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            while (message := await receive())["type"] != "lifespan.shutdown":
                await send({"type": "lifespan.startup.complete"})
            await send({"type": "lifespan.shutdown.complete"})
            return
        chunks = []
        while True:
            message = await receive()
            chunks.append(message.get("body", b""))
            if not message.get("more_body"):
                break
        body = b"".join(chunks)
        headers = {key.decode().lower(): value.decode() for key, value in scope["headers"]}
        signature = headers.get(HEADER.lower(), "")
        self.requests.append((signature, body))
        try:
            verify(signature, body, [self.secret], now=time.time() + self.late_by)
        except SignatureError as refusal:
            self.refused.append(refusal.code)
            status = 401
        else:
            self.accepted.append((time.monotonic(), json.loads(body)))
            status = 204
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})


@pytest.fixture
async def api(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(engine, authentication=authentication()))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1", headers=signed_in()
    ) as client:
        yield client


async def until(condition: Any, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not await condition():
        assert time.monotonic() < deadline
        await asyncio.sleep(0.01)


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value)


async def in_service(engine: AsyncEngine) -> bool:
    async with engine.connect() as conn:
        found = await conn.execute(text("SELECT in_service_until > now() FROM webhook_stats"))
        return bool(found.scalar_one_or_none())


async def test_a_change_of_state_leaves_signed_in_less_than_a_minute_and_a_replay_is_refused(
    engine: AsyncEngine, api: httpx.AsyncClient
) -> None:
    agent = uuid4()
    await record(engine, answering(agent, 1, 3.5, ("p-1", "p-2")), reading(agent, 3, RATES))
    receiver = Receiver(secrets.token_urlsafe(32))
    async with serving(receiver) as base:
        webhook = Webhook(parse_destination(base + "/webhooks"), receiver.secret)
        stop = asyncio.Event()
        # Les boucles des webhooks gardent leurs réglages de production.
        worker = asyncio.create_task(run_worker(engine, stop, idle_delay=0.01, webhook=webhook))
        try:
            await until(lambda: in_service(engine), timeout=10)

            declared_at = time.monotonic()
            response = await api.post(
                "/api/v1/incidents",
                json={
                    "scope": {"datasets": [{"namespace": RATES.namespace, "name": RATES.name}]},
                    "window": {"from": hours(0).isoformat(), "to": hours(10).isoformat()},
                    "severity": "high",
                },
            )
            assert response.status_code == 201, response.text
            incident_id = response.json()["id"]

            async def both_announced() -> bool:
                return len(receiver.accepted) >= 2

            await until(both_announced, timeout=DEADLINE_SECONDS)
        finally:
            stop.set()
            await asyncio.wait_for(worker, timeout=30)

        detail = (await api.get(f"/api/v1/incidents/{incident_id}")).json()
        (declared_received, declared), (analysed_received, analysed) = receiver.accepted
        assert receiver.refused == []
        assert declared_received - declared_at < DEADLINE_SECONDS
        assert analysed_received - declared_at < DEADLINE_SECONDS
        assert (declared["type"], analysed["type"]) == ("incident.declared", "incident.analysed")
        assert declared["incident"]["id"] == analysed["incident"]["id"] == incident_id
        # Chaque message dit l'heure du changement d'état, celle que l'API donne.
        assert instant(declared["occurred_at"]) == instant(detail["declared_at"])
        assert instant(analysed["occurred_at"]) == instant(detail["analysis"]["finished_at"])
        assert (
            analysed["incident"]["analysis"]["counts"]
            == detail["analysis"]["counts"]
            == {
                "answers": 1,
                "recipients": 2,
                "lower_bound": False,
            }
        )

        # Le même appel, rejoué au récepteur six minutes plus tard : hors de la fenêtre.
        signature, body = receiver.requests[0]
        receiver.late_by = DEFAULT_TOLERANCE + 60
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=receiver), base_url=base
        ) as replaying:
            replayed = await replaying.post("/webhooks", content=body, headers={HEADER: signature})
            receiver.late_by = 0.0
            forged = await replaying.post(
                "/webhooks",
                content=body,
                headers={HEADER: signature.rsplit("=", 1)[0] + "=" + "0" * 64},
            )
            altered = await replaying.post(
                "/webhooks",
                content=body.replace(b'"severity":"high"', b'"severity":"low"'),
                headers={HEADER: signature},
            )
    assert (replayed.status_code, forged.status_code, altered.status_code) == (401, 401, 401)
    assert receiver.refused == [OUTSIDE_TOLERANCE, SIGNATURE_MISMATCH, SIGNATURE_MISMATCH]
    assert len(receiver.accepted) == 2
