"""L'envoi des événements à l'API d'ingestion, lot par lot."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest

from answerlineage_corpus.loader import (
    ENV_TOKEN,
    ENV_TOKEN_FILE,
    LoadError,
    LoadResult,
    ReportResult,
    batch_url,
    send,
    send_reports,
    token_from_environment,
)


class FakeApi(BaseHTTPRequestHandler):
    """API d'ingestion de substitution : retient les lots reçus."""

    batches: ClassVar[list[dict[str, Any]]] = []
    status: ClassVar[int] = 202
    body: ClassVar[bytes | None] = None

    def do_POST(self) -> None:
        length = int(self.headers["content-length"])
        events = json.loads(self.rfile.read(length))
        type(self).batches.append(
            {
                "path": self.path,
                "type": self.headers["content-type"],
                "authorization": self.headers.get("authorization"),
                "events": events,
            }
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


def test_reports_are_sent_one_by_one_to_the_coverage_route(api: str) -> None:
    FakeApi.status = 201
    FakeApi.body = b"{}"
    assert send_reports([{"rank": 0}, {"rank": 1}], api) == ReportResult(accepted=2)
    assert [batch["events"] for batch in FakeApi.batches] == [{"rank": 0}, {"rank": 1}]
    assert {batch["path"] for batch in FakeApi.batches} == {"/api/v1/coverage"}


def test_a_rejected_report_is_counted_and_the_others_are_sent(api: str) -> None:
    FakeApi.status = 422
    FakeApi.body = b'{"errors": []}'
    assert send_reports([{"rank": 0}, {"rank": 1}], api) == ReportResult(rejected=2)
    assert len(FakeApi.batches) == 2


def test_a_report_refused_otherwise_stops_the_load(api: str) -> None:
    FakeApi.status = 413
    FakeApi.body = b"{}"
    with pytest.raises(LoadError, match="relevé \\(HTTP 413\\)"):
        send_reports([{"rank": 0}, {"rank": 1}], api)
    assert len(FakeApi.batches) == 1


def test_reports_need_an_http_address() -> None:
    with pytest.raises(LoadError, match="http://"):
        send_reports([{"rank": 0}], "ftp://exemple")


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


# Le jeton porteur d'un émetteur, quand l'API en demande un.

TOKEN = "canari.jeton-corpus_4b1e"  # noqa: S105 — canari de test, pas un secret.


def test_the_token_goes_with_every_batch_and_report(api: str) -> None:
    send(events(3), api, batch_size=2, token=TOKEN)
    FakeApi.status, FakeApi.body = 201, b"{}"
    send_reports([{"rank": 0}], api, token=TOKEN)
    assert [batch["authorization"] for batch in FakeApi.batches] == [f"Bearer {TOKEN}"] * 3


def test_without_token_no_authorization_is_sent(api: str) -> None:
    send(events(1), api)
    assert FakeApi.batches[0]["authorization"] is None


@pytest.mark.parametrize("address", ["http://lignage.exemple.fr", "http://10.0.0.5:8000"])
def test_the_token_never_leaves_in_http_beyond_the_loopback(address: str) -> None:
    for sending in (
        lambda: send(events(1), address, token=TOKEN),
        lambda: send_reports([{"rank": 0}], address, token=TOKEN),
    ):
        with pytest.raises(LoadError, match="https") as refused:
            sending()
        assert TOKEN not in str(refused.value)


def test_the_token_comes_from_a_variable_or_a_file(tmp_path: Path) -> None:
    held = tmp_path / "jeton"
    held.write_text(TOKEN + "\n", encoding="ascii")
    assert token_from_environment({}) is None
    assert token_from_environment({ENV_TOKEN: TOKEN}) == TOKEN
    assert token_from_environment({ENV_TOKEN_FILE: str(held)}) == TOKEN


@pytest.mark.parametrize(
    "environment",
    [
        {ENV_TOKEN: TOKEN, ENV_TOKEN_FILE: "/dev/null"},
        {ENV_TOKEN: f"Bearer {TOKEN}"},
        {ENV_TOKEN_FILE: "/chemin/canari-absent"},
    ],
    ids=["les deux", "préfixe", "fichier absent"],
)
def test_an_unusable_token_is_refused_without_being_cited(environment: dict[str, str]) -> None:
    with pytest.raises(LoadError) as refused:
        token_from_environment(environment)
    assert TOKEN not in str(refused.value)
    assert "canari-absent" not in str(refused.value)
