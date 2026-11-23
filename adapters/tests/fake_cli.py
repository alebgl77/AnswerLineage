"""Le côté CLI du protocole de contrôle du Claude Agent SDK, joué par les tests.

Le SDK parle à la CLI Claude Code par des messages JSON, une ligne chacun :
à l'initialisation, il lui déclare ses crochets, chacun sous un identifiant de
rappel ; la CLI lui demande ensuite d'exécuter un rappel (``hook_callback``)
et attend sa réponse. Ce transport tient la place de la CLI : aucun processus,
aucun modèle, aucun réseau. Les crochets passent ainsi par le vrai code du
SDK, de la déclaration à la réponse que reçoit la CLI.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Mapping
from typing import Any

from claude_agent_sdk import Transport

# Le délai qu'un rappel a pour répondre ; un crochet qui ne répond pas fait échouer le test.
ANSWER_TIMEOUT = 5.0


class FakeCli(Transport):
    """La CLI jouée : elle enregistre les crochets déclarés et les déclenche à la demande."""

    def __init__(self) -> None:
        self._outgoing: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._answers: dict[str, asyncio.Future[dict[str, Any]]] = {}
        # Pour chaque événement, les filtres déclarés et leurs identifiants de rappel.
        self.hooks: dict[str, list[tuple[str | None, list[str]]]] = {}
        self._ready = False
        self._counter = 0

    async def connect(self) -> None:
        self._ready = True

    async def write(self, data: str) -> None:
        for line in data.splitlines():
            if line.strip():
                self._receive(json.loads(line))

    def _receive(self, message: Mapping[str, Any]) -> None:
        if message.get("type") == "control_request":
            request = message["request"]
            if request.get("subtype") == "initialize":
                for event, matchers in (request.get("hooks") or {}).items():
                    self.hooks[event] = [
                        (matcher.get("matcher"), list(matcher["hookCallbackIds"]))
                        for matcher in matchers
                    ]
            answer = {"subtype": "success", "request_id": message["request_id"], "response": {}}
            self._outgoing.put_nowait({"type": "control_response", "response": answer})
        elif message.get("type") == "control_response":
            response = message["response"]
            waiting = self._answers.pop(response["request_id"], None)
            if waiting is not None:
                waiting.set_result(response)

    async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        while (message := await self._outgoing.get()) is not None:
            yield message

    async def close(self) -> None:
        self._ready = False
        self._outgoing.put_nowait(None)

    def is_ready(self) -> bool:
        return self._ready

    async def end_input(self) -> None:
        return None

    async def fire(
        self, event: str, payload: Mapping[str, Any], tool_use_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Déclenche les rappels d'un événement, comme la CLI ; rend la réponse de chacun."""
        answers = []
        for matcher, callbacks in self.hooks.get(event, []):
            subject = str(payload.get("tool_name", ""))
            if matcher is not None and not re.fullmatch(matcher, subject):
                continue
            for callback in callbacks:
                self._counter += 1
                request_id = f"cli_{self._counter}"
                answer = asyncio.get_running_loop().create_future()
                self._answers[request_id] = answer
                request = {
                    "subtype": "hook_callback",
                    "callback_id": callback,
                    "input": dict(payload),
                    "tool_use_id": tool_use_id,
                }
                message = {"type": "control_request", "request_id": request_id, "request": request}
                self._outgoing.put_nowait(message)
                response = await asyncio.wait_for(answer, ANSWER_TIMEOUT)
                answers.append(response["response"])
        return answers
