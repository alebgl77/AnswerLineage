"""La réponse d'un agent et ses destinataires, de l'adaptateur au graphe.

Le parcours est complet : les crochets de l'adaptateur voient une demande et son
appel d'outil, l'intégration déclare la réponse qu'elle a publiée dans un fil
Slack ; les événements partent par la vraie boîte d'envoi vers l'API
d'ingestion, un worker les projette dans le graphe, l'API de lecture retrouve la
réponse par son message. La réponse, sa diffusion et ses destinataires
pseudonymisés sont rattachés au run de l'agent. Un canari placé dans la réponse
et dans les identités ne se retrouve ni dans les événements reçus, ni dans la
base, ni au journal ; une identité refusée par le calcul n'est recopiée nulle part.
"""

from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import logging
import secrets
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from adapters.tests.test_claude_agent_sdk import (
    AGENT,
    SESSION,
    fire,
    prompt_submitted,
    stopped,
    tool_used,
)
from answerlineage_adapters.claude_agent_sdk import ClaudeAgentLineage
from answerlineage_api.app import create_app
from answerlineage_api.auth.roles import Role
from answerlineage_api.pseudonyms import vault
from answerlineage_emitter.delivery import SLACK, slack_message_ref
from answerlineage_emitter.fingerprint import ANSWER_LABEL, FingerprintKey
from answerlineage_emitter.outbox import Outbox, OutboxSettings
from answerlineage_emitter.pseudonym import PseudonymKey
from answerlineage_workers.queue import process_batch
from api.tests.issuer import authentication, signed_in
from tests.test_no_token_anywhere import dump
from tests.test_pseudonymized_recipients import traces

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

MESSAGE = slack_message_ref("C024BE91L", "1355517523.000005")


class Recorder(httpx.AsyncBaseTransport):
    """Le chemin vers l'API : il garde chaque lot tel que l'API le reçoit, puis le lui passe."""

    def __init__(self, app: Any) -> None:
        self._app = httpx.ASGITransport(app=app)
        self.batches: list[list[dict[str, Any]]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        self.batches.append(json.loads(gzip.decompress(body)))
        return await self._app.handle_async_request(request)


async def test_the_answer_and_its_pseudonymized_recipients_are_attached_to_the_run(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """Critères : quand le run se termine, l'empreinte, le canal, la référence du message et
    les destinataires pseudonymisés sont enregistrés ; aucun fragment de la réponse n'est dans
    l'événement."""
    mark = "canari" + secrets.token_hex(4)
    answer = f"Le taux de change du jour est de 1,08. {mark}"
    person, member = f"Camille.{mark}@Exemple.FR", "U07AB12CD"
    refused = f"Camille {mark} <camille.martin@exemple.fr>"
    fingerprints, pseudonyms = secrets.token_bytes(32), secrets.token_bytes(32)

    app = create_app(engine, authentication=authentication())
    recorder = Recorder(app)
    with caplog.at_level(logging.DEBUG):
        async with httpx.AsyncClient(transport=recorder, headers=signed_in(Role.EMITTER)) as client:
            box = Outbox(
                OutboxSettings.create("http://127.0.0.1:9", flush_seconds=5), client=client
            )
            adapter = ClaudeAgentLineage(
                AGENT,
                box,
                fingerprint_key=FingerprintKey(fingerprints),
                pseudonym_key=PseudonymKey(pseudonyms),
            )
            async with adapter:
                await fire(adapter, "UserPromptSubmit", prompt_submitted(f"Le taux ? {mark}"))
                await fire(adapter, "PostToolUse", tool_used())
                await fire(adapter, "Stop", stopped(answer))
                adapter.record_delivery(
                    SLACK, MESSAGE, [person, member, refused], answer=answer, session_id=SESSION
                )
        assert (box.stats.sent, box.stats.rejected) == (4, 0)
        while not (await process_batch(engine)).empty:
            pass

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            headers=signed_in(),
        ) as reader:
            found = await reader.get(
                "/api/v1/answers", params={"channel": SLACK, "message_ref": MESSAGE}
            )
            (delivered,) = found.json()["answers"]
            sources = await reader.get(f"/api/v1/answers/{delivered['id']}/sources")

    # La réponse est celle du run de l'agent, retrouvée par son message, sous son empreinte.
    expected = hmac.new(fingerprints, ANSWER_LABEL + b"\x00" + answer.encode(), hashlib.sha256)
    assert delivered["fingerprint"] == "hmac-sha256:" + expected.hexdigest()
    assert delivered["agent"] == AGENT.job()
    assert [tool["name"] for tool in sources.json()["tools"]] == ["execute_sql"]
    async with engine.connect() as conn:
        state = await conn.scalar(
            text("SELECT state FROM run WHERE run_id = :run"), {"run": delivered["run_id"]}
        )
        linked = (
            await conn.execute(
                text(
                    "SELECT r.pseudonym FROM answer a"
                    " JOIN delivery d ON d.answer_id = a.id"
                    " JOIN delivery_recipient l ON l.delivery_id = d.id"
                    " JOIN recipient r ON r.id = l.recipient_id"
                    " WHERE a.run_id = :run AND d.channel = :channel AND d.message_ref = :message"
                ),
                {"run": delivered["run_id"], "channel": SLACK, "message": MESSAGE},
            )
        ).scalars()
        recipients = sorted(linked)
    # La diffusion, arrivée après la fin du run, ne change pas son état.
    assert state == "COMPLETE"
    # Les destinataires sont ceux que l'API reconnaîtrait en révélant : son propre calcul, sous
    # la même clé, donne les mêmes pseudonymes. L'identité refusée n'en a pas.
    by_the_api = vault.PseudonymKey(pseudonyms)
    assert recipients == sorted(
        value
        for identity in (person, member)
        if (value := vault.pseudonym_of(by_the_api, identity))
    )
    assert vault.pseudonym_of(by_the_api, refused) is None
    assert (adapter.stats.deliveries, adapter.stats.recipients_refused) == (1, 1)

    # Rien de la réponse ni des identités, sous aucune forme : ni dans les événements reçus, ni
    # dans la base, ni au journal.
    received = json.dumps(recorder.batches, ensure_ascii=False)
    stored = await dump(engine)
    for place in (received, stored, caplog.text):
        assert mark not in place
        assert "1,08" not in place
        for identity in (person, member, refused):
            for trace in traces(identity):
                assert trace not in place
