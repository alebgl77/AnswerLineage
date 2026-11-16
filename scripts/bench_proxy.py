"""Banc de latence du proxy MCP : ce qu'il ajoute à un appel d'outil.

Le banc lance un serveur MCP de test et le vrai proxy devant lui, chacun dans
son processus, servis comme la ligne de commande du proxy les sert. Le proxy
remet ses événements à sa boîte d'envoi, vers une API d'ingestion injoignable :
la boîte garde, abandonne et reprend, sans qu'aucun appel ne l'attende. Il appelle
ensuite l'outil un nombre réglable de fois directement et autant de fois à
travers le proxy, en alternance, et mesure la latence ajoutée de deux façons :

- par comparaison : chaque percentile des appels à travers le proxy, moins le
  même percentile des appels directs, vus du client ;
- par la mesure interne du proxy : la latence ajoutée (``addedLatencyMs``) de
  l'événement de chaque appel, ce que le proxy dépense avant de relayer la
  requête et avant de rendre chaque partie de la réponse.

Il échoue (code 1) quand le p99 de l'une ou l'autre dépasse le seuil, ou quand
la mesure interne n'est pas cohérente : un événement par appel, chacun réussi,
une latence ajoutée qui ne dépasse ni la latence vue par le proxy ni celle vue
par le client.

Usage : ``python -m scripts.bench_proxy [--calls 100000] [--concurrency 1] [--threshold-ms 5]``
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import secrets
import socket
import sys
import time
from array import array
from collections.abc import Awaitable, Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx
import uvicorn

from answerlineage_proxy.app import McpProxy
from answerlineage_proxy.cli import SERVER_OPTIONS
from answerlineage_proxy.config import ProxySettings
from answerlineage_proxy.events import QOS_FACET
from answerlineage_proxy.fingerprint import FingerprintKey
from answerlineage_proxy.outbox import Outbox, OutboxSettings

ROOT: Final = Path(__file__).resolve().parents[1]
DEFAULT_CALLS: Final = 100_000
DEFAULT_THRESHOLD_MS: Final = 5.0
# Appels de mise en route, non mesurés côté client : connexions ouvertes, caches chauds.
WARMUP_CALLS: Final = 50
# Délai accordé à un processus du banc pour démarrer ou s'arrêter.
_PROCESS_TIMEOUT: Final = 60.0

TOOL: Final = "get_weather"
CALL: Final = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": TOOL, "arguments": {"location": "Lyon"}},
    }
).encode()
RESULT: Final = (
    b'{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text",'
    b'"text":"Ciel clair, 14 degres"}],"isError":false}}'
)
HEADERS: Final = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
    "mcp-protocol-version": "2026-07-28",
    "mcp-method": "tools/call",
    "mcp-name": TOOL,
}

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]


class BenchError(RuntimeError):
    """Le banc n'a pas pu mesurer : processus muet, appel en échec."""


async def upstream_app(scope: Scope, receive: Receive, send: Send) -> None:
    """Serveur MCP de test : lit la requête et rend un résultat fixe, sans attendre."""
    if scope["type"] == "lifespan":
        while (await receive())["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        await send({"type": "lifespan.shutdown.complete"})
        return
    while (await receive()).get("more_body", False):
        pass
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(RESULT)).encode("ascii")),
    ]
    await send({"type": "http.response.start", "status": 200, "headers": headers})
    await send({"type": "http.response.body", "body": RESULT})


class BenchSink:
    """Destination des événements du proxy au banc : n'en garde que les latences.

    Les ``skip`` premiers événements, ceux de la mise en route, ne sont pas
    mesurés : la mesure interne porte sur les mêmes appels que celle du client.
    """

    def __init__(self, skip: int = 0) -> None:
        self.added = array("d")
        self.latency = array("d")
        self.failures = 0
        self._skip = skip

    def emit(self, event: Mapping[str, Any]) -> None:
        if self._skip > 0:
            self._skip -= 1
            return
        signals = event["run"]["facets"].get(QOS_FACET, {})
        if signals.get("outcome") != "success":
            self.failures += 1
            return
        self.added.append(signals["addedLatencyMs"])
        self.latency.append(signals["latencyMs"])

    def report_coverage(self, report: Mapping[str, Any]) -> None:
        """Les relevés de couverture ne sont pas mesurés."""

    def summary(self) -> dict[str, Any]:
        return {
            "measured": len(self.added),
            "failures": self.failures,
            "added": Distribution.of(self.added).as_dict(),
            "latency": Distribution.of(self.latency).as_dict(),
            "excess": sum(
                1
                for added, latency in zip(self.added, self.latency, strict=True)
                if added > latency
            ),
        }


def percentile(values: Sequence[float], rank: float) -> float:
    """Percentile au rang le plus proche : une valeur mesurée, jamais une interpolation."""
    if not values:
        return math.nan
    ordered = sorted(values)
    return ordered[max(0, math.ceil(rank / 100 * len(ordered)) - 1)]


@dataclass(frozen=True)
class Distribution:
    """p50, p99 et maximum d'une série de latences, en millisecondes."""

    p50: float
    p99: float
    max: float

    @classmethod
    def of(cls, values: Sequence[float]) -> Distribution:
        return cls(percentile(values, 50), percentile(values, 99), percentile(values, 100))

    @classmethod
    def from_dict(cls, values: Mapping[str, float]) -> Distribution:
        return cls(values["p50"], values["p99"], values["max"])

    def as_dict(self) -> dict[str, float]:
        return {"p50": self.p50, "p99": self.p99, "max": self.max}

    def minus(self, other: Distribution) -> Distribution:
        return Distribution(self.p50 - other.p50, self.p99 - other.p99, self.max - other.max)


@dataclass(frozen=True)
class BenchResult:
    """Ce que le banc a mesuré, et son verdict."""

    calls: int
    concurrency: int
    threshold_ms: float
    direct: Distribution
    proxied: Distribution
    internal: Distribution
    internal_latency: Distribution
    # Événements mesurés par le proxy, hors mise en route ; appels sans succès ; mesures
    # internes dont la latence ajoutée dépasse la latence de l'échange.
    measured: int
    failures: int
    excess: int

    @property
    def compared(self) -> Distribution:
        return self.proxied.minus(self.direct)

    def problems(self) -> list[str]:
        """Ce qui fait échouer le banc : mesure interne incohérente, ou seuil dépassé.

        Le proxy voit chaque échange à peu près dans l'intervalle où le client le
        voit, mais il prend son dernier horodatage après avoir rendu la fin de la
        réponse : sur une machine chargée, un appel isolé peut finir plus tard pour
        lui que pour le client. La queue de la distribution peut donc dépasser ;
        la médiane de la latence vue par le proxy, sur les mêmes appels, ne dépasse
        pas celle du client sans incohérence réelle.
        """
        problems = []
        if self.measured != self.calls:
            problems.append(f"{self.measured} mesures internes pour {self.calls} appels")
        if self.failures:
            problems.append(f"{self.failures} appels sans succès")
        if self.excess or self.internal_latency.p50 > self.proxied.p50:
            problems.append("latence vue par le proxy supérieure à celle vue par le client")
        if self.compared.p99 > self.threshold_ms:
            problems.append(f"p99 par comparaison au-delà de {self.threshold_ms:g} ms")
        if self.internal.p99 > self.threshold_ms:
            problems.append(f"p99 de la mesure interne au-delà de {self.threshold_ms:g} ms")
        return problems

    def render(self) -> str:
        """Le tableau des mesures et le verdict. La différence de deux maxima, deux valeurs
        isolées, n'a pas de sens : elle n'est pas affichée."""
        compared = self.compared
        rows = [
            ("Appel direct, vu du client", self.direct.as_dict()),
            ("À travers le proxy, vu du client", self.proxied.as_dict()),
            ("Échange vu par le proxy", self.internal_latency.as_dict()),
            ("Latence ajoutée, par comparaison", {"p50": compared.p50, "p99": compared.p99}),
            ("Latence ajoutée, mesure interne", self.internal.as_dict()),
        ]
        lines = [
            f"Banc du proxy MCP : {self.calls} appels, {self.concurrency} à la fois.",
            f"{'':36}{'p50':>10}{'p99':>10}{'max':>10}",
            *(
                f"{label:36}"
                + "".join(
                    f"{row[stat]:10.3f}" if stat in row else f"{'—':>10}"
                    for stat in ("p50", "p99", "max")
                )
                + "  ms"
                for label, row in rows
            ),
        ]
        problems = self.problems()
        if problems:
            lines.append("Échec : " + " ; ".join(problems) + ".")
        else:
            lines.append(f"Tenu : p99 de la latence ajoutée au plus {self.threshold_ms:g} ms.")
        return "\n".join(lines)


async def serve(app: Any, ready: Callable[[int], None], stop: Awaitable[Any]) -> None:
    """Sert ``app`` sur un port libre de la boucle locale, comme la ligne de commande du proxy,
    annonce ce port, puis s'arrête quand ``stop`` aboutit."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, **SERVER_OPTIONS))
    serving = asyncio.ensure_future(server.serve())
    while not server.started:
        if serving.done():
            serving.result()
            raise BenchError("le serveur s'est arrêté au démarrage")
        await asyncio.sleep(0.01)
    ready(server.servers[0].sockets[0].getsockname()[1])
    await stop
    server.should_exit = True
    await serving


def _announce(port: int) -> None:
    print(json.dumps({"port": port}), flush=True)


async def _input_closed() -> None:
    """Le banc ferme l'entrée standard d'un processus pour l'arrêter."""
    await asyncio.to_thread(sys.stdin.buffer.read)


async def serve_upstream() -> None:
    await serve(upstream_app, _announce, _input_closed())


class MeasuredOutbox(Outbox):
    """La boîte d'envoi du proxy, telle qu'en service ; le banc garde au passage la mesure
    interne de chaque événement."""

    def __init__(self, settings: OutboxSettings, measure: BenchSink) -> None:
        super().__init__(settings)
        self.measure = measure

    def emit(self, event: Mapping[str, Any]) -> None:
        self.measure.emit(event)
        super().emit(event)


async def serve_proxy(upstream: str) -> None:
    """Le vrai proxy, avec une clé d'empreinte et sa boîte d'envoi : il fait tout ce qu'il
    fait en service, l'API d'ingestion en panne."""
    sink = BenchSink(skip=WARMUP_CALLS)
    key = FingerprintKey(secrets.token_bytes(32))
    settings = ProxySettings.create(upstream, "banc", fingerprint_key=key)
    with socket.socket() as unreachable:
        # Réservé sans écouter : chaque connexion de la boîte à l'API y est refusée.
        unreachable.bind(("127.0.0.1", 0))
        api = f"http://127.0.0.1:{unreachable.getsockname()[1]}"
        box = MeasuredOutbox(OutboxSettings.create(api, flush_seconds=0), sink)
        await serve(McpProxy(settings, box), _announce, _input_closed())
    print(json.dumps(sink.summary()), flush=True)


class Process:
    """Un processus du banc : il annonce son port, puis rend son bilan quand on l'arrête."""

    def __init__(self, *arguments: str) -> None:
        self._arguments = arguments
        self._process: asyncio.subprocess.Process | None = None

    async def start(self) -> int:
        self._process = process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "scripts.bench_proxy",
            *self._arguments,
            cwd=ROOT,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        line = b""
        if process.stdout is not None:
            line = await asyncio.wait_for(process.stdout.readline(), _PROCESS_TIMEOUT)
        if not line:
            await process.wait()
            raise BenchError(f"le processus « {self._arguments[0]} » n'a pas démarré")
        port: int = json.loads(line)["port"]
        return port

    async def stop(self) -> dict[str, Any] | None:
        """Ferme son entrée et attend sa fin ; rend sa dernière ligne écrite, s'il y en a une."""
        process = self._process
        if process is None or process.returncode is not None:
            return None
        if process.stdin is not None:
            process.stdin.close()
        try:
            output, _ = await asyncio.wait_for(process.communicate(), _PROCESS_TIMEOUT)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise BenchError(f"le processus « {self._arguments[0]} » ne s'est pas arrêté") from None
        lines = output.splitlines()
        report: dict[str, Any] | None = json.loads(lines[-1]) if lines else None
        return report


async def _call(client: httpx.AsyncClient, url: str) -> float:
    """Un appel d'outil ; sa latence vue du client, en millisecondes."""
    started = time.perf_counter_ns()
    response = await client.post(url, content=CALL, headers=HEADERS)
    elapsed = time.perf_counter_ns() - started
    if response.status_code != 200 or response.content != RESULT:
        raise BenchError(f"appel en échec, statut {response.status_code}")
    return elapsed / 1_000_000


async def load(
    direct_url: str, proxied_url: str, *, calls: int, concurrency: int
) -> tuple[array[float], array[float]]:
    """Appelle l'outil ``calls`` fois de chaque façon, en alternant l'ordre d'un appel à l'autre."""
    direct, proxied = array("d"), array("d")
    limits = httpx.Limits(
        max_connections=2 * concurrency, max_keepalive_connections=2 * concurrency
    )
    async with httpx.AsyncClient(trust_env=False, timeout=30, limits=limits) as client:
        client.headers.clear()
        for _ in range(WARMUP_CALLS):
            await _call(client, direct_url)
            await _call(client, proxied_url)
        remaining = [calls]

        async def worker() -> None:
            while remaining[0] > 0:
                remaining[0] -= 1
                if remaining[0] % 2:
                    direct.append(await _call(client, direct_url))
                    proxied.append(await _call(client, proxied_url))
                else:
                    proxied.append(await _call(client, proxied_url))
                    direct.append(await _call(client, direct_url))

        await asyncio.gather(*(worker() for _ in range(concurrency)))
    return direct, proxied


async def run_bench(
    *,
    calls: int = DEFAULT_CALLS,
    concurrency: int = 1,
    threshold_ms: float = DEFAULT_THRESHOLD_MS,
) -> BenchResult:
    """Lance le serveur MCP de test et le proxy, mesure, puis arrête les deux."""
    upstream, proxy = Process("upstream"), None
    try:
        upstream_url = f"http://127.0.0.1:{await upstream.start()}"
        proxy = Process("proxy", "--upstream", upstream_url)
        proxy_url = f"http://127.0.0.1:{await proxy.start()}"
        direct, proxied = await load(
            upstream_url + "/mcp", proxy_url + "/mcp", calls=calls, concurrency=concurrency
        )
        internal = await proxy.stop()
    finally:
        if proxy is not None:
            await proxy.stop()
        await upstream.stop()
    if internal is None:
        raise BenchError("le proxy n'a rendu aucune mesure interne")
    return BenchResult(
        calls=calls,
        concurrency=concurrency,
        threshold_ms=threshold_ms,
        direct=Distribution.of(direct),
        proxied=Distribution.of(proxied),
        internal=Distribution.from_dict(internal["added"]),
        internal_latency=Distribution.from_dict(internal["latency"]),
        measured=internal["measured"],
        failures=internal["failures"],
        excess=internal["excess"],
    )


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("un entier positif est attendu")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.bench_proxy",
        description="Mesure la latence que le proxy MCP ajoute à un appel d'outil.",
    )
    parser.add_argument(
        "role",
        nargs="?",
        default="run",
        choices=["run", "upstream", "proxy"],
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--upstream", help=argparse.SUPPRESS)
    parser.add_argument(
        "--calls",
        type=_positive,
        default=DEFAULT_CALLS,
        help=f"appels mesurés de chaque façon, {DEFAULT_CALLS} par défaut",
    )
    parser.add_argument(
        "--concurrency", type=_positive, default=1, help="appels menés à la fois, 1 par défaut"
    )
    parser.add_argument(
        "--threshold-ms",
        type=float,
        default=DEFAULT_THRESHOLD_MS,
        help=f"seuil du p99 de la latence ajoutée, {DEFAULT_THRESHOLD_MS:g} ms par défaut",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.role == "upstream":
        asyncio.run(serve_upstream())
        return 0
    if args.role == "proxy":
        asyncio.run(serve_proxy(args.upstream))
        return 0
    try:
        result = asyncio.run(
            run_bench(
                calls=args.calls, concurrency=args.concurrency, threshold_ms=args.threshold_ms
            )
        )
    except BenchError as error:
        print(f"Banc impossible : {error}.")
        return 2
    print(result.render())
    return 1 if result.problems() else 0


if __name__ == "__main__":
    sys.exit(main())
