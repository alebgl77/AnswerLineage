"""Signaux de qualité de service : horodatages, issue d'un échange, reprises."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from answerlineage_proxy.signals import (
    ERROR_CODE_MAX,
    ERROR_CODE_MIN,
    OUTCOMES,
    SAMPLE_BYTES,
    SIGNAL_MAX,
    CallSignals,
    CallTiming,
    ReplySample,
    RetryKey,
    RetryMemory,
    assess,
    milliseconds,
)
from answerlineage_proxy.tracecontext import TraceContext, resolve_trace

JSON = "application/json"
SSE = "text/event-stream"
SECOND = 1_000_000_000


class Clock:
    """Horloge réglée à la main, en nanosecondes."""

    def __init__(self) -> None:
        self.now = 1_000

    def __call__(self) -> int:
        return self.now

    def advance(self, nanoseconds: int) -> None:
        self.now += nanoseconds


def test_the_proxy_is_charged_only_for_the_time_it_works() -> None:
    clock = Clock()
    timing = CallTiming(clock)
    clock.advance(30)  # contrôle de l'en-tête Host
    timing.pause()
    clock.advance(5_000)  # le client envoie son corps
    timing.resume()
    clock.advance(70)  # lecture du corps, préparation du relais
    timing.pause()
    timing.pause()  # une pause de trop ne compte rien
    clock.advance(900_000)  # le serveur MCP répond
    timing.resume()
    timing.resume()  # une reprise de trop non plus
    clock.advance(25)
    timing.finish()
    clock.advance(10_000)
    timing.finish()  # seule la première fin compte
    assert timing.added == 125
    assert (timing.started, timing.ended) == (1_000, 1_000 + 30 + 5_000 + 70 + 900_000 + 25)


def test_a_duration_is_given_in_milliseconds_to_the_microsecond_and_bounded() -> None:
    assert milliseconds(1_234_567) == 1.235
    assert milliseconds(0) == 0.0
    assert milliseconds(-5) == 0.0
    assert milliseconds(2 * SIGNAL_MAX * 1_000_000) == SIGNAL_MAX


def sample(media: str | None, *chunks: bytes, encoding: str | None = None) -> ReplySample:
    kept = ReplySample(media, encoding)
    for chunk in chunks:
        kept.add(chunk)
    return kept


def reply(**members: object) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": 1, **members}).encode()


def event(message: bytes, *, kind: str = "message") -> bytes:
    return b"event: " + kind.encode() + b"\ndata: " + message + b"\n\n"


ERROR = reply(error={"code": -32602, "message": "argument inconnu : canari-message-9b1f"})
RESULT = reply(result={"content": [], "isError": False})
# Entre deux membres : les données d'un événement SSE peuvent y être coupées en lignes.
SPLIT = ERROR.index(b'"id"')
PROGRESS = b'{"jsonrpc":"2.0","method":"notifications/progress","params":{"progress":1}}'


@pytest.mark.parametrize(
    ("kept", "expected"),
    [
        (sample(JSON, RESULT), (False, None)),
        (sample(JSON, ERROR), (True, -32602)),
        (sample("application/json; charset=utf-8", ERROR[:7], ERROR[7:]), (True, -32602)),
        (sample("Application/JSON", ERROR), (True, -32602)),
        # Code absent, hors de 32 bits, booléen, fractionnaire : l'erreur reste, sans code.
        (sample(JSON, reply(error={"message": "x"})), (True, None)),
        (sample(JSON, reply(error={"code": ERROR_CODE_MAX + 1})), (True, None)),
        (sample(JSON, reply(error={"code": True})), (True, None)),
        (sample(JSON, reply(error={"code": -32602.0})), (True, None)),
        (sample(JSON, reply(error="échec")), (True, None)),
        (sample(JSON, reply(error={"code": ERROR_CODE_MIN})), (True, ERROR_CODE_MIN)),
        # « error » nul : une réponse à l'ancienne, sans erreur.
        (sample(JSON, reply(result={}, error=None)), (False, None)),
        # Illisible, ambigu ou qui n'est pas un objet : rien n'est établi.
        (sample(JSON, b'{"error":{"code":1},"error":null}'), (False, None)),
        (sample(JSON, b'{"error": {"code": 1'), (False, None)),
        (sample(JSON, b"[" * 100_000), (False, None)),
        (sample(JSON, b"[1, 2]"), (False, None)),
        # Corps compressé, autre type, pas de type : la réponse n'est pas lue.
        (sample(JSON, ERROR, encoding="gzip"), (False, None)),
        (sample(JSON, ERROR, encoding="identity"), (True, -32602)),
        (sample("text/plain", ERROR), (False, None)),
        (sample(None, ERROR), (False, None)),
        (sample(JSON), (False, None)),
    ],
)
def test_a_json_reply_tells_its_json_rpc_error_and_code(
    kept: ReplySample, expected: tuple[bool, int | None]
) -> None:
    assert kept.error() == expected


def test_a_large_json_reply_is_read_by_its_start() -> None:
    padding = "x" * (2 * SAMPLE_BYTES)
    error = reply(error={"code": -32001, "message": padding})
    result = reply(result={"texte": padding})
    assert sample(JSON, error[:4096], error[4096:]).error() == (True, -32001)
    assert sample(JSON, result).error() == (False, None)
    # Un code au-delà de 32 bits est tu, même lu dans le début.
    huge = reply(error={"code": 2**40, "message": padding})
    assert sample(JSON, huge).error() == (True, None)


@pytest.mark.parametrize(
    ("stream", "expected"),
    [
        (event(PROGRESS) + event(RESULT), (False, None)),
        (event(PROGRESS) + event(ERROR), (True, -32602)),
        # Fins de ligne CRLF, données sur plusieurs lignes, champ sans espace.
        (event(ERROR).replace(b"\n", b"\r\n"), (True, -32602)),
        (b"data:" + ERROR[:SPLIT] + b"\ndata:" + ERROR[SPLIT:] + b"\n\n", (True, -32602)),
        # La dernière réponse compte ; un commentaire ou un événement sans données est sauté.
        (event(ERROR) + event(RESULT) + b": battement\n\n", (False, None)),
        (event(RESULT) + event(ERROR) + b"event: fin\n\n", (True, -32602)),
        # Un flux qui ne s'achève pas sur une ligne vide : son dernier événement n'est pas lu.
        (event(RESULT) + event(ERROR)[:-1], (False, None)),
        # Ni requête ni notification ne sont une réponse.
        (event(reply(method="sampling/createMessage", error={"code": 1})), (False, None)),
        (event(b"pas du json"), (False, None)),
    ],
)
def test_an_event_stream_tells_the_error_of_its_last_reply(
    stream: bytes, expected: tuple[bool, int | None]
) -> None:
    assert sample(SSE, stream).error() == expected
    # Morceau par morceau, comme le relais les voit.
    assert sample(SSE, *(stream[i : i + 7] for i in range(0, len(stream), 7))).error() == expected


def test_only_the_end_of_a_long_event_stream_is_kept_and_read() -> None:
    progress = [event(PROGRESS) for _ in range(5_000)]
    kept = sample(SSE, *progress, event(ERROR))
    assert kept.error() == (True, -32602)
    assert kept.size == sum(map(len, progress)) + len(event(ERROR))
    # Les morceaux gardés couvrent au plus la fenêtre et un morceau.
    assert kept._kept < SAMPLE_BYTES + len(progress[0])
    # Un seul morceau plus grand que la fenêtre : son début, tronqué, n'est pas lu.
    single = sample(SSE, b"data: " + b"x" * SAMPLE_BYTES + b"\n\n" + event(RESULT))
    assert single.error() == (False, None)
    cut = sample(SSE, event(ERROR) + b"data: " + b"y" * SAMPLE_BYTES + b"\n\n")
    assert cut.error() == (False, None)


@pytest.mark.parametrize(
    ("failure", "status", "body", "expected"),
    [
        ("client_abort", 200, ERROR, ("client_abort", None)),
        ("timeout", None, None, ("timeout", None)),
        ("transport_error", 500, None, ("transport_error", None)),
        (None, None, None, ("transport_error", None)),
        (None, 500, ERROR, ("http_error", 500)),
        (None, 404, None, ("http_error", 404)),
        (None, 200, ERROR, ("jsonrpc_error", -32602)),
        (None, 307, None, ("success", None)),
        (None, 200, RESULT, ("success", None)),
    ],
)
def test_the_outcome_of_an_exchange(
    failure: str | None, status: int | None, body: bytes | None, expected: tuple[str, int | None]
) -> None:
    kept = sample(JSON, body) if body is not None else None
    assert assess(failure=failure, status=status, sample=kept) == expected  # type: ignore[arg-type]


def test_signals_say_an_error_by_its_category_and_code_and_omit_what_is_unknown() -> None:
    success = CallSignals(latency_ms=3.2, added_latency_ms=0.1, outcome="success")
    assert success.fields() == {
        "latencyMs": 3.2,
        "addedLatencyMs": 0.1,
        "outcome": "success",
        "errorCount": 0,
    }
    failed = CallSignals(
        latency_ms=9.0, added_latency_ms=0.2, outcome="http_error", error_code=503, retry_count=0
    )
    assert failed.fields() == {
        "latencyMs": 9.0,
        "addedLatencyMs": 0.2,
        "outcome": "http_error",
        "errorCount": 1,
        "errorCode": 503,
        "retryCount": 0,
    }
    errors = {outcome for outcome in OUTCOMES if CallSignals(0, 0, outcome).error_count}  # type: ignore[arg-type]
    assert errors == {"jsonrpc_error", "http_error", "transport_error", "timeout"}


KEY = RetryKey("trace:4bf92f3577b34da6a3ce929d0e0e4736", "get_weather", "hmac-sha256:" + "a" * 64)


def calls(memory: RetryMemory, *attempts: tuple[int, int, bool]) -> Iterator[int | None]:
    """Enregistre des appels (début, fin, échec) de la même clé, en secondes."""
    for started, ended, failed in attempts:
        yield memory.record(KEY, started=started * SECOND, ended=ended * SECOND, failed=failed)


def test_a_retry_follows_a_failed_call_of_the_same_key_within_the_window() -> None:
    memory = RetryMemory()
    # Échec, reprise qui échoue, reprise qui réussit, puis un nouvel appel.
    assert list(calls(memory, (0, 1, True), (2, 3, True), (4, 5, False), (6, 7, True))) == [
        0,
        1,
        2,
        0,
    ]


def test_a_call_long_after_a_failure_is_not_a_retry() -> None:
    memory = RetryMemory()
    assert list(calls(memory, (0, 1, True), (62, 63, True), (63, 64, False))) == [0, 0, 1]


def test_a_call_that_started_before_the_failure_ended_is_not_its_retry() -> None:
    memory = RetryMemory()
    assert list(calls(memory, (0, 5, True), (2, 6, True), (7, 8, True))) == [0, 0, 1]


def test_a_success_closes_the_chain_even_when_it_does_not_follow_it() -> None:
    memory = RetryMemory()
    assert list(calls(memory, (0, 5, True), (2, 6, False), (7, 8, False))) == [0, 0, 0]
    assert len(memory) == 0


def test_another_key_is_another_chain() -> None:
    memory = RetryMemory()
    other = RetryKey(KEY.scope, KEY.tool, "hmac-sha256:" + "b" * 64)
    assert memory.record(KEY, started=0, ended=SECOND, failed=True) == 0
    assert memory.record(other, started=2 * SECOND, ended=3 * SECOND, failed=True) == 0
    assert memory.record(KEY, started=4 * SECOND, ended=5 * SECOND, failed=True) == 1


def test_without_a_key_the_number_of_retries_is_unknown_not_zero() -> None:
    memory = RetryMemory()
    assert memory.record(None, started=0, ended=1, failed=True) is None
    assert len(memory) == 0


def test_the_memory_of_retries_is_bounded_in_number_and_in_time() -> None:
    memory = RetryMemory(window_ns=10 * SECOND, capacity=3)
    for index in range(5):
        key = RetryKey("proxy", f"outil{index}", "hmac-sha256:" + "c" * 64)
        memory.record(key, started=index * SECOND, ended=index * SECOND, failed=True)
    assert len(memory) == 3
    # La plus ancienne chaîne est oubliée : sa reprise n'est plus reconnue.
    forgotten = RetryKey("proxy", "outil0", "hmac-sha256:" + "c" * 64)
    assert memory.record(forgotten, started=6 * SECOND, ended=6 * SECOND, failed=False) == 0
    # Passé la fenêtre, les chaînes échues sont retirées.
    memory.record(KEY, started=30 * SECOND, ended=30 * SECOND, failed=True)
    assert len(memory) == 1


@pytest.mark.parametrize(("window", "capacity"), [(0, 1), (SECOND, 0)])
def test_the_memory_of_retries_has_a_window_and_a_capacity(window: int, capacity: int) -> None:
    with pytest.raises(ValueError, match="positives"):
        RetryMemory(window_ns=window, capacity=capacity)


RECEIVED = TraceContext("4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7")


def test_the_scope_of_a_retry_is_the_session_then_the_received_trace_then_the_proxy() -> None:
    memory = RetryMemory()
    session = b"canari-session-1868c0ea"
    by_session = memory.scope([session], resolve_trace(RECEIVED, []))
    assert by_session.startswith("session:")
    # La session n'est gardée que sous une empreinte à clé, propre au processus.
    assert "canari" not in by_session
    assert session.hex() not in by_session
    assert by_session == memory.scope([session], None)
    assert by_session != RetryMemory().scope([session], None)
    assert memory.scope([b"autre"], None) != by_session
    # Deux en-têtes de session : aucune n'est retenue.
    two = memory.scope([session, b"autre"], resolve_trace(RECEIVED, []))
    assert two == "trace:" + RECEIVED.trace_id
    header = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    assert memory.scope([], resolve_trace(None, [header])) == (
        "trace:0af7651916cd43dd8448eb211c80319c"
    )
    # Un contexte créé par le proxy est propre à l'appel : il ne relie rien.
    assert memory.scope([], resolve_trace(None, [])) == "proxy"
    assert memory.scope([], None) == "proxy"
