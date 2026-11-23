"""Deux réplicas du proxy derrière un répartiteur : l'un est arrêté sous charge, aucun appel
n'échoue.

Les réplicas sont lancés par la ligne de commande, chacun dans son processus, devant le
même serveur MCP de test, avec un port d'administration. Le répartiteur, écrit ici,
fait ce que fait un équilibreur réel : il interroge ``/readyz`` de chaque réplica à
intervalle régulier, retire de sa rotation un réplica qui ne se dit pas prêt, et
répartit les appels entre les autres, sans jamais reprendre un appel en échec.

Des clients appellent l'outil sans relâche, des appels courts et des appels longs ;
SIGTERM arrête l'un des réplicas au moment où un appel long vient d'y commencer. Le
test mesure la fenêtre de l'arrêt : le retrait du réplica par le répartiteur, la
fermeture de son écoute à la fin du délai de retrait, la fin de l'appel long pendant
le délai de grâce, puis la sortie du processus.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import re
import signal
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx
import pytest

from answerlineage_proxy.app import Receive, Scope, Send
from proxy.tests.conftest import running, until

pytestmark = pytest.mark.anyio

# Délai de retrait et délai de grâce des réplicas ; période des sondes du répartiteur.
DELAY = 1.0
GRACE = 5
PROBE_PERIOD = 0.1
# Durée d'un appel court et d'un appel long au serveur MCP de test.
SHORT = 0.02
LONG = 2.0
CLIENTS = 8
_READY = re.compile(r"Prêt : appels sur 127\.0\.0\.1:(\d+), santé sur 127\.0\.0\.1:(\d+)")


def result(identifier: int) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "id": identifier, "result": {"content": [], "isError": False}}
    ).encode()


async def slow_mcp_server(scope: Scope, receive: Receive, send: Send) -> None:
    """Serveur MCP de test : répond à un appel d'outil après la pause qu'il demande."""
    if scope["type"] != "http":
        return
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if not message.get("more_body", False):
            break
    call = json.loads(body)
    await asyncio.sleep(call["params"]["arguments"]["pause"])
    answer = result(call["id"])
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(answer)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": answer, "more_body": False})


def tool_call(identifier: int, pause: float) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": identifier,
            "method": "tools/call",
            "params": {"name": "get_weather", "arguments": {"pause": pause}},
        }
    ).encode()


@dataclass
class Replica:
    """Un proxy lancé par la ligne de commande ; son journal, lu au fil de l'eau."""

    process: asyncio.subprocess.Process
    url: str
    health: str
    journal: list[str] = field(default_factory=list)


@asynccontextmanager
async def replica(upstream: str) -> AsyncIterator[Replica]:
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("ANSWERLINEAGE_")
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "answerlineage_proxy.cli",
        "--upstream",
        upstream,
        "--server-name",
        "meteo",
        "--port",
        "0",
        "--admin-port",
        "0",
        "--shutdown-delay-seconds",
        str(DELAY),
        "--shutdown-grace-seconds",
        str(GRACE),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )
    assert process.stderr is not None
    journal: list[str] = []
    ready = None
    while ready is None:
        line = (await asyncio.wait_for(process.stderr.readline(), 30)).decode()
        assert line, "".join(journal)
        journal.append(line)
        ready = _READY.search(line)

    async def read_journal() -> None:
        assert process.stderr is not None
        while line := await process.stderr.readline():
            journal.append(line.decode())

    reading = asyncio.ensure_future(read_journal())
    started = Replica(
        process, f"http://127.0.0.1:{ready[1]}", f"http://127.0.0.1:{ready[2]}", journal
    )
    try:
        yield started
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
        await reading


@dataclass
class Served:
    """Un appel relayé par le répartiteur : vers quel réplica, quand, et pour quelle durée."""

    backend: str
    started: float
    ended: float
    long: bool


@dataclass
class Backend:
    name: str
    replica: Replica
    in_rotation: bool = False
    withdrawn_at: float | None = None
    long_started: list[float] = field(default_factory=list)


class Balancer:
    """Répartiteur de test : tourniquet sur les réplicas prêts, sondes de ``/readyz``."""

    def __init__(self, backends: list[Backend]) -> None:
        self.backends = backends
        self.served: list[Served] = []
        self._turn = itertools.count()
        self._client = httpx.AsyncClient(trust_env=False, timeout=30)
        self._probe = httpx.AsyncClient(trust_env=False, timeout=0.5)

    async def watch(self) -> None:
        while True:
            for backend in self.backends:
                try:
                    answer = await self._probe.get(backend.replica.health + "/readyz")
                    ready = answer.status_code == 200
                except httpx.HTTPError:
                    ready = False
                if backend.in_rotation and not ready:
                    backend.withdrawn_at = time.monotonic()
                backend.in_rotation = ready
            await asyncio.sleep(PROBE_PERIOD)

    async def aclose(self) -> None:
        await self._client.aclose()
        await self._probe.aclose()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        ready = [backend for backend in self.backends if backend.in_rotation]
        if not ready:
            await _answer(send, 503, b"aucun replica pret")
            return
        backend = ready[next(self._turn) % len(ready)]
        long = json.loads(body)["params"]["arguments"]["pause"] >= LONG
        started = time.monotonic()
        if long:
            backend.long_started.append(started)
        try:
            relayed = await self._client.post(
                backend.replica.url + scope["path"],
                content=body,
                headers={"content-type": "application/json"},
            )
        except httpx.HTTPError:
            # Un équilibreur réel pourrait reprendre l'appel ailleurs : celui-ci ne le fait
            # jamais, pour que le moindre échec du réplica se voie.
            await _answer(send, 502, b"replica injoignable")
            return
        self.served.append(Served(backend.name, started, time.monotonic(), long))
        await _answer(send, relayed.status_code, relayed.content)


async def _answer(send: Send, status: int, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


@dataclass
class Load:
    """Des clients qui appellent l'outil sans relâche à travers le répartiteur."""

    url: str
    failures: list[str] = field(default_factory=list)
    calls: int = 0
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1))

    async def client(self, pause: float) -> None:
        async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
            while not self._stop.is_set():
                identifier = next(self._ids)
                try:
                    answer = await client.post(
                        self.url + "/mcp",
                        content=tool_call(identifier, pause),
                        headers={"content-type": "application/json"},
                    )
                    if (answer.status_code, answer.content) != (200, result(identifier)):
                        self.failures.append(f"{answer.status_code} {answer.content[:60]!r}")
                except httpx.HTTPError as error:
                    self.failures.append(type(error).__name__)
                self.calls += 1

    def stop(self) -> None:
        self._stop.set()


@dataclass
class Window:
    """La fenêtre de l'arrêt d'un réplica, mesurée depuis l'envoi de SIGTERM, en secondes."""

    withdrawn: float
    last_routed: float
    long_answered: float
    exited: float


async def test_two_replicas_under_load_one_stopped_by_sigterm_no_call_fails() -> None:
    async with (
        running(slow_mcp_server) as upstream,
        replica(upstream) as first,
        replica(upstream) as second,
    ):
        backends = [Backend("premier", first), Backend("second", second)]
        balancer = Balancer(backends)
        watching = asyncio.ensure_future(balancer.watch())
        try:
            async with running(balancer) as balancer_url:
                await until(lambda: all(backend.in_rotation for backend in backends))
                load = Load(balancer_url)
                clients = [
                    *(asyncio.ensure_future(load.client(SHORT)) for _ in range(CLIENTS)),
                    asyncio.ensure_future(load.client(LONG)),
                ]
                # Le réplica arrêté est celui où un appel long vient de commencer : l'appel
                # sera encore en cours quand l'écoute se fermera.
                await until(
                    lambda: (
                        len(balancer.served) > 50
                        and any(
                            backend.long_started
                            and time.monotonic() - backend.long_started[-1] < 0.1
                            for backend in backends
                        )
                    ),
                    timeout=60,
                )
                stopped, kept = sorted(
                    backends,
                    key=lambda backend: backend.long_started[-1] if backend.long_started else 0,
                    reverse=True,
                )
                long_call = stopped.long_started[-1]
                since = time.monotonic()
                stopped.replica.process.send_signal(signal.SIGTERM)
                code = await asyncio.wait_for(stopped.replica.process.wait(), DELAY + GRACE + 10)
                exited = time.monotonic() - since
                # La charge continue sur le réplica restant.
                await asyncio.sleep(0.5)
                load.stop()
                await asyncio.gather(*clients)
        finally:
            watching.cancel()
            await asyncio.gather(watching, return_exceptions=True)
            await balancer.aclose()

    assert load.failures == []
    assert code == 0
    assert stopped.withdrawn_at is not None
    on_stopped = [call for call in balancer.served if call.backend == stopped.name]
    (long_served,) = [call for call in on_stopped if call.long and call.started == long_call]
    window = Window(
        withdrawn=stopped.withdrawn_at - since,
        last_routed=max(call.started for call in on_stopped) - since,
        long_answered=long_served.ended - since,
        exited=exited,
    )
    # Le répartiteur retire le réplica avant que celui-ci ferme son écoute : aucun appel
    # ne trouve porte close.
    assert window.withdrawn < DELAY
    assert window.last_routed < DELAY
    # L'appel long, en cours quand l'écoute s'est fermée, est rendu pendant le délai de grâce.
    assert DELAY < window.long_answered < DELAY + GRACE
    assert window.long_answered <= window.exited < DELAY + GRACE + 5
    # Le réplica restant a servi les appels d'après le retrait.
    after = [call for call in balancer.served if call.started > since + window.withdrawn]
    assert after
    assert {call.backend for call in after} == {kept.name}
    journal = "".join(stopped.replica.journal)
    assert "Arrêt demandé (SIGTERM) : non prêt ; le proxy sert encore 1 s" in journal
    assert "Écoute fermée : 1 appel(s) en cours, 5 s au plus pour se terminer." in journal
    assert "coupé" not in journal
