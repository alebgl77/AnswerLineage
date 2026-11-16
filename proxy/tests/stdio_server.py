"""Serveur MCP de test, en stdio : lancé par le proxy comme un vrai serveur, en processus enfant.

Il lit une requête JSON-RPC par ligne et répond à ``initialize``, ``tools/list``
et ``tools/call``. Ce que fait un appel d'outil dépend de l'outil :

- ``fail`` : une erreur JSON-RPC, dont le message cite les arguments ;
- ``huge`` : un résultat de ``size`` octets, l'identifiant en tête de ligne ;
  ``huge_late_id`` : le même, l'identifiant en fin de ligne ;
- ``die`` : meurt sans répondre, avec le code ``code`` ;
- ``wait_eof`` : ne répond pas ; le serveur s'arrête à la fin de son entrée ;
- ``ask`` : envoie d'abord une requête et une notification (de ``size`` octets de plus)
  au client, puis répond ;
- ``whoami`` : rend le pid du serveur ;
- ``detach`` : lance un descendant qui garde la sortie du serveur ouverte, et rend son pid ;
- ``close_input`` : ferme l'entrée du serveur, répond, puis s'arrête après ``delay`` ;
- ``environment`` : dit, pour chaque nom de ``names``, si la variable existe ;
- tout autre outil : un résultat qui recopie les arguments, et ``RESULT_TEXT``.

Options : ``--record DOSSIER`` écrit dans ``DOSSIER/recu`` les octets lus et
dans ``DOSSIER/ecrit`` les octets écrits ; ``--ignore NOM`` ignore un signal
(``TERM``, ``INT``, ``HUP``) ; ``--exit-code N`` est le code de sortie à la fin
de l'entrée ; ``--stderr TEXTE`` écrit ce texte sur la sortie d'erreur au départ ;
``--ready`` écrit ``pret`` sur la sortie d'erreur une fois les signaux réglés.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, BinaryIO

RESULT_TEXT = "canari-resultat-stdio-3c8e"
SERVER_REQUEST_ID = "serveur-1"


class Server:
    def __init__(self, output: BinaryIO, record: Path | None) -> None:
        self._output = output
        self._received = (record / "recu").open("ab") if record else None
        self._written = (record / "ecrit").open("ab") if record else None

    def received(self, line: bytes) -> None:
        if self._received is not None:
            self._received.write(line)
            self._received.flush()

    def write(self, data: bytes) -> None:
        if self._written is not None:
            self._written.write(data)
            self._written.flush()
        self._output.write(data)
        self._output.flush()

    def send(self, message: dict[str, Any]) -> None:
        self.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")

    def reply(self, identifier: object, result: dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "id": identifier, "result": result})

    def call(self, identifier: object, params: dict[str, Any]) -> None:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name == "fail":
            message = f"Lieu inconnu : {json.dumps(arguments, ensure_ascii=False)}"
            error = {"code": -32602, "message": message}
            self.send({"jsonrpc": "2.0", "id": identifier, "error": error})
        elif name in ("huge", "huge_late_id"):
            text = "x" * int(arguments.get("size", 0))
            result = {"content": [{"type": "text", "text": text}], "isError": False}
            if name == "huge":
                self.reply(identifier, result)
            else:
                self.send({"jsonrpc": "2.0", "result": result, "id": identifier})
        elif name == "die":
            os._exit(int(arguments.get("code", 1)))
        elif name == "wait_eof":
            return
        elif name == "ask":
            self.send({"jsonrpc": "2.0", "id": SERVER_REQUEST_ID, "method": "ping"})
            progress = {
                "progressToken": "p",
                "progress": 1,
                "message": "y" * arguments.get("size", 0),
            }
            self.send({"jsonrpc": "2.0", "method": "notifications/progress", "params": progress})
            self.reply(identifier, {"content": [{"type": "text", "text": "ok"}]})
        elif name == "whoami":
            self.reply(identifier, {"structuredContent": {"pid": os.getpid()}, "content": []})
        elif name == "detach":
            # Un descendant qui garde la sortie du serveur ouverte après sa mort.
            holder = subprocess.Popen(["sleep", "30"])  # noqa: S607 — commande de test.
            self.reply(identifier, {"structuredContent": {"pid": holder.pid}, "content": []})
        elif name == "close_input":
            os.close(0)
            self.reply(identifier, {"content": []})
            time.sleep(float(arguments.get("delay", 0.5)))
            os._exit(0)
        elif name == "environment":
            present = {name: name in os.environ for name in arguments.get("names", [])}
            self.reply(identifier, {"structuredContent": present, "content": []})
        else:
            echo = json.dumps(arguments, ensure_ascii=False)
            content = [{"type": "text", "text": f"{RESULT_TEXT} {echo}"}]
            self.reply(identifier, {"content": content, "isError": False})

    def handle(self, line: bytes) -> None:
        try:
            message = json.loads(line)
        except ValueError:
            error = {"code": -32700, "message": "Parse error"}
            self.send({"jsonrpc": "2.0", "id": None, "error": error})
            return
        if not isinstance(message, dict) or "method" not in message:
            return
        method, identifier = message["method"], message.get("id")
        if method == "initialize":
            self.reply(
                identifier,
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "meteo", "version": "1.0.0"},
                },
            )
        elif method == "tools/list":
            tools = [{"name": "get_weather", "inputSchema": {"type": "object"}}]
            self.reply(identifier, {"tools": tools})
        elif method == "tools/call":
            self.call(identifier, message.get("params") or {})
        elif method == "ping":
            self.reply(identifier, {})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--record", type=Path)
    parser.add_argument("--ignore", action="append", default=[])
    parser.add_argument("--exit-code", type=int, default=0)
    parser.add_argument("--stderr")
    parser.add_argument("--ready", action="store_true")
    args = parser.parse_args()
    for name in args.ignore:
        signal.signal(getattr(signal, f"SIG{name}"), signal.SIG_IGN)
    if args.stderr:
        sys.stderr.write(args.stderr + "\n")
        sys.stderr.flush()
    if args.ready:
        sys.stderr.write("pret\n")
        sys.stderr.flush()
    server = Server(sys.stdout.buffer, args.record)
    for line in sys.stdin.buffer:
        server.received(line)
        server.handle(line)
    return int(args.exit_code)


if __name__ == "__main__":
    sys.exit(main())
