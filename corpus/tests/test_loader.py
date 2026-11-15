"""L'envoi des événements à l'API d'ingestion, lot par lot."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

import pytest

from answerlineage_corpus.loader import LoadError, LoadResult, batch_url, send


class FakeApi(BaseHTTPRequestHandler):
    """API d'ingestion de substitution : retient les lots reçus."""

    batches: ClassVar[list[dict[str, Any]]] = []
    status: ClassVar[int] = 202
    body: ClassVar[bytes | None] = None

    def do_POST(self) -> None:
        length = int(self.headers["content-length"])
        events = json.loads(self.rfile.read(length))
        type(self).batches.append(
            {"path": self.path, "type": self.headers["content-type"], "events": events}
        )
        report = {"accepted": len(events), "duplicates": 1, "rejected": []}
        payload = self.body if self.body is not None else json.dumps(report).encode()
        self.send_response(self.status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        """Rien n'est écrit sur la sortie des tests."""


@pytest.fixture
def api() -> Iterator[str]:
    FakeApi.batches = []
    FakeApi.status = 202
    FakeApi.body = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeApi)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def events(count: int) -> list[dict[str, Any]]:
    return [{"rank": rank} for rank in range(count)]


def test_events_are_sent_in_batches_and_reports_are_added(api: str) -> None:
    result = send(events(5), api, batch_size=2)
    assert result == LoadResult(accepted=5, duplicates=3, rejected=0)
    assert [batch["events"] for batch in FakeApi.batches] == [
        [{"rank": 0}, {"rank": 1}],
        [{"rank": 2}, {"rank": 3}],
        [{"rank": 4}],
    ]
    assert {batch["path"] for batch in FakeApi.batches} == {"/api/v1/lineage/batch"}
    assert {batch["type"] for batch in FakeApi.batches} == {"application/json"}


def test_nothing_to_send_sends_nothing(api: str) -> None:
    assert send([], api) == LoadResult()
    assert FakeApi.batches == []


def test_rejected_events_are_counted(api: str) -> None:
    FakeApi.body = json.dumps(
        {"accepted": 1, "duplicates": 0, "rejected": [{"index": 0, "errors": []}]}
    ).encode()
    assert send(events(2), api) == LoadResult(accepted=1, duplicates=0, rejected=1)


def test_a_refused_batch_stops_the_load(api: str) -> None:
    FakeApi.status = 413
    with pytest.raises(LoadError, match="HTTP 413"):
        send(events(3), api, batch_size=1)
    assert len(FakeApi.batches) == 1


def test_an_unreadable_answer_stops_the_load(api: str) -> None:
    FakeApi.body = b"<html>"
    with pytest.raises(LoadError, match="illisible"):
        send(events(1), api)


def test_an_unreachable_api_is_reported() -> None:
    with pytest.raises(LoadError, match="injoignable"):
        send(events(1), "http://127.0.0.1:9", timeout=2)


@pytest.mark.parametrize("address", ["file:///etc/passwd", "127.0.0.1:8000", "ftp://exemple", ""])
def test_only_an_http_address_is_accepted(address: str) -> None:
    with pytest.raises(LoadError, match="http://"):
        send(events(1), address)


def test_a_batch_holds_at_least_one_event(api: str) -> None:
    with pytest.raises(LoadError, match="au moins un"):
        send(events(1), api, batch_size=0)


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("http://127.0.0.1:8000", "http://127.0.0.1:8000/api/v1/lineage/batch"),
        ("http://127.0.0.1:8000/", "http://127.0.0.1:8000/api/v1/lineage/batch"),
        ("https://lignage.exemple/al/", "https://lignage.exemple/al/api/v1/lineage/batch"),
    ],
)
def test_batch_address_is_built_from_the_api_address(base: str, expected: str) -> None:
    assert batch_url(base) == expected
