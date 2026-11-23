"""La boîte d'envoi du proxy face à une API d'ingestion en panne, de bout en bout.

Le vrai proxy relaie des appels d'outil devant un faux serveur MCP ; sa boîte
d'envoi remet événements et relevés à la vraie API d'ingestion, sur un vrai
PostgreSQL, et le worker les projette dans le graphe.

- Chaos : l'API refuse la connexion, puis accepte et ne répond jamais, puis
  revient. Aucun appel n'attend, et chaque événement finit dans le graphe, une
  fois. L'API y tourne dans son propre processus, comme en service : celle qui
  ingère l'arriéré ne dispute pas la boucle du proxy.
- Pertes : là où un événement peut disparaître (boîte pleine, rejet par l'API,
  arrêt), la couverture lue par l'API montre chaque perte, dans sa fenêtre.

La latence : ce test vérifie qu'aucun appel n'est bloqué, que la prise en charge
d'un événement ne coûte qu'un ajout en mémoire, et que les appels faits l'API en
panne ne sont pas plus lents que ceux d'un proxy sans boîte, à un seuil tolérant
près : une machine d'intégration continue est partagée et bruyante, et le proxy,
le faux serveur MCP et le client y partagent un processus. Le seuil strict, 5 ms
au p99, est celui du banc de latence de chaque nuit (``make bench``), qui passe
lui aussi par la boîte d'envoi, l'API injoignable.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import socket
import sys
import time
from array import array
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, MutableMapping
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from datetime import timedelta
from typing import Any

import httpx
import pytest
import uvicorn
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_proxy.app import McpProxy
from answerlineage_proxy.config import ProxySettings
from answerlineage_proxy.credentials import ClientCredentials, ClientCredentialsTokens
from answerlineage_proxy.outbox import BACKOFF_CAP, Outbox, OutboxSettings
from answerlineage_proxy.sink import EventSink, NullSink
from answerlineage_workers.queue import process_batch
from api.tests.issuer import (
    EMITTER_CLIENT,
    EMITTER_SECRET,
    EmitterTokens,
    LocalIssuer,
    authentication,
    serving_issuer,
    signed_in,
)
from proxy.tests.conftest import TOOL_RESULT, FakeMcpServer, running, tool_call

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

Scope = MutableMapping[str, Any]
Receive = Callable[[], Any]
Send = Callable[[MutableMapping[str, Any]], Any]

# Appels du chaos, répartis sur les trois états de l'API, et appels de référence, sans boîte.
CALLS = 10_000
BASELINE_CALLS = 1_000
CONCURRENCY = 4
# Un appel que la boîte bloquerait sur le trou noir attendrait au moins le délai de lecture
# de son envoi (10 s) : aucun appel n'en approche.
BLOCKED_S = 8.0
REQUEST_TIMEOUT_S = 3.0
# Seuil tolérant de l'intégration continue : le p99 d'un état de panne au plus le double de
# celui des appels de référence, faits sans boîte, plus 20 ms. Le seuil strict est celui du
# banc de chaque nuit.
P99_FACTOR = 2.0
P99_MARGIN_MS = 20.0
# Ce que la prise en charge d'un événement coûte à un appel : un ajout en mémoire.
EMIT_P99_TOLERANCE_MS = 1.0


class Gate:
    """L'API réelle derrière une porte : ouverte, trou noir qui accepte et ne répond jamais,
    ou trou noir pour les seuls lots d'événements."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.black_hole = False
        self.hold_batches = False
        self.held = 0
        self._released = asyncio.Event()

    def open(self) -> None:
        """L'API revient : les requêtes retenues reçoivent un 503, comme d'un service qui
        redémarre."""
        self.black_hole = False
        self._released.set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and (
            self.black_hole or (self.hold_batches and scope["path"].endswith("/lineage/batch"))
        ):
            self.held += 1
            # Rien n'est répondu : la requête reste ouverte jusqu'au départ du client, ou
            # jusqu'au retour du service.
            gone = asyncio.ensure_future(self._client_gone(receive))
            released = asyncio.ensure_future(self._released.wait())
            await asyncio.wait({gone, released}, return_when=asyncio.FIRST_COMPLETED)
            for task in (gone, released):
                task.cancel()
            if self._released.is_set() and not gone.done():
                await send({"type": "http.response.start", "status": 503, "headers": []})
                await send({"type": "http.response.body", "body": b""})
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _client_gone(receive: Receive) -> None:
        while (await receive())["type"] != "http.disconnect":
            pass


@contextmanager
def reserved_port() -> Iterator[socket.socket]:
    """Un port de la boucle locale réservé sans écouter : une connexion y est refusée."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    try:
        yield sock
    finally:
        sock.close()


@asynccontextmanager
async def served(app: Any, sock: socket.socket) -> AsyncIterator[None]:
    """Sert ``app`` sur le port réservé : la connexion n'y est plus refusée."""
    config = uvicorn.Config(app, log_level="warning", access_log=False, timeout_graceful_shutdown=1)
    server = uvicorn.Server(config)
    task = asyncio.ensure_future(server.serve(sockets=[sock]))
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.005)
    try:
        yield
    finally:
        server.should_exit = True
        await task


# L'API d'ingestion dans son propre processus : elle annonce son port, puis s'arrête à la
# fermeture de son entrée.
_API_PROCESS = """
import asyncio, json, sys
import uvicorn
from answerlineage_api.app import create_app


async def main():
    config = uvicorn.Config(
        create_app(), host="127.0.0.1", port=0, log_level="warning", access_log=False
    )
    server = uvicorn.Server(config)
    serving = asyncio.ensure_future(server.serve())
    while not server.started:
        if serving.done():
            serving.result()
            return
        await asyncio.sleep(0.01)
    print(json.dumps({"port": server.servers[0].sockets[0].getsockname()[1]}), flush=True)
    await asyncio.to_thread(sys.stdin.buffer.read)
    server.should_exit = True
    await serving


asyncio.run(main())
"""


@asynccontextmanager
async def api_process(database: URL, issuer: LocalIssuer) -> AsyncIterator[int]:
    """La vraie API d'ingestion, dans un processus à part, sur la base du test ; son port.

    Elle lit les clés de l'émetteur servi en HTTP, comme en service."""
    environment = {
        **os.environ,
        "ANSWERLINEAGE_DATABASE_URL": database.render_as_string(hide_password=False),
        "ANSWERLINEAGE_OIDC_ISSUER": issuer.url,
        "ANSWERLINEAGE_OIDC_AUDIENCE": issuer.audience,
        "ANSWERLINEAGE_OIDC_EMITTER_CLIENTS": EMITTER_CLIENT,
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _API_PROCESS,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        env=environment,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    try:
        line = await asyncio.wait_for(process.stdout.readline(), 60)
        assert line, "l'API n'a pas démarré"
        yield int(json.loads(line)["port"])
    finally:
        process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), 30)
        except TimeoutError:  # pragma: no cover — une API qui ne s'arrête pas.
            process.kill()
            await process.wait()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(65_536):
            writer.write(chunk)
            await writer.drain()
    except ConnectionError:
        pass
    finally:
        writer.close()


class Switch:
    """Le port de l'API, réservé : connexion refusée, puis trou noir qui accepte et ne répond
    jamais, puis relais vers l'API. À son retour, les connexions retenues sont fermées, comme
    par un service qui redémarre."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._server: asyncio.Server | None = None
        self._target: int | None = None
        self._held: set[asyncio.StreamWriter] = set()
        self.held = 0

    async def black_hole(self) -> None:
        self._server = await asyncio.start_server(self._handle, sock=self._sock)

    def forward(self, port: int) -> None:
        self._target = port
        for writer in list(self._held):
            writer.close()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server.close_clients()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._target is None:
            self.held += 1
            self._held.add(writer)
            try:
                # Rien n'est répondu : la connexion reste ouverte jusqu'au départ du client.
                while await reader.read(65_536):
                    pass
            except ConnectionError:
                pass
            finally:
                self._held.discard(writer)
                writer.close()
            return
        api_reader, api_writer = await asyncio.open_connection("127.0.0.1", self._target)
        await asyncio.gather(_pipe(reader, api_writer), _pipe(api_reader, writer))


class TimedOutbox(Outbox):
    """La boîte d'envoi, dont on mesure ce que coûte la prise en charge d'un événement."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.emit_ms = array("d")

    def emit(self, event: Mapping[str, Any]) -> None:
        started = time.perf_counter_ns()
        super().emit(event)
        self.emit_ms.append((time.perf_counter_ns() - started) / 1_000_000)


def p99(values: array[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.99 * len(ordered)) - 1)]


@asynccontextmanager
async def proxied(sink: EventSink, **options: Any) -> AsyncIterator[tuple[str, McpProxy]]:
    """Le vrai proxy devant un faux serveur MCP, servis comme en ligne de commande."""
    upstream = FakeMcpServer()
    async with running(upstream) as upstream_url:
        proxy = McpProxy(ProxySettings.create(upstream_url, "meteo"), sink, **options)
        async with running(proxy, transparent=True) as proxy_url:
            yield proxy_url + "/mcp", proxy


async def calls(url: str, count: int, *, concurrency: int = CONCURRENCY) -> array[float]:
    """``count`` appels d'outil ; leurs latences vues du client, en millisecondes."""
    latencies = array("d")
    remaining = [count]
    headers = {"content-type": "application/json", "accept": "application/json"}
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(trust_env=False, timeout=30, limits=limits) as client:

        async def worker() -> None:
            while remaining[0] > 0:
                remaining[0] -= 1
                started = time.perf_counter_ns()
                response = await client.post(url, content=tool_call(), headers=headers)
                latencies.append((time.perf_counter_ns() - started) / 1_000_000)
                assert response.status_code == 200
                assert response.content == TOOL_RESULT

        await asyncio.gather(*(worker() for _ in range(concurrency)))
    return latencies


async def until(condition: Callable[[], bool], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition jamais remplie"
        await asyncio.sleep(0.02)


async def settled(box: Outbox, *, queued: int, dropped: int) -> None:
    """Attend que la boîte compte ``queued`` événements et ``dropped`` pertes par débordement,
    sans lot en cours d'envoi qui fausserait l'un des deux."""
    await until(
        lambda: box.queued == queued and box.stats.dropped_overflow == dropped,
        timeout=REQUEST_TIMEOUT_S + 5,
    )


async def project(engine: AsyncEngine) -> None:
    """Le worker projette tout ce qui attend dans la file."""
    while not (await process_batch(engine, batch_size=1_000)).empty:
        pass


async def scalar(engine: AsyncEngine, statement: str) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(text(statement))).scalar_one()


async def coverage_of(engine: AsyncEngine) -> dict[str, Any]:
    """La couverture de l'émetteur lue par l'API, sur toute la période de ses relevés."""
    async with engine.connect() as conn:
        start, end = (
            await conn.execute(
                text("SELECT min(window_start), max(window_end) FROM coverage_report")
            )
        ).one()
    transport = httpx.ASGITransport(app=create_app(engine, authentication=authentication()))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://localhost", headers=signed_in()
    ) as client:
        response = await client.get(
            "/api/v1/coverage", params={"from": start.isoformat(), "to": end.isoformat()}
        )
    assert response.status_code == 200
    [emitter] = response.json()["emitters"]
    coverage: dict[str, Any] = emitter["coverage"]
    return coverage


def holes(coverage: Mapping[str, Any], kind: str) -> list[tuple[str, str, int]]:
    return [
        (hole["window"]["from"], hole["window"]["to"], hole["count"])
        for hole in coverage["holes"]
        if hole["kind"] == kind
    ]


def every_loss_is_marked(coverage: Mapping[str, Any], *, calls: int, lost: int) -> None:
    """Chaque perte est dite, dans sa fenêtre : les événements que l'émetteur déclare perdus
    sont exactement ceux qui manquent au graphe, fenêtre par fenêtre."""
    assert coverage["status"] == "measured"
    assert coverage["calls_seen"] == calls
    assert coverage["events_lost"] == lost
    assert coverage["events_in_graph"] == calls - lost
    assert holes(coverage, "events_lost") == holes(coverage, "missing_from_graph")
    assert sum(count for _, _, count in holes(coverage, "events_lost")) == lost


async def test_ten_thousand_calls_pass_while_the_api_is_down_then_reach_the_graph_once(
    engine: AsyncEngine, database: URL
) -> None:
    """Étant donné l'API d'ingestion indisponible, quand 10 000 appels passent, alors aucun
    n'est retardé et les événements partent au retour du service."""
    async with proxied(NullSink()) as (url, _):
        baseline = await calls(url, BASELINE_CALLS)

    phases: dict[str, array[float]] = {}
    with reserved_port() as sock:
        settings = OutboxSettings.create(f"http://127.0.0.1:{sock.getsockname()[1]}")
        async with AsyncExitStack() as stack:
            # Le proxy obtient son jeton de l'émetteur par client_credentials, et l'API en
            # vérifie la signature par le JWKS de l'émetteur : tous deux joints en HTTP.
            issuer, _ = await stack.enter_async_context(serving_issuer())
            credentials = ClientCredentials.create(issuer.url, EMITTER_CLIENT, EMITTER_SECRET)
            box = TimedOutbox(settings, tokens=ClientCredentialsTokens(credentials))
            switch = Switch(sock)
            stack.push_async_callback(switch.close)
            api_port = await stack.enter_async_context(api_process(database, issuer))
            url, proxy = await stack.enter_async_context(proxied(box))
            # L'API coupée : la connexion est refusée.
            phases["refusée"] = await calls(url, CALLS // 3)
            assert box.stats.sent == 0
            assert box.stats.retries > 0
            # Puis un trou noir : la connexion est acceptée, rien n'est jamais répondu.
            await switch.black_hole()
            phases["trou noir"] = await calls(url, CALLS // 3)
            # La boîte attend peut-être encore sa reprise, jusqu'au plafond : le trou noir
            # n'est éprouvé qu'une fois qu'une connexion y est retenue.
            await until(lambda: switch.held > 0, timeout=BACKOFF_CAP + 10)
            assert box.stats.sent == 0
            # Puis l'API revient : la boîte se vide.
            switch.forward(api_port)
            phases["rétablie"] = await calls(url, CALLS - 2 * (CALLS // 3))
            await until(lambda: box.stats.sent == CALLS, timeout=180)
            await proxy.aclose()

    # Aucun appel bloqué, et peu de latence ajoutée, même l'API en panne.
    reference = p99(baseline)
    for name, latencies in phases.items():
        assert len(latencies) >= CALLS // 3, name
        assert max(latencies) < BLOCKED_S * 1000, name
        assert p99(latencies) <= P99_FACTOR * reference + P99_MARGIN_MS, (name, reference)
    assert len(box.emit_ms) == CALLS
    assert p99(box.emit_ms) <= EMIT_P99_TOLERANCE_MS

    # Rien de perdu, et chaque événement dans le graphe, une fois.
    stats = box.stats
    assert (stats.dropped_overflow, stats.dropped_shutdown, stats.rejected) == (0, 0, 0)
    await project(engine)
    assert await scalar(engine, "SELECT count(*) FROM event_raw WHERE tool_call") == CALLS
    assert await scalar(engine, "SELECT count(DISTINCT run_id) FROM event_raw") == CALLS
    assert await scalar(engine, "SELECT count(*) FROM run") == CALLS
    assert await scalar(engine, "SELECT count(*) FROM ingest_queue") == 0
    coverage = await coverage_of(engine)
    every_loss_is_marked(coverage, calls=CALLS, lost=0)
    assert coverage["rate"] == 1.0


async def test_a_full_outbox_drops_the_oldest_and_the_coverage_shows_the_hole_in_its_window(
    engine: AsyncEngine,
) -> None:
    """Étant donné la boîte pleine, quand un événement arrive, alors le plus ancien est
    abandonné, le compteur de pertes augmente, et la couverture montre le trou."""
    gate = Gate(create_app(engine, authentication=authentication()))
    with reserved_port() as sock:
        settings = OutboxSettings.create(f"http://127.0.0.1:{sock.getsockname()[1]}", max_events=20)
        box = Outbox(settings, request_timeout=REQUEST_TIMEOUT_S, tokens=EmitterTokens())
        async with AsyncExitStack() as stack:
            url, proxy = await stack.enter_async_context(
                proxied(box, coverage_window=timedelta(seconds=1))
            )
            await calls(url, 50, concurrency=1)
            # Un lot en cours d'envoi sort de la borne le temps de l'envoi : son échec le remet
            # en file, et l'éviction se fait alors. L'état se lit une fois le lot revenu.
            await settled(box, queued=20, dropped=30)
            # Des fenêtres se closent l'API coupée ; puis leurs événements sont évincés par de
            # nouveaux : leurs relevés, déjà en file, sont corrigés.
            await asyncio.sleep(1.5)
            await calls(url, 20, concurrency=1)
            await settled(box, queued=20, dropped=50)
            await stack.enter_async_context(served(gate, sock))
            await until(lambda: box.queued == 0 and box.stats.sent == 20, timeout=30)
            await proxy.aclose()

    await project(engine)
    every_loss_is_marked(await coverage_of(engine), calls=70, lost=50)


async def test_events_rejected_by_the_api_are_marked_lost(engine: AsyncEngine) -> None:
    """L'API refuse chaque événement, plus lourd que sa borne ; elle accepte les relevés."""
    strict = create_app(engine, max_body_bytes=1024, authentication=authentication())
    with reserved_port() as sock:
        settings = OutboxSettings.create(f"http://127.0.0.1:{sock.getsockname()[1]}")
        box = Outbox(settings, request_timeout=REQUEST_TIMEOUT_S, tokens=EmitterTokens())
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(served(strict, sock))
            url, proxy = await stack.enter_async_context(proxied(box))
            await calls(url, 12, concurrency=1)
            await until(lambda: box.stats.rejected == 12, timeout=30)
            await proxy.aclose()

    assert box.stats.sent == 0, box.stats
    assert box.stats.reports_sent > 0, box.stats
    await project(engine)
    every_loss_is_marked(await coverage_of(engine), calls=12, lost=12)


async def test_events_still_waiting_at_shutdown_are_marked_lost_by_the_last_report(
    engine: AsyncEngine,
) -> None:
    """À l'arrêt, les lots ne passent plus, les relevés si : ce qui reste est perdu, et dit."""
    gate = Gate(create_app(engine, authentication=authentication()))
    gate.hold_batches = True
    with reserved_port() as sock:
        settings = OutboxSettings.create(
            f"http://127.0.0.1:{sock.getsockname()[1]}", flush_seconds=1.5
        )
        box = Outbox(settings, request_timeout=30, tokens=EmitterTokens())
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(served(gate, sock))
            url, proxy = await stack.enter_async_context(proxied(box))
            await calls(url, 9, concurrency=1)
            await until(lambda: gate.held > 0, timeout=10)
            started = time.monotonic()
            await proxy.aclose()
            assert time.monotonic() - started < 1.5 + 0.5

    assert (box.stats.sent, box.stats.dropped_shutdown) == (0, 9)
    await project(engine)
    every_loss_is_marked(await coverage_of(engine), calls=9, lost=9)
