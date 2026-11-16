"""Le proxy en stdio relaie à l'identique et émet un événement par appel d'outil.

Le proxy tourne dans le processus du test, devant un vrai serveur MCP en stdio
(``stdio_server.py``), qu'il lance comme processus enfant. Le client est le
test : il écrit dans un tube l'entrée du proxy et lit sa sortie dans un autre.
Le serveur de test note les octets qu'il reçoit et ceux qu'il écrit, pour les
comparer à ceux du client.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import itertools
import json
import logging
import os
import signal
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from answerlineage_proxy import stdio
from answerlineage_proxy.config import ENV_FINGERPRINT_KEY, ProxyConfigurationError, SqlTool
from answerlineage_proxy.config import StdioSettings as Settings
from answerlineage_proxy.fingerprint import RESULT_LABEL, arguments_fingerprint
from answerlineage_proxy.prefix import PREFIX_BYTES
from answerlineage_proxy.sink import MemorySink
from answerlineage_proxy.stdio import (
    EXIT_NOT_EXECUTABLE,
    EXIT_NOT_FOUND,
    Direction,
    StdioProxy,
    _Backlog,
    _Copy,
    _Gap,
    _Lines,
    _Stamp,
    exit_status,
    settings_from_args,
)
from proxy.tests.conftest import (
    INITIALIZE,
    STDIO_KEY,
    stdio_call,
    stdio_message,
    stdio_notification,
    stdio_server,
    stdio_session,
    until,
    write_all,
)
from proxy.tests.stdio_server import RESULT_TEXT, SERVER_REQUEST_ID

pytestmark = pytest.mark.anyio

KEY_BYTES = bytes(range(32))
ARGUMENT_CANARY = "canari-argument-stdio-9d41"
# Vecteur publié dans spec/run-id/vectors-v1.json.
TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
RUN_ID = "a7e19836-59d9-8033-bd72-8a4bccb7271f"
SQL_TOOLS = {"execute_sql": SqlTool("query", "bigquery", "bigquery")}


def facet(event: Mapping[str, Any], name: str = "answerlineage_toolCall") -> Mapping[str, Any]:
    result: Mapping[str, Any] = event["run"]["facets"][name]
    return result


def reply_of(replies: list[bytes], identifier: object) -> bytes:
    (found,) = [line for line in replies if json.loads(line).get("id") == identifier]
    return found


def result_fingerprint(reply: bytes) -> str:
    """Empreinte du résultat : le message tel que relayé, sans sa fin de ligne."""
    digest = hmac.new(KEY_BYTES, RESULT_LABEL + b"\x00" + reply.rstrip(b"\n"), hashlib.sha256)
    return "hmac-sha256:" + digest.hexdigest()


# Le relais.


async def test_messages_pass_byte_for_byte_both_ways_and_each_tool_call_emits_one_event(
    tmp_path: Path,
) -> None:
    arguments = {"location": "Séville, ES"}
    sent = b"".join(
        [
            INITIALIZE,
            stdio_notification("notifications/initialized"),
            # Fin de ligne CRLF, blancs irréguliers : rien n'est réécrit.
            b'{ "jsonrpc" : "2.0", "id" : 1, "method" : "tools/list" }\r\n',
            stdio_call(2, "get_weather", arguments),
            stdio_call("trois", "fail", {"location": "Nulle part"}),
            stdio_message(4, "ping"),
        ]
    )
    async with stdio_session(tmp_path) as session:
        # Le client écrit en morceaux irréguliers : un message peut être coupé n'importe où.
        cuts = [0, 1, 9, 150, 151, 400, len(sent)]
        for start, end in itertools.pairwise(cuts):
            await session.send(sent[start:end])
            await asyncio.sleep(0.01)
        replies = await session.replies(5)
        code = await session.close()

    assert code == 0
    assert session.received == sent
    assert bytes(session.reader.data) == session.written
    assert len(replies) == 5

    events = session.events
    assert [facet(event)["tool"] for event in events] == ["get_weather", "fail"]
    succeeded, failed = events
    for event in events:
        assert event["eventType"] == "COMPLETE"
        assert facet(event)["transport"] == "stdio"
        assert facet(event)["server"] == "meteo"
        assert "httpStatus" not in facet(event)
        assert event["job"] == {"namespace": "mcp://meteo", "name": facet(event)["tool"]}
    reply = reply_of(replies, 2)
    assert facet(succeeded)["resultSize"] == len(reply) - 1
    assert facet(succeeded)["resultFingerprint"] == result_fingerprint(reply)
    assert facet(succeeded)["argumentsFingerprint"] == arguments_fingerprint(STDIO_KEY, arguments)
    assert facet(succeeded, "answerlineage_qos")["outcome"] == "success"
    qos = facet(failed, "answerlineage_qos")
    assert (qos["outcome"], qos["errorCode"], qos["errorCount"]) == ("jsonrpc_error", -32602, 1)
    assert facet(failed)["resultFingerprint"] == result_fingerprint(reply_of(replies, "trois"))
    for event in events:
        qos = facet(event, "answerlineage_qos")
        assert 0 <= qos["addedLatencyMs"] <= qos["latencyMs"]
        assert qos["retryCount"] == 0

    stats = session.proxy.stats
    assert (stats.tool_calls, stats.events_emitted, stats.uninspected) == (2, 2, 0)
    assert stats.relayed == 6
    assert stats.unidentified_replies == stats.unmatched_calls == 0


async def test_the_relay_never_waits_for_the_inspection(tmp_path: Path) -> None:
    """La boucle de l'inspection est figée : les messages passent quand même, puis
    l'inspection rattrape son retard et décrit chaque appel."""
    async with stdio_session(tmp_path) as session:
        await session.send(INITIALIZE)
        await session.replies(1)
        sent = b"".join(stdio_call(index, "get_weather", {"jour": index}) for index in range(1, 4))
        os.write(session.to_proxy, sent)
        # Fige la boucle du proxy, qui est celle du test : seuls les fils de relais tournent.
        deadline = time.monotonic() + 5
        while session.reader.data.count(b"\n") < 4 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert session.reader.data.count(b"\n") == 4
        assert session.sink.events == []
        await until(lambda: len(session.sink.events) == 3)
        assert await session.close() == 0
    assert session.received == INITIALIZE + sent


async def test_an_inspection_left_behind_drops_its_copies_and_counts_them(tmp_path: Path) -> None:
    """Sans place pour une seule copie, rien n'est inspecté : tout passe, tout est compté."""
    sent = INITIALIZE + stdio_call(1, "get_weather", {}) + stdio_call(2, "get_weather", {})
    async with stdio_session(tmp_path, backlog_bytes=1) as session:
        await session.send(sent)
        await session.replies(3)
        assert await session.close() == 0
    assert session.received == sent
    assert bytes(session.reader.data) == session.written
    assert session.events == []
    stats = session.proxy.stats
    assert stats.uninspected == 3
    assert stats.unidentified_replies == 3
    assert stats.lagged_bytes == len(sent) + len(session.written)


def stamp(read_at: int = 0) -> _Stamp:
    relayed = _Stamp(read_at)
    relayed.sent_at = read_at + 1
    relayed.written_at = read_at + 2
    return relayed


def gap(direction: Direction, *pieces: bytes) -> _Gap:
    dropped = _Gap(direction)
    for piece in pieces:
        dropped.add(piece)
    return dropped


@pytest.mark.parametrize(
    ("before", "dropped", "after", "lost", "read"),
    [
        # La lacune coupe un message, en achève un autre et en commence un troisième.
        (b'{"a":', [b'1}\n{"b":2}\n{"c"'], b':3}\n{"d":4}\n', 3, [b'{"d":4}']),
        # Elle s'achève sur une fin de ligne : le message suivant est lu en entier.
        (b'{"a":', [b"1}\n"], b'{"b":2}\n', 1, [b'{"b":2}']),
        # Elle tient dans un seul message : il est perdu à sa fin de ligne.
        (b'{"a":', [b"1"], b'}\n{"b":2}\n', 1, [b'{"b":2}']),
        # Plusieurs morceaux de suite se fondent en une lacune.
        (b"", [b'{"a":1}\n{"b"', b":2}\n"], b'{"c":3}\n', 2, [b'{"c":3}']),
    ],
)
def test_a_gap_loses_the_messages_it_touches_and_the_next_one_is_read(
    before: bytes, dropped: list[bytes], after: bytes, lost: int, read: list[bytes]
) -> None:
    lines = _Lines(1024, None)
    assert lines.feed(before, stamp()) == []
    counted = lines.gap(gap("client", *dropped))
    done = lines.feed(after, stamp())
    assert counted + done.count(None) == lost
    assert [line.content for line in done if line is not None] == read
    assert not lines.partial


def test_after_a_gap_the_message_is_skipped_until_its_end_of_line() -> None:
    lines = _Lines(1024, None)
    lines.feed(b'{"a":', stamp())
    assert lines.gap(gap("server", b"1")) == 0
    assert lines.feed(b"2", stamp()) == []
    assert lines.partial
    done = lines.feed(b'}\n{"b":2}\n', stamp())
    assert done[0] is None
    assert [line.content for line in done[1:] if line is not None] == [b'{"b":2}']


def test_the_work_on_a_piece_not_yet_written_is_kept_to_be_counted_later() -> None:
    """Une requête part à l'inspection avant d'être écrite : le temps du proxy sur ce
    morceau n'est connu qu'ensuite, quand l'appel est décrit."""
    lines = _Lines(1024, None)
    unwritten = _Stamp(10)
    (first,) = lines.feed(b'{"a":1}\n{"b":', unwritten)
    (second,) = lines.feed(b"2}\n", stamp(20))
    assert first is not None and second is not None
    assert (first.started, first.added, first.unsettled) == (10, 0, (unwritten,))
    assert (second.started, second.added, second.unsettled) == (10, 1, (unwritten,))


def test_a_message_longer_than_the_size_read_keeps_only_its_beginning() -> None:
    lines = _Lines(100, None)
    pieces = [b"a" * 50, b"b" * 100, b"c" * 20_000]
    for piece in pieces:
        assert lines.feed(piece, stamp()) == []
    (line,) = lines.feed(b"\n", stamp())
    assert line is not None
    assert line.content is None
    assert line.head == b"".join(pieces)[:PREFIX_BYTES]
    assert line.size == 20_150


def test_the_backlog_keeps_copies_within_its_bound_and_merges_what_it_drops() -> None:
    wakes: list[None] = []
    backlog = _Backlog(1000, lambda: wakes.append(None))
    backlog.offer("client", b"a" * 500, stamp())
    backlog.offer("client", b"b\n" * 300, stamp())
    backlog.offer("server", b"c\n", stamp())
    backlog.offer("client", b"d\n" * 10, stamp())
    items, closed = backlog.take()
    assert not closed
    assert len(wakes) == 1
    copy, client_gap, server = items
    assert isinstance(copy, _Copy) and copy.data == b"a" * 500
    assert isinstance(client_gap, _Gap) and (client_gap.newlines, client_gap.size) == (310, 620)
    # La copie du serveur ne tenait plus non plus : elle est sa propre lacune.
    assert isinstance(server, _Gap) and server.direction == "server"
    backlog.offer("client", b"e\n", stamp())
    backlog.close()
    items, closed = backlog.take()
    assert closed
    assert [item.data for item in items if isinstance(item, _Copy)] == [b"e\n"]
    assert len(wakes) == 3


# Ce que l'inspection lit, et ce qu'elle ne devine pas.


async def test_the_trace_context_of_meta_gives_the_expected_run(tmp_path: Path) -> None:
    async with stdio_session(tmp_path) as session:
        await session.send(
            stdio_call(1, "get_weather", {}, traceparent=TRACEPARENT, tracestate="a=b")
        )
        await session.send(stdio_call(2, "get_weather", {}, traceparent="ff-" + TRACEPARENT[3:]))
        await session.send(stdio_call(3, "get_weather", {}))
        await session.replies(3)
        assert await session.close() == 0
    from_meta, malformed, without = session.events
    assert from_meta["run"]["runId"] == RUN_ID
    assert facet(from_meta)["traceOrigin"] == "meta"
    assert (facet(from_meta)["traceId"], facet(from_meta)["spanId"]) == (
        TRACEPARENT[3:35],
        TRACEPARENT[36:52],
    )
    # Une valeur mal formée est ignorée : le proxy crée un contexte, pour le run seulement.
    for event in (malformed, without):
        assert facet(event)["traceOrigin"] == "proxy"
        assert event["run"]["runId"] != RUN_ID
    # Le message n'est jamais modifié, pas même pour y porter le contexte créé.
    assert b"traceparent" not in session.received.splitlines()[2]


async def test_what_cannot_be_read_without_ambiguity_passes_and_is_counted(
    tmp_path: Path,
) -> None:
    sent = b"".join(
        [
            b"[" + stdio_call(1, "get_weather", {}).rstrip(b"\n") + b"]\n",
            b'{"jsonrpc":"2.0","id":2,"method":"tools/call","method":"ping"}\n',
            b'{"jsonrpc":"2.0","id":3,"method":"tools/call"\n',
            # Une notification tools/call : aucune réponse ne la suivra.
            stdio_notification("tools/call", {"name": "get_weather", "arguments": {}}),
            b"\n",
            # Un message inachevé quand le client ferme son entrée.
            b'{"jsonrpc":"2.0","id":9,"method":"tools/call"',
        ]
    )
    async with stdio_session(tmp_path) as session:
        await session.send(sent)
        await session.replies(3)
        assert await session.close() == 0
    assert session.received == sent
    assert session.events == []
    assert session.proxy.stats.uninspected == 6
    assert sum(report["uninspected"] for report in session.sink.reports) == 6


async def test_requests_from_the_server_and_answers_of_the_client_are_not_calls(
    tmp_path: Path,
) -> None:
    async with stdio_session(tmp_path) as session:
        await session.send(stdio_call(1, "ask", {}))
        await session.replies(3)
        await session.send(
            b'{"jsonrpc":"2.0","id":"%s","result":{}}\n' % SERVER_REQUEST_ID.encode()
        )
        assert await session.close() == 0
    (event,) = session.events
    assert facet(event, "answerlineage_qos")["outcome"] == "success"
    stats = session.proxy.stats
    assert (stats.uninspected, stats.unidentified_replies, stats.unmatched_calls) == (0, 0, 0)


async def test_a_call_too_large_to_be_read_is_described_from_its_beginning(
    tmp_path: Path,
) -> None:
    big = {"location": "x" * 5000}
    sql = {"query": "SELECT a FROM s.t WHERE b = '" + "y" * 5000 + "'"}
    async with stdio_session(tmp_path, max_inspected_bytes=1024, sql_tools=SQL_TOOLS) as session:
        await session.send(stdio_call(1, "get_weather", big) + stdio_call(2, "execute_sql", sql))
        await session.replies(2)
        assert await session.close() == 0
    weather, query = session.events
    assert facet(weather)["tool"] == "get_weather"
    assert "argumentsFingerprint" not in facet(weather)
    assert facet(query)["unresolvedReason"] == "request_too_large"
    assert session.proxy.stats.inspected_from_prefix == 2


async def test_a_message_too_large_to_be_followed_is_counted_or_left_alone(
    tmp_path: Path,
) -> None:
    big = "z" * 20_000
    sent = b"".join(
        [
            # L'identifiant au-delà du début lu : la réponse ne pourra pas être rattachée.
            b'{"jsonrpc":"2.0","method":"tools/call","params":{"name":"get_weather",'
            b'"arguments":{"l":"%s"}},"id":9}\n' % big.encode(),
            # Une requête qui n'est pas un appel d'outil, trop grande : comptée, comme en HTTP.
            stdio_message(10, "resources/read", {"uri": big}),
            # La réponse du client à une requête du serveur : ni requête ni appel.
            b'{"jsonrpc":"2.0","id":"%s","result":{"l":"%s"}}\n'
            % (SERVER_REQUEST_ID.encode(), big.encode()),
        ]
    )
    async with stdio_session(tmp_path, max_inspected_bytes=1024) as session:
        await session.send(sent)
        await session.send(stdio_call(11, "ask", {"size": 5000}))
        await session.replies(4)
        assert await session.close() == 0
    assert [facet(event)["tool"] for event in session.events] == ["ask"]
    stats = session.proxy.stats
    assert (stats.uninspected, stats.inspected_from_prefix) == (2, 0)
    # La réponse au premier appel n'a pas d'appel en attente : elle est simplement relayée ;
    # la notification trop grande du serveur n'est pas une réponse.
    assert stats.unidentified_replies == 0


async def test_a_reply_too_large_to_be_read_is_followed_by_its_identifier(
    tmp_path: Path,
) -> None:
    async with stdio_session(tmp_path, max_inspected_bytes=1024) as session:
        await session.send(stdio_call(1, "huge", {"size": 300_000}))
        replies = await session.replies(1)
        assert await session.close() == 0
    (event,) = session.events
    assert event["eventType"] == "COMPLETE"
    assert facet(event)["resultSize"] == len(replies[0]) - 1
    assert facet(event)["resultFingerprint"] == result_fingerprint(replies[0])


async def test_a_reply_whose_identifier_cannot_be_read_is_never_guessed(tmp_path: Path) -> None:
    """L'identifiant est au-delà du début lu : l'appel n'a pas d'issue établie, son
    événement est perdu et compté, plutôt que d'en inventer une."""
    async with stdio_session(tmp_path, max_inspected_bytes=1024) as session:
        await session.send(stdio_call(1, "huge_late_id", {"size": 300_000}))
        await session.replies(1)
        assert await session.close() == 0
    assert session.events == []
    stats = session.proxy.stats
    assert (stats.unidentified_replies, stats.unmatched_calls) == (1, 1)
    reports = session.sink.reports
    assert sum(report["calls_seen"] for report in reports) == 1
    assert sum(report["events_lost"] for report in reports) == 1


async def test_an_identifier_used_twice_before_its_reply_describes_neither_call(
    tmp_path: Path,
) -> None:
    async with stdio_session(tmp_path) as session:
        await session.send(stdio_call(5, "wait_eof", {}) + stdio_call(5, "wait_eof", {}))
        await until(lambda: session.received.count(b"\n") == 2)
        assert await session.close() == 0
    assert session.events == []
    stats = session.proxy.stats
    assert (stats.unmatched_calls, stats.uninspected) == (1, 1)


async def test_both_replies_to_an_identifier_used_twice_are_relayed_without_an_event(
    tmp_path: Path,
) -> None:
    async with stdio_session(tmp_path) as session:
        await session.send(stdio_call(5, "get_weather", {}) + stdio_call(5, "get_weather", {}))
        await session.replies(2)
        # Une requête qui n'est pas un appel d'outil reprend aussi l'identifiant en attente.
        await session.send(stdio_call(6, "wait_eof", {}) + stdio_message(6, "resources/list"))
        await until(lambda: session.received.count(b"\n") == 4)
        assert await session.close() == 0
    assert session.events == []
    stats = session.proxy.stats
    assert (stats.unmatched_calls, stats.uninspected) == (2, 1)


async def test_a_cancellation_names_only_a_request_still_pending(tmp_path: Path) -> None:
    cancelled = "notifications/cancelled"
    async with stdio_session(tmp_path) as session:
        await session.send(stdio_message(8, "resources/list"))
        for request in (None, "inconnu", 8):
            await session.send(stdio_notification(cancelled, {"requestId": request}))
        await session.send(stdio_notification(cancelled))
        await until(lambda: session.received.count(b"\n") == 5)
        assert await session.close() == 0
    assert session.events == []
    assert session.proxy.stats.uninspected == 0


async def test_the_memory_of_pending_requests_is_bounded(tmp_path: Path) -> None:
    async with stdio_session(tmp_path, pending_requests=2) as session:
        # Une requête sans réponse, qui n'est pas un appel d'outil, puis trois appels.
        await session.send(stdio_message(9, "resources/list"))
        await session.send(b"".join(stdio_call(index, "wait_eof", {}) for index in range(3)))
        await until(lambda: session.received.count(b"\n") == 4)
        await until(lambda: session.proxy.stats.unmatched_calls == 1)
        assert await session.close() == 0
    # Les plus anciennes sont oubliées ; les deux autres s'achèvent à la fermeture du client.
    assert [facet(event, "answerlineage_qos")["outcome"] for event in session.events] == [
        "client_abort",
        "client_abort",
    ]


async def test_a_call_with_sql_is_analysed_after_its_reply(tmp_path: Path) -> None:
    query = {"query": "SELECT c.nom FROM crm.clients c WHERE c.ville = 'Lyon'"}
    async with stdio_session(tmp_path, sql_tools=SQL_TOOLS) as session:
        await session.send(stdio_call(1, "execute_sql", query))
        await session.replies(1)
        assert await session.close() == 0
    (event,) = session.events
    assert [dataset["name"] for dataset in event["inputs"]] == ["crm.clients"]
    assert event["job"]["facets"]["sql"]["query"] == (
        "SELECT c.nom FROM crm.clients AS c WHERE c.ville = ?"
    )


# La fin d'un appel, d'un côté ou de l'autre.


async def test_a_server_that_dies_during_a_call_fails_it_and_gives_its_exit_code(
    tmp_path: Path,
) -> None:
    async with stdio_session(tmp_path) as session:
        await session.send(stdio_call(1, "die", {"code": 3}))
        code = await session.finished()
        # La sortie du proxy s'est fermée avec celle du serveur.
        assert not session.reader.is_alive()
    assert code == 3
    (event,) = session.events
    assert event["eventType"] == "FAIL"
    qos = facet(event, "answerlineage_qos")
    assert (qos["outcome"], qos["errorCount"]) == ("transport_error", 1)
    assert "resultSize" not in facet(event)


async def test_a_client_that_closes_its_input_abandons_its_pending_calls(tmp_path: Path) -> None:
    async with stdio_session(tmp_path, "--exit-code", "4") as session:
        await session.send(stdio_call(1, "wait_eof", {}))
        await until(lambda: session.received.count(b"\n") == 1)
        assert await session.close() == 4
    (event,) = session.events
    assert event["eventType"] == "ABORT"
    qos = facet(event, "answerlineage_qos")
    assert (qos["outcome"], qos["errorCount"]) == ("client_abort", 0)


async def test_a_cancelled_call_is_abandoned_when_the_client_cancels_it(tmp_path: Path) -> None:
    async with stdio_session(tmp_path) as session:
        await session.send(stdio_call("long", "wait_eof", {}))
        await session.send(stdio_notification("notifications/cancelled", {"requestId": "long"}))
        await until(lambda: len(session.events) == 1)
        (event,) = session.events
        assert event["eventType"] == "ABORT"
        assert facet(event, "answerlineage_qos")["outcome"] == "client_abort"
        assert await session.close() == 0
    assert len(session.events) == 1


async def test_a_client_that_stops_reading_ends_the_relay() -> None:
    """Personne ne lit plus la sortie du proxy : le relais s'achève, l'appel est dit
    abandonné par le client, et le proxy rend le code de l'enfant.

    Selon le moment où le proxy voit le client partir, l'enfant reçoit la requête et
    voit le tube rompu à sa prochaine écriture, ou ne la reçoit pas et voit la fin de
    son entrée : le serveur de test s'arrête de la même façon dans les deux cas.
    """
    client_input, to_proxy = os.pipe()
    from_proxy, client_output = os.pipe()
    os.close(from_proxy)
    sink = MemorySink()
    settings = Settings.create(stdio_server("--exit-code", "7"), "meteo", fingerprint_key=STDIO_KEY)
    proxy = StdioProxy(
        settings, sink, input_fd=client_input, output_fd=client_output, forwarded_signals=()
    )
    task = asyncio.ensure_future(proxy.run())
    try:
        write_all(to_proxy, stdio_call(1, "huge", {"size": 1_000_000}))
        os.close(to_proxy)
        code = await asyncio.wait_for(task, timeout=20)
    finally:
        for fd in (client_input, client_output):
            os.close(fd)
    assert code == 7
    (event,) = sink.events
    assert event["eventType"] == "ABORT"
    assert facet(event, "answerlineage_qos")["outcome"] == "client_abort"


async def test_a_forwarded_signal_stops_the_server_and_gives_its_code(tmp_path: Path) -> None:
    async with stdio_session(tmp_path, forwarded_signals=(signal.SIGUSR1,)) as session:
        await session.send(INITIALIZE)
        await session.replies(1)
        os.kill(os.getpid(), signal.SIGUSR1)
        code = await session.finished()
    assert code == 128 + signal.SIGUSR1
    assert signal.getsignal(signal.SIGUSR1) == signal.SIG_DFL


async def test_an_interrupted_proxy_leaves_no_server_behind(tmp_path: Path) -> None:
    async with stdio_session(tmp_path) as session:
        await session.send(stdio_call(1, "whoami", {}))
        (reply,) = await session.replies(1)
        pid = json.loads(reply)["result"]["structuredContent"]["pid"]
        session.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await session.task
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_a_proxy_that_cannot_start_relaying_stops_its_server(tmp_path: Path) -> None:
    # SIGKILL ne s'intercepte pas : le relais ne commence pas, le serveur est arrêté.
    async with stdio_session(tmp_path, forwarded_signals=(signal.SIGKILL,)) as session:
        with pytest.raises(RuntimeError):
            await session.finished()
    assert session.received == b""
    assert session.reader.data == b""


async def test_a_descendant_that_keeps_the_output_open_does_not_hold_the_proxy(
    tmp_path: Path,
) -> None:
    async with stdio_session(tmp_path) as session:
        await session.send(stdio_call(1, "detach", {}))
        (reply,) = await session.replies(1)
        holder = json.loads(reply)["result"]["structuredContent"]["pid"]
        try:
            await session.send(stdio_call(2, "die", {"code": 0}))
            assert await session.finished() == 0
        finally:
            os.kill(holder, signal.SIGKILL)
    assert [event["eventType"] for event in session.events] == ["COMPLETE", "FAIL"]


async def test_a_server_that_closes_its_input_closes_the_input_of_the_proxy(
    tmp_path: Path,
) -> None:
    async with stdio_session(tmp_path) as session:
        await session.send(stdio_call(1, "close_input", {"delay": 1}))
        await session.replies(1)
        await session.send(stdio_call(2, "get_weather", {}))

        def refused() -> bool:
            try:
                os.write(session.to_proxy, b"\n")
            except BrokenPipeError:
                return True
            return False

        # Plus personne ne lit l'entrée du proxy : le client le voit à son tour.
        await until(refused)
        assert await session.finished() == 0
    first, second = session.events
    assert first["eventType"] == "COMPLETE"
    assert (second["eventType"], facet(second, "answerlineage_qos")["outcome"]) == (
        "FAIL",
        "transport_error",
    )


@pytest.mark.parametrize(
    ("broken", "logged"),
    [("server", "Relais vers le serveur MCP interrompu"), ("client", "Relais vers le client")],
)
async def test_a_relay_that_breaks_is_logged_and_ends_its_direction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    broken: str,
    logged: str,
) -> None:
    original = stdio._write_all
    async with stdio_session(tmp_path) as session:

        def write(fd: int, data: bytes) -> bool:
            if (fd == session.proxy._output) == (broken == "client"):
                raise RuntimeError("canari-panne")
            return original(fd, data)

        monkeypatch.setattr(stdio, "_write_all", write)
        with caplog.at_level(logging.ERROR, logger="answerlineage_proxy"):
            await session.send(stdio_call(1, "get_weather", {}))
            await until(lambda: logged in caplog.text)
            assert await session.close() == 0
    assert "(RuntimeError)" in caplog.text
    assert "canari" not in caplog.text
    (event,) = session.events
    assert facet(event, "answerlineage_qos")["outcome"] == "transport_error"


@pytest.mark.parametrize(
    ("method", "logged"),
    [("_consume", "Copie non inspectée"), ("_on_request", "Message non inspecté")],
)
async def test_an_inspection_that_fails_never_reaches_the_relay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    method: str,
    logged: str,
) -> None:
    def broken(*args: object) -> None:
        raise RuntimeError("canari-panne")

    monkeypatch.setattr(StdioProxy, method, broken)
    with caplog.at_level(logging.ERROR, logger="answerlineage_proxy"):
        async with stdio_session(tmp_path) as session:
            await session.send(stdio_call(1, "get_weather", {}))
            await session.replies(1)
            assert await session.close() == 0
    assert session.events == []
    assert f"{logged} (RuntimeError)" in caplog.text
    assert "canari" not in caplog.text


def test_a_message_after_a_gap_is_described_and_the_one_it_cut_is_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sink = MemorySink()
    proxy = StdioProxy(Settings.create(["serveur"], "meteo"), sink, forwarded_signals=())
    with caplog.at_level(logging.WARNING, logger="answerlineage_proxy"):
        proxy._consume(gap("client", b'{"jsonrpc":"2.0","id":0,"method":"ping"'))
        proxy._consume(gap("client", b"x"))
        proxy._consume(_Copy("client", b"}\n" + stdio_call(1, "get_weather", {}), stamp()))
    assert proxy.stats.uninspected == 1
    assert proxy.stats.lagged_bytes == 40
    assert caplog.text.count("Inspection en retard") == 1
    proxy._settle_pending()
    (event,) = sink.events
    assert facet(event, "answerlineage_qos")["outcome"] == "transport_error"


async def test_an_input_that_cannot_be_read_is_read_as_closed() -> None:
    from_proxy, client_output = os.pipe()
    settings = Settings.create(stdio_server("--exit-code", "8"), "meteo")
    proxy = StdioProxy(
        settings, MemorySink(), input_fd=-1, output_fd=client_output, forwarded_signals=()
    )
    try:
        assert await asyncio.wait_for(proxy.run(), timeout=20) == 8
    finally:
        os.close(from_proxy)
        os.close(client_output)


def test_the_exit_status_is_the_code_of_the_server_or_128_plus_its_signal() -> None:
    assert (exit_status(0), exit_status(3), exit_status(-15)) == (0, 3, 143)


@pytest.mark.parametrize(
    ("command", "code", "text"),
    [
        (["/rien/canari-commande-introuvable"], EXIT_NOT_FOUND, "commande introuvable"),
        ([str(Path(__file__).with_name("conftest.py"))], EXIT_NOT_EXECUTABLE, "non exécutable"),
        (["canari\0commande"], EXIT_NOT_EXECUTABLE, "non lancé (ValueError)"),
    ],
)
async def test_a_command_that_does_not_start_gives_a_fixed_error(
    command: list[str], code: int, text: str, caplog: pytest.LogCaptureFixture
) -> None:
    sink = MemorySink()
    proxy = StdioProxy(Settings.create(command, "meteo"), sink, forwarded_signals=())
    with caplog.at_level(logging.DEBUG):
        assert await proxy.run() == code
    assert text in caplog.text
    assert "canari" not in caplog.text
    assert sink.events == sink.reports == []


# Ce qui ne sort pas du proxy.


async def test_marked_arguments_and_results_never_leave_the_proxy(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        async with stdio_session(tmp_path) as session:
            await session.send(stdio_call(1, "get_weather", {"location": ARGUMENT_CANARY}))
            await session.send(stdio_call(2, "fail", {"location": ARGUMENT_CANARY}))
            replies = await session.replies(2)
            assert await session.close() == 0
    # Les canaris ont traversé le proxy, dans les deux sens.
    assert ARGUMENT_CANARY.encode() in session.received
    assert RESULT_TEXT.encode() in replies[0]
    assert ARGUMENT_CANARY.encode() in replies[1]
    # Ils ne sont ni dans les événements, ni dans les relevés, ni dans le journal.
    captured = json.dumps([*session.events, *session.sink.reports]) + caplog.text
    for canary in (ARGUMENT_CANARY, RESULT_TEXT, "Lieu inconnu"):
        assert canary not in captured
    assert len(session.events) == 2


async def test_the_server_gets_the_environment_except_the_fingerprint_key(tmp_path: Path) -> None:
    environment = {**os.environ, ENV_FINGERPRINT_KEY: "ab" * 32, "ANSWERLINEAGE_ESSAI": "1"}
    names = [ENV_FINGERPRINT_KEY, "ANSWERLINEAGE_ESSAI"]
    async with stdio_session(tmp_path, environment=environment) as session:
        await session.send(stdio_call(1, "environment", {"names": names}))
        (reply,) = await session.replies(1)
        assert await session.close() == 0
    present = json.loads(reply)["result"]["structuredContent"]
    assert present == {ENV_FINGERPRINT_KEY: False, "ANSWERLINEAGE_ESSAI": True}


# Les réglages.


def test_the_command_follows_the_double_dash_and_names_the_server_by_default() -> None:
    settings, window = settings_from_args(
        ["--max-inspected-bytes", "2048", "--", "/opt/serveurs/mcp-meteo", "--", "-v"], {}
    )
    assert settings.command == ("/opt/serveurs/mcp-meteo", "--", "-v")
    assert settings.server_name == "mcp-meteo"
    assert settings.max_inspected_bytes == 2048
    assert window.total_seconds() == 60
    named, _ = settings_from_args(["--server-name", "meteo", "--", "uvx", "serveur"], {})
    assert named.server_name == "meteo"
    from_environment, _ = settings_from_args(
        ["--", "npx"], {"ANSWERLINEAGE_PROXY_SERVER_NAME": "meteo"}
    )
    assert from_environment.server_name == "meteo"
    # La commande n'apparaît pas dans la représentation des réglages.
    assert "/opt/serveurs" not in repr(settings)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--upstream", "http://127.0.0.1:9000", "--", "serveur"], "--upstream"),
        (["--port", "9000", "--", "serveur"], "--port"),
        (["--allow-host", "proxy.interne", "--", "serveur"], "--allow-host"),
        (["--no-preserve-host", "--", "serveur"], "--no-preserve-host"),
        (["--"], "Indiquer la commande"),
        (["--", "/canari/serveur meteo"], "tiré du nom du programme lancé"),
        (["--sql-budget-ms", "0", "--", "serveur"], "sql_budget_ms"),
    ],
)
def test_a_setting_without_sense_in_stdio_mode_is_refused(
    arguments: list[str], message: str
) -> None:
    with pytest.raises(ProxyConfigurationError, match=message) as refused:
        settings_from_args(arguments, {})
    assert "canari" not in str(refused.value)
