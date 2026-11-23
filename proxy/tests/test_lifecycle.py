"""Le cycle de vie du proxy HTTP : sondes de santé sur un port à part, arrêt gracieux.

Le proxy est servi comme la ligne de commande le sert, dans le processus du test,
devant le faux serveur MCP ; l'arrêt est demandé comme un signal le demanderait.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from answerlineage_proxy.app import McpProxy
from answerlineage_proxy.config import ProxySettings
from answerlineage_proxy.lifecycle import Lifecycle
from answerlineage_proxy.sink import EventSink, MemorySink, Settled
from proxy.tests.conftest import (
    MODERN_HEADERS,
    MODERN_META,
    SSE_FIRST,
    SSE_LAST,
    TOOL_RESULT,
    FakeMcpServer,
    exchange,
    running,
    tool_call,
    until,
)

pytestmark = pytest.mark.anyio

CALL = tool_call(**MODERN_META)


@dataclass
class Served:
    upstream: FakeMcpServer
    proxy: McpProxy
    lifecycle: Lifecycle
    task: asyncio.Future[int]
    sink: EventSink

    @property
    def url(self) -> str:
        assert self.lifecycle.address is not None
        host, port = self.lifecycle.address
        return f"http://{host}:{port}"

    @property
    def admin(self) -> str:
        assert self.lifecycle.admin_address is not None
        host, port = self.lifecycle.admin_address
        return f"http://{host}:{port}"

    async def stopped(self, timeout: float = 20) -> int:
        return await asyncio.wait_for(self.task, timeout)


@asynccontextmanager
async def served(
    *,
    delay: float = 5.0,
    grace: int = 15,
    admin: bool = True,
    sink: EventSink | None = None,
) -> AsyncIterator[Served]:
    """Le proxy servi par son cycle de vie devant le faux serveur MCP, sur des ports tirés."""
    upstream = FakeMcpServer()
    async with running(upstream) as upstream_url:
        settings = ProxySettings.create(
            upstream_url,
            "meteo",
            port=0,
            admin_port=0 if admin else None,
            shutdown_delay=delay,
            shutdown_grace=grace,
        )
        destination = sink if sink is not None else MemorySink()
        proxy = McpProxy(settings, destination)
        lifecycle = Lifecycle(proxy, settings)
        task = asyncio.ensure_future(lifecycle.serve())
        listening = asyncio.ensure_future(lifecycle.listening.wait())
        await asyncio.wait({task, listening}, timeout=20, return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            task.result()
        try:
            yield Served(upstream, proxy, lifecycle, task, destination)
        finally:
            listening.cancel()
            if not task.done():
                lifecycle.request_stop(signal.SIGINT)
                lifecycle.request_stop(signal.SIGINT)
                await asyncio.wait_for(task, 30)


async def call(url: str, path: str = "/mcp") -> httpx.Response:
    async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
        return await client.post(url + path, headers=dict(MODERN_HEADERS), content=CALL)


async def refused(url: str) -> bool:
    """Vrai quand plus rien n'écoute à cette adresse. La requête n'est pas un appel d'outil :
    relayée, elle n'émet aucun événement."""
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            await client.delete(url + "/mcp")
    except httpx.ConnectError:
        return True
    return False


async def test_the_admin_port_says_alive_and_ready_on_the_loopback_only() -> None:
    async with served() as s:
        host, _ = s.lifecycle.admin_address or ("", 0)
        assert host == "127.0.0.1"
        live = await exchange("GET", s.admin + "/livez")
        ready = await exchange("GET", s.admin + "/readyz")
        head = await exchange("HEAD", s.admin + "/readyz")
        assert (live.status, live.body) == (200, b'{"status":"alive"}')
        assert (ready.status, ready.body) == (200, b'{"status":"ready"}')
        assert (head.status, head.body) == (200, b"")
        assert live.header("cache-control") == ["no-store"]
        # Ni en-tête Server ni Date : le port d'administration ne dit rien du logiciel.
        assert live.header("server") == live.header("date") == []


async def test_the_admin_port_answers_a_closed_list_and_nothing_else() -> None:
    async with served() as s:
        unknown = await exchange("GET", s.admin + "/mcp")
        posted = await exchange("POST", s.admin + "/readyz", content=CALL)
        assert (unknown.status, unknown.body) == (404, b'{"error":"not_found"}')
        assert (posted.status, posted.body) == (405, b'{"error":"method_not_allowed"}')
        assert posted.header("allow") == ["GET, HEAD"]
        # Le port d'administration ne relaie rien au serveur MCP.
        assert s.upstream.requests == []


@pytest.mark.parametrize(
    ("headers", "refusal"),
    [
        ([("host", "attaquant.exemple:8080")], b'{"error":"host_not_allowed"}'),
        ([("origin", "https://attaquant.exemple")], b'{"error":"origin_not_allowed"}'),
        ([("sec-fetch-site", "cross-site")], b'{"error":"cross_site_not_allowed"}'),
    ],
)
async def test_a_page_of_another_site_cannot_read_the_admin_port(
    headers: list[tuple[str, str]], refusal: bytes
) -> None:
    async with served() as s:
        answered = await exchange("GET", s.admin + "/readyz", headers=headers)
        assert (answered.status, answered.body) == (403, refusal)


async def test_without_an_admin_port_the_proxy_listens_for_calls_only() -> None:
    async with served(admin=False) as s:
        assert s.lifecycle.admin_address is None
        assert (await call(s.url)).status_code == 200


async def test_on_sigterm_the_proxy_is_not_ready_but_serves_for_the_withdrawal_delay() -> None:
    async with served(delay=1.0) as s:
        since = time.monotonic()
        s.lifecycle.request_stop(signal.SIGTERM)
        await until(lambda: s.lifecycle.health.state == "draining")
        ready = await exchange("GET", s.admin + "/readyz")
        live = await exchange("GET", s.admin + "/livez")
        assert (ready.status, ready.body) == (503, b'{"status":"draining"}')
        assert live.status == 200
        # Un répartiteur qui n'a pas encore vu le proxy non prêt lui envoie encore des appels.
        relayed = await call(s.url)
        assert (relayed.status_code, relayed.content) == (200, TOOL_RESULT)
        assert time.monotonic() - since < 1.0
        await until(lambda: s.task.done(), timeout=10)
        assert await s.stopped() == 0
        assert time.monotonic() - since >= 1.0
        assert await refused(s.url)


@pytest.mark.parametrize("second", [signal.SIGTERM, signal.SIGINT])
async def test_a_second_signal_cuts_the_withdrawal_delay(second: signal.Signals) -> None:
    async with served(delay=60) as s:
        s.lifecycle.request_stop(signal.SIGTERM)
        await until(lambda: s.lifecycle.health.state == "draining")
        s.lifecycle.request_stop(second)
        assert await s.stopped(timeout=10) == 0


async def test_sigint_skips_the_withdrawal_delay() -> None:
    async with served(delay=60) as s:
        s.lifecycle.request_stop(signal.SIGINT)
        assert await s.stopped(timeout=10) == 0


async def test_a_call_in_flight_is_answered_within_the_grace_delay_once_listening_stops() -> None:
    async with served(delay=0, grace=10) as s:
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            request = client.build_request(
                "POST", s.url + "/sse", headers=dict(MODERN_HEADERS), content=CALL
            )
            response = await client.send(request, stream=True)
            stream = response.aiter_raw()
            assert await anext(stream) == SSE_FIRST
            s.lifecycle.request_stop(signal.SIGTERM)
            # L'écoute est fermée : un nouvel appel ne trouve plus le proxy.
            await until(lambda: s.lifecycle.health.state == "draining")
            deadline = time.monotonic() + 5
            while not await refused(s.url):
                assert time.monotonic() < deadline
                await asyncio.sleep(0.02)
            assert not s.task.done()
            # L'appel en cours, lui, se termine.
            s.upstream.gate.set()
            rest = b"".join([chunk async for chunk in stream])
            await response.aclose()
        assert rest == SSE_LAST
        assert await s.stopped() == 0
        assert isinstance(s.sink, MemorySink)
        (event,) = s.sink.events
        assert event["eventType"] == "COMPLETE"


async def test_a_call_longer_than_the_grace_delay_is_cut_and_its_event_says_it_failed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with served(delay=0, grace=1) as s:
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            request = client.build_request(
                "POST", s.url + "/hang", headers=dict(MODERN_HEADERS), content=CALL
            )
            response = await client.send(request, stream=True)
            stream = response.aiter_raw()
            assert await anext(stream) == SSE_FIRST
            since = time.monotonic()
            with caplog.at_level(logging.INFO, logger="answerlineage_proxy"):
                s.lifecycle.request_stop(signal.SIGTERM)
                # La réponse reste inachevée : le client sait qu'elle est tronquée.
                with pytest.raises(httpx.RemoteProtocolError):
                    async for _ in stream:
                        pass
                await response.aclose()
                assert await s.stopped() == 0
        assert time.monotonic() - since < 5
        assert "1 appel(s) en cours, 1 s au plus pour se terminer" in caplog.text
        assert "1 appel(s) coupé(s) au-delà du délai de grâce" in caplog.text
        assert isinstance(s.sink, MemorySink)
        (event,) = s.sink.events
        assert event["eventType"] == "FAIL"
        assert event["run"]["facets"]["answerlineage_qos"]["outcome"] == "transport_error"


class RecordingSink:
    """Destination à remise différée qui note l'ordre de ce qu'on lui demande."""

    def __init__(self, starting: asyncio.Event | None = None) -> None:
        self.journal: list[str] = []
        self._starting = starting

    def watch(self, settled: Settled) -> None:
        self.journal.append("watch")

    async def start(self) -> None:
        if self._starting is not None:
            await self._starting.wait()
        self.journal.append("start")

    def emit(self, event: Mapping[str, Any]) -> None:
        self.journal.append(f"emit:{event['eventType']}")

    def report_coverage(self, report: Mapping[str, Any]) -> None:
        self.journal.append("report")

    async def aclose(self) -> None:
        self.journal.append("aclose")


async def test_the_outbox_is_flushed_once_the_calls_in_flight_are_answered() -> None:
    sink = RecordingSink()
    async with served(delay=0, grace=10, sink=sink) as s:
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            request = client.build_request(
                "POST", s.url + "/sse", headers=dict(MODERN_HEADERS), content=CALL
            )
            response = await client.send(request, stream=True)
            stream = response.aiter_raw()
            await anext(stream)
            s.lifecycle.request_stop(signal.SIGTERM)
            await asyncio.sleep(0.3)
            assert "aclose" not in sink.journal
            s.upstream.gate.set()
            b"".join([chunk async for chunk in stream])
            await response.aclose()
        assert await s.stopped() == 0
    assert sink.journal.index("emit:COMPLETE") < sink.journal.index("aclose")
    assert sink.journal[-1] == "aclose"


async def test_a_call_cut_at_shutdown_is_described_before_the_outbox_is_flushed() -> None:
    sink = RecordingSink()
    async with served(delay=0, grace=1, sink=sink) as s:
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            request = client.build_request(
                "POST", s.url + "/hang", headers=dict(MODERN_HEADERS), content=CALL
            )
            response = await client.send(request, stream=True)
            stream = response.aiter_raw()
            await anext(stream)
            s.lifecycle.request_stop(signal.SIGTERM)
            with pytest.raises(httpx.RemoteProtocolError):
                async for _ in stream:
                    pass
            await response.aclose()
        assert await s.stopped() == 0
    # L'appel coupé finit de se décrire avant que la boîte d'envoi se vide : son événement
    # part avec les autres.
    assert sink.journal.index("emit:FAIL") < sink.journal.index("aclose")
    assert s.proxy.stats.cut_at_shutdown == 1


async def test_the_proxy_is_alive_but_not_ready_while_it_starts() -> None:
    starting = asyncio.Event()
    upstream = FakeMcpServer()
    async with running(upstream) as upstream_url:
        settings = ProxySettings.create(upstream_url, "meteo", port=0, admin_port=0)
        lifecycle = Lifecycle(McpProxy(settings, RecordingSink(starting)), settings)
        task = asyncio.ensure_future(lifecycle.serve())
        await until(lambda: lifecycle.admin_address is not None)
        host, port = lifecycle.admin_address or ("", 0)
        live = await exchange("GET", f"http://{host}:{port}/livez")
        ready = await exchange("GET", f"http://{host}:{port}/readyz")
        assert live.status == 200
        assert (ready.status, ready.body) == (503, b'{"status":"starting"}')
        starting.set()
        await asyncio.wait_for(lifecycle.listening.wait(), 10)
        assert (await exchange("GET", f"http://{host}:{port}/readyz")).status == 200
        lifecycle.request_stop(signal.SIGINT)
        assert await asyncio.wait_for(task, 20) == 0


async def test_a_stop_asked_while_starting_ends_without_ever_being_ready() -> None:
    starting = asyncio.Event()
    async with running(FakeMcpServer()) as upstream_url:
        settings = ProxySettings.create(upstream_url, "meteo", port=0, admin_port=0)
        lifecycle = Lifecycle(McpProxy(settings, RecordingSink(starting)), settings)
        task = asyncio.ensure_future(lifecycle.serve())
        await until(lambda: lifecycle.admin_address is not None)
        lifecycle.request_stop(signal.SIGTERM)
        starting.set()
        assert await asyncio.wait_for(task, 20) == 0
        assert not lifecycle.listening.is_set()
        assert lifecycle.health.state == "stopped"


async def test_a_port_already_taken_stops_the_proxy_before_it_is_ready() -> None:
    async with served(admin=False) as taken:
        _, port = taken.lifecycle.address or ("", 0)
        settings = ProxySettings.create("http://127.0.0.1:9", "meteo", port=port)
        lifecycle = Lifecycle(McpProxy(settings, MemorySink()), settings)
        # Le code de sortie du serveur HTTP qui ne démarre pas.
        assert await asyncio.wait_for(lifecycle.serve(), 20) == 3
        assert lifecycle.health.state == "stopped"
        assert not lifecycle.listening.is_set()


async def test_sigterm_sent_to_the_process_starts_the_graceful_stop() -> None:
    before = signal.getsignal(signal.SIGTERM)
    async with served(delay=0) as s:
        # Le cycle de vie a pris la main sur SIGTERM : le signal ne tue pas le processus.
        assert signal.getsignal(signal.SIGTERM) is not before
        os.kill(os.getpid(), signal.SIGTERM)
        assert await s.stopped() == 0
    assert signal.getsignal(signal.SIGTERM) is before
