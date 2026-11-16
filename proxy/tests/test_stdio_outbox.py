"""En stdio, la boîte d'envoi se vide à l'arrêt du serveur, dans son délai et pas au-delà."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from proxy.tests.conftest import stdio_server
from proxy.tests.stdio_server import RESULT_TEXT

FLUSH_SECONDS = 1.0
# Ce que la sortie du proxy peut suivre celle du serveur au-delà du délai de vidage : la fin
# des analyses, du dernier relevé et de l'interpréteur, sur une machine partagée.
MARGIN_SECONDS = 4.0


def test_the_flush_delay_bounds_how_long_the_exit_follows_the_server(tmp_path: Path) -> None:
    """L'API injoignable : le proxy attend son délai de vidage, compte l'événement perdu, et
    rend le code du serveur sans s'attarder davantage."""
    call = {"name": "get_weather", "arguments": {"location": "Séville"}}
    sent = b"".join(
        json.dumps(message).encode() + b"\n"
        for message in (
            {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": call},
        )
    )
    with socket.socket() as unreachable:
        # Réservé sans écouter : la connexion à l'API y est refusée.
        unreachable.bind(("127.0.0.1", 0))
        api = f"http://127.0.0.1:{unreachable.getsockname()[1]}"
        process = subprocess.Popen(  # noqa: S603 — commande du test, sans shell.
            [
                sys.executable,
                "-m",
                "answerlineage_proxy.cli",
                "--api-url",
                api,
                "--outbox-flush-seconds",
                str(FLUSH_SECONDS),
                "--",
                *stdio_server("--record", str(tmp_path)),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(os.environ),
        )
        assert process.stdin is not None
        assert process.stdout is not None
        process.stdin.write(sent)
        process.stdin.flush()
        # La réponse rendue, le client ferme son entrée : le serveur s'arrête, le proxy aussi.
        answered = b""
        while answered.count(b"\n") < 2:
            chunk = os.read(process.stdout.fileno(), 65_536)
            assert chunk, answered
            answered += chunk
        process.stdin.close()
        closed = time.monotonic()
        stdout, stderr = process.communicate(timeout=60)
        elapsed = time.monotonic() - closed

    assert process.returncode == 0
    assert RESULT_TEXT.encode() in answered + stdout
    # Le délai de vidage a été tenu, puis l'événement compté perdu, et pas davantage.
    assert FLUSH_SECONDS * 0.7 <= elapsed < FLUSH_SECONDS + MARGIN_SECONDS
    assert "1 événements non envoyés sont comptés perdus".encode() in stderr
