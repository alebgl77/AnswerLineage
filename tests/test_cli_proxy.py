"""``answerlineage proxy -- <commande>`` : la commande commune lance le proxy en stdio.

Le client MCP lance ``answerlineage`` comme il lancerait le serveur : les
appels passent octet pour octet, et le processus qu'il a lancé est le proxy
lui-même, qui reçoit ses signaux et rend le code de sortie du serveur.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
from pathlib import Path

from proxy.tests.conftest import stdio_server
from proxy.tests.stdio_server import RESULT_TEXT


def answerlineage(*arguments: str) -> list[str]:
    return [sys.executable, "-m", "answerlineage_cli.cli", *arguments]


def test_calls_pass_through_the_proxy_launched_by_the_common_command(tmp_path: Path) -> None:
    record = tmp_path / "echanges"
    record.mkdir()
    call = {"name": "get_weather", "arguments": {"location": "Séville"}}
    sent = b"".join(
        json.dumps(message, ensure_ascii=False).encode() + b"\n"
        for message in (
            {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": call},
        )
    )
    command = answerlineage(
        "proxy", "--server-name", "meteo", "--", *stdio_server("--record", str(record))
    )
    completed = subprocess.run(  # noqa: S603 — commande du test, sans shell.
        command, input=sent, capture_output=True, timeout=60, check=False
    )
    assert completed.returncode == 0
    assert (record / "recu").read_bytes() == sent
    assert completed.stdout == (record / "ecrit").read_bytes()
    assert RESULT_TEXT.encode() in completed.stdout
    assert "Serveur MCP « meteo » lancé en stdio.".encode() in completed.stderr


def test_a_signal_sent_to_the_common_command_reaches_the_server() -> None:
    process = subprocess.Popen(  # noqa: S603 — commande du test, sans shell.
        answerlineage("proxy", "--", *stdio_server("--ready")),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stderr is not None
        seen = b""
        while b"pret" not in seen:
            line = process.stderr.readline()
            assert line, seen.decode(errors="replace")
            seen += line
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=30) == 128 + signal.SIGTERM
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=30)
