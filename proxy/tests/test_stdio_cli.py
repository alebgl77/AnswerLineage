"""La ligne de commande du mode stdio : ``answerlineage-proxy [réglages] -- <commande>``.

Ces tests lancent le vrai proxy, dans son propre processus, comme un client MCP
lance un serveur stdio : ils écrivent sur son entrée, lisent sa sortie et sa
sortie d'erreur, et lui envoient des signaux.
"""

from __future__ import annotations

import json
import logging
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any

import pytest

from answerlineage_proxy import cli, stdio
from proxy.tests.conftest import stdio_server
from proxy.tests.stdio_server import RESULT_TEXT

ARGUMENT_CANARY = "canari-argument-ligne-de-commande-61f0"
SERVER_LOG = "journal du serveur de test"


def proxy_command(*arguments: str) -> list[str]:
    return [sys.executable, "-m", "answerlineage_proxy.cli", *arguments]


def line(identifier: int, method: str, params: dict[str, Any] | None = None) -> bytes:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": identifier, "method": method}
    if params is not None:
        body["params"] = params
    return json.dumps(body).encode() + b"\n"


@contextmanager
def launched(*arguments: str) -> Iterator[subprocess.Popen[bytes]]:
    process = subprocess.Popen(  # noqa: S603 — commande du test, sans shell.
        proxy_command(*arguments),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=30)


def wait_for(stream: IO[bytes] | None, text: bytes) -> bytes:
    """Lit la sortie d'erreur du proxy jusqu'à ``text`` : ce qu'il a lu."""
    assert stream is not None
    seen = b""
    while text not in seen:
        chunk = stream.readline()
        assert chunk, seen.decode(errors="replace")
        seen += chunk
    return seen


def test_the_proxy_writes_only_what_the_server_writes_and_lets_its_errors_pass(
    tmp_path: Path,
) -> None:
    record = tmp_path / "echanges"
    record.mkdir()
    sent = b"".join(
        [
            line(0, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {}}),
            line(1, "tools/call", {"name": "get_weather", "arguments": {"l": ARGUMENT_CANARY}}),
            line(2, "tools/call", {"name": "fail", "arguments": {"l": ARGUMENT_CANARY}}),
        ]
    )
    command = stdio_server("--record", str(record), "--stderr", SERVER_LOG, "--exit-code", "5")
    with launched("--server-name", "meteo", "--", *command) as process:
        output, errors = process.communicate(sent, timeout=30)
    assert process.returncode == 5
    # Sur la sortie du proxy, rien que ce que le serveur a écrit ; à son entrée, ce que
    # le client a envoyé.
    assert output == (record / "ecrit").read_bytes()
    assert (record / "recu").read_bytes() == sent
    assert RESULT_TEXT.encode() in output
    # La sortie d'erreur du serveur passe telle quelle ; le proxy y écrit son journal, sans
    # rien des messages ni de la commande.
    assert errors.count(SERVER_LOG.encode() + b"\n") == 1
    assert b"Serveur MCP \xc2\xab meteo \xc2\xbb lanc\xc3\xa9 en stdio." in errors
    for secret in (ARGUMENT_CANARY, RESULT_TEXT, "Lieu inconnu", str(record)):
        assert secret.encode() not in errors


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_a_signal_is_forwarded_and_the_proxy_ends_with_the_server(number: int) -> None:
    with launched("--", *stdio_server("--ready")) as process:
        wait_for(process.stderr, b"pret")
        process.send_signal(number)
        assert process.wait(timeout=30) == 128 + number


def test_a_server_that_ignores_a_signal_keeps_the_proxy_running() -> None:
    """Le proxy ne s'arrête jamais avant son enfant : il ne laisse pas d'orphelin."""
    with launched(
        "--", *stdio_server("--ignore", "TERM", "--ready", "--exit-code", "6")
    ) as process:
        wait_for(process.stderr, b"pret")
        process.send_signal(signal.SIGTERM)
        wait_for(process.stderr, b"Signal SIGTERM transmis au serveur MCP.")
        time.sleep(0.3)
        assert process.poll() is None
        assert process.stdin is not None
        process.stdin.close()
        assert process.wait(timeout=30) == 6


def test_a_command_that_cannot_be_found_gives_a_fixed_error() -> None:
    missing = "/rien/canari-serveur-introuvable"
    with launched("--", missing, "--canari-argument") as process:
        output, errors = process.communicate(timeout=30)
    assert process.returncode == stdio.EXIT_NOT_FOUND
    assert output == b""
    assert "Serveur MCP non lancé : commande introuvable.".encode() in errors
    assert b"canari" not in errors


def test_a_setting_without_sense_in_stdio_mode_stops_the_proxy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["--upstream", "http://127.0.0.1:9000", "--", "serveur"]) == 2
    assert capsys.readouterr().err == (
        "Configuration invalide : L'option --upstream est sans objet en mode stdio.\n"
    )


def test_the_command_line_runs_the_stdio_proxy_with_its_settings(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    started: list[stdio.StdioProxy] = []

    async def run(proxy: stdio.StdioProxy) -> int:
        started.append(proxy)
        return 7

    monkeypatch.setattr(stdio.StdioProxy, "run", run)
    with caplog.at_level(logging.WARNING, logger="answerlineage_proxy"):
        code = cli.main(["--coverage-window-seconds", "5", "--", "/opt/mcp-meteo", "-v"])
    assert code == 7
    (proxy,) = started
    assert proxy.settings.command == ("/opt/mcp-meteo", "-v")
    assert proxy.settings.server_name == "mcp-meteo"
    assert proxy.coverage.length.total_seconds() == 5
    assert "Aucune clé d'empreinte" in caplog.text
    assert "Aucune destination d'événements" in caplog.text


def test_the_stdio_proxy_says_which_read_rules_it_ignores(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    async def run(proxy: stdio.StdioProxy) -> int:
        return 0

    rules = tmp_path / "regles.yaml"
    rules.write_text(
        "version: 1\nrules:\n  - tool: get_price\n"
        "    datasets:\n      - { namespace: postgres://ref:5432, name: ref.tarifs }\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(stdio.StdioProxy, "run", run)
    monkeypatch.setenv("ANSWERLINEAGE_PROXY_FINGERPRINT_KEY", "ab" * 32)
    arguments = ["--sql-tool", "get_price=query:postgres", "--read-rules-file", str(rules)]
    with caplog.at_level(logging.WARNING, logger="answerlineage_proxy"):
        assert cli.main([*arguments, "--", "serveur"]) == 0
    assert "La règle de lecture de l'outil « get_price » est ignorée" in caplog.text
    assert "Aucune clé d'empreinte" not in caplog.text
    assert "ab" * 32 not in caplog.text
