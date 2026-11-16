"""Le banc de latence du proxy fonctionne, et sa mesure interne est cohérente.

Le banc complet (100 000 appels, seuil du p99 à 5 ms) tourne la nuit, sur une
machine qui ne fait que lui (``.github/workflows/bench.yml``). Ici, quelques
milliers d'appels vérifient qu'il lance le vrai proxy, qu'il mesure de ses deux
façons, et que la mesure interne tient avec celle du client : aucun seuil de
temps, que la charge d'une machine partagée ferait échouer à tort.
"""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from answerlineage_proxy.app import McpProxy
from answerlineage_proxy.config import ProxySettings
from answerlineage_proxy.events import build_tool_call_event
from answerlineage_proxy.inspection import Inspection
from answerlineage_proxy.signals import CallSignals
from answerlineage_proxy.tracecontext import resolve_trace
from scripts import bench_proxy
from scripts.bench_proxy import (
    CALL,
    HEADERS,
    RESULT,
    WARMUP_CALLS,
    BenchError,
    BenchResult,
    BenchSink,
    Distribution,
    Process,
    main,
    percentile,
    run_bench,
    serve,
    upstream_app,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_the_bench_runs_the_real_proxy_and_its_own_measure_holds() -> None:
    result = await run_bench(calls=2_000, concurrency=2, threshold_ms=math.inf)
    assert result.problems() == []
    # Un événement par appel mesuré, mise en route exclue, aucun appel en échec.
    assert (result.measured, result.failures, result.excess) == (2_000, 0, 0)
    for measure in (result.direct, result.proxied, result.internal, result.internal_latency):
        assert 0 < measure.p50 <= measure.p99 <= measure.max
    # La latence ajoutée ne dépasse pas l'échange que le proxy a vu, ni ce que le client a vu.
    assert result.internal.p50 <= result.internal_latency.p50 <= result.proxied.p50
    assert "Tenu" in result.render()


async def test_a_process_that_does_not_start_stops_the_bench() -> None:
    process = Process("proxy", "--upstream", "pas une adresse")
    with pytest.raises(BenchError, match="n'a pas démarré"):
        await process.start()
    assert await process.stop() is None
    assert await Process("upstream").stop() is None


async def test_the_test_server_and_the_proxy_are_served_like_the_command_line() -> None:
    """En ligne de commande, chacun annonce son port, puis s'arrête à la fermeture de son entrée."""
    upstream_ports: list[int] = []
    proxy_ports: list[int] = []
    stop = asyncio.Event()
    sink = BenchSink(skip=1)
    upstream = asyncio.ensure_future(serve(upstream_app, upstream_ports.append, stop.wait()))
    while not upstream_ports:
        await asyncio.sleep(0.01)
    settings = ProxySettings.create(f"http://127.0.0.1:{upstream_ports[0]}", "banc")
    proxy = asyncio.ensure_future(serve(McpProxy(settings, sink), proxy_ports.append, stop.wait()))
    while not proxy_ports:
        await asyncio.sleep(0.01)
    async with httpx.AsyncClient(trust_env=False) as client:
        for _ in range(3):
            response = await client.post(
                f"http://127.0.0.1:{proxy_ports[0]}/mcp", content=CALL, headers=HEADERS
            )
            assert response.content == RESULT
            # Le serveur HTTP du proxy n'ajoute ni Server ni Date.
            assert "server" not in response.headers
            assert "date" not in response.headers
    stop.set()
    await asyncio.gather(upstream, proxy)
    summary = sink.summary()
    assert (summary["measured"], summary["failures"], summary["excess"]) == (2, 0, 0)
    assert 0 < summary["added"]["p50"] <= summary["latency"]["p50"]


async def test_a_server_that_stops_before_serving_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def gone(self: Any, sockets: Any = None) -> None:
        return None

    monkeypatch.setattr("uvicorn.Server.serve", gone)
    ports: list[int] = []
    never: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    with pytest.raises(BenchError, match="arrêté au démarrage"):
        await serve(upstream_app, ports.append, never)
    assert ports == []


def event(outcome: str = "success", added: float = 0.2, latency: float = 1.5) -> dict[str, Any]:
    trace = resolve_trace(None, [])
    signals = CallSignals(latency_ms=latency, added_latency_ms=added, outcome=outcome)  # type: ignore[arg-type]
    return build_tool_call_event(
        run_id=trace.run_id,
        event_time=datetime.now(UTC),
        outcome="COMPLETE",
        server="banc",
        inspection=Inspection(is_tool_call=True, tool="get_weather"),
        signals=signals,
    )


def test_the_sink_keeps_only_latencies_skips_the_warm_up_and_counts_failures() -> None:
    sink = BenchSink(skip=2)
    for _ in range(2):
        sink.emit(event(added=50.0))
    sink.emit(event(added=0.1, latency=1.0))
    sink.emit(event(added=0.3, latency=2.0))
    sink.emit(event(added=3.0, latency=2.5))
    sink.emit(event("http_error"))
    sink.report_coverage({"emitter": "mcp://banc"})
    assert sink.summary() == {
        "measured": 3,
        "failures": 1,
        "added": {"p50": 0.3, "p99": 3.0, "max": 3.0},
        "latency": {"p50": 2.0, "p99": 2.5, "max": 2.5},
        "excess": 1,
    }


def test_a_percentile_is_a_measured_value_at_the_nearest_rank() -> None:
    values = [float(value) for value in range(100, 0, -1)]
    assert (percentile(values, 50), percentile(values, 99), percentile(values, 100)) == (
        50.0,
        99.0,
        100.0,
    )
    assert percentile([7.0], 99) == 7.0
    assert math.isnan(percentile([], 50))


def result(**changes: Any) -> BenchResult:
    fields: dict[str, Any] = {
        "calls": 1_000,
        "concurrency": 1,
        "threshold_ms": 5.0,
        "direct": Distribution(1.0, 2.0, 9.0),
        "proxied": Distribution(2.5, 4.0, 12.0),
        "internal": Distribution(0.2, 0.4, 2.0),
        "internal_latency": Distribution(1.5, 3.0, 10.0),
        "measured": 1_000,
        "failures": 0,
        "excess": 0,
        **changes,
    }
    return BenchResult(**fields)


def test_the_bench_holds_below_the_threshold_and_says_so() -> None:
    held = result()
    assert held.compared == Distribution(1.5, 2.0, 3.0)
    assert held.problems() == []
    text = held.render()
    assert "1000 appels, 1 à la fois" in text
    assert "Latence ajoutée, par comparaison" in text
    assert "Tenu : p99 de la latence ajoutée au plus 5 ms." in text


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"proxied": Distribution(2.5, 7.5, 12.0)}, "p99 par comparaison au-delà de 5 ms"),
        ({"internal": Distribution(0.2, 5.5, 6.0)}, "p99 de la mesure interne au-delà de 5 ms"),
        ({"measured": 990}, "990 mesures internes pour 1000 appels"),
        ({"failures": 3}, "3 appels sans succès"),
        ({"excess": 1}, "latence vue par le proxy supérieure à celle vue par le client"),
        (
            {"internal_latency": Distribution(3.0, 3.5, 10.0)},
            "latence vue par le proxy supérieure à celle vue par le client",
        ),
    ],
)
def test_the_bench_fails_on_a_threshold_or_an_incoherent_measure(
    changes: dict[str, Any], problem: str
) -> None:
    failed = result(**changes)
    assert failed.problems() == [problem]
    assert f"Échec : {problem}." in failed.render()


def test_a_tail_seen_later_by_the_proxy_than_by_the_client_is_not_incoherent() -> None:
    """Le proxy prend son dernier horodatage après avoir rendu la fin de la réponse : sur
    une machine chargée, un appel isolé peut finir plus tard pour lui que pour le client.
    La queue de la distribution peut donc dépasser ; la médiane, non."""
    assert result(internal_latency=Distribution(1.5, 4.5, 13.0)).problems() == []


@pytest.mark.parametrize(
    ("outcome", "code"), [(result(), 0), (result(failures=1), 1), (BenchError("muet"), 2)]
)
def test_the_command_line_exits_with_the_verdict(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    outcome: BenchResult | BenchError,
    code: int,
) -> None:
    received: dict[str, Any] = {}

    async def fake_run(**options: Any) -> BenchResult:
        received.update(options)
        if isinstance(outcome, BenchError):
            raise outcome
        return outcome

    monkeypatch.setattr(bench_proxy, "run_bench", fake_run)
    assert main(["--calls", "1000", "--concurrency", "2", "--threshold-ms", "4"]) == code
    assert received == {"calls": 1000, "concurrency": 2, "threshold_ms": 4.0}
    output = capsys.readouterr().out
    assert ("Banc impossible : muet." in output) == (code == 2)


def test_the_default_bench_is_a_hundred_thousand_calls_one_at_a_time() -> None:
    args = bench_proxy.build_parser().parse_args([])
    assert (args.role, args.calls, args.concurrency, args.threshold_ms) == ("run", 100_000, 1, 5.0)
    assert WARMUP_CALLS > 0


@pytest.mark.parametrize("value", ["0", "-3", "beaucoup"])
def test_a_number_of_calls_is_a_positive_integer(value: str) -> None:
    with pytest.raises(SystemExit):
        bench_proxy.build_parser().parse_args(["--calls", value])
