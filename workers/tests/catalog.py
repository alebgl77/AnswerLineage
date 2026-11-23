"""Un faux consommateur OpenLineage, servi sur la boucle locale.

Il retient chaque requête reçue, et répond ce qu'on lui dit : un statut, après
une attente s'il le faut. Il peut aussi parler TLS, sous un certificat tiré pour
le test par ``openssl``.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import uvicorn

LINEAGE = "/api/v1/lineage"


@dataclass(frozen=True)
class Answer:
    status: int = 201
    # Attente avant de répondre, en secondes : une réponse tardive, ou qui ne vient pas.
    delay: float = 0.0


@dataclass(frozen=True)
class Received:
    path: str
    headers: dict[str, str]
    body: bytes
    # Le port du client : une même connexion garde le même.
    peer: int


@dataclass
class FakeCatalog:
    """Répond d'abord les réponses en attente, puis ``answer`` à chaque requête."""

    answer: Answer = field(default_factory=Answer)
    pending: deque[Answer] = field(default_factory=deque)
    received: list[Received] = field(default_factory=list)
    location: str = "http://127.0.0.1:9/ailleurs"

    def then(self, *answers: Answer) -> FakeCatalog:
        self.pending.extend(answers)
        return self

    def bodies(self) -> list[bytes]:
        return [request.body for request in self.received]

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            while (message := await receive())["type"] != "lifespan.shutdown":
                await send({"type": "lifespan.startup.complete"})
            await send({"type": "lifespan.shutdown.complete"})
            return
        chunks = []
        while True:
            message = await receive()
            chunks.append(message.get("body", b""))
            if not message.get("more_body"):
                break
        self.received.append(
            Received(
                path=scope["path"],
                headers={key.decode(): value.decode() for key, value in scope["headers"]},
                body=b"".join(chunks),
                peer=scope["client"][1],
            )
        )
        answer = self.pending.popleft() if self.pending else self.answer
        if answer.delay:
            await asyncio.sleep(answer.delay)
        headers = [(b"content-type", b"application/json"), (b"content-length", b"0")]
        if 300 <= answer.status < 400:
            headers.append((b"location", self.location.encode()))
        await send({"type": "http.response.start", "status": answer.status, "headers": headers})
        await send({"type": "http.response.body", "body": b""})


@asynccontextmanager
async def serving(app: Any, *, certificate: tuple[Path, Path] | None = None) -> AsyncIterator[str]:
    """Sert l'application sur un port libre de la boucle locale ; rend son adresse de base."""
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        log_level="warning",
        access_log=False,
        timeout_graceful_shutdown=1,
        ssl_certfile=None if certificate is None else str(certificate[0]),
        ssl_keyfile=None if certificate is None else str(certificate[1]),
    )
    server = uvicorn.Server(config)
    task = asyncio.ensure_future(server.serve())
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.005)
    port = server.servers[0].sockets[0].getsockname()[1]
    scheme = "http" if certificate is None else "https"
    try:
        yield f"{scheme}://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


def certificate(directory: Path, names: str) -> tuple[Path, Path]:
    """Un certificat autosigné et sa clé, pour ``names`` (``IP:127.0.0.1,DNS:localhost``)."""
    openssl = shutil.which("openssl")
    if openssl is None:
        # En CI, un test de TLS sauté serait un faux vert.
        if os.environ.get("CI"):
            raise RuntimeError("openssl est requis pour éprouver la vérification TLS")
        pytest.skip("openssl indisponible : la vérification TLS n'est pas éprouvée")
    cert, key = directory / "destination.pem", directory / "destination.key"
    subprocess.run(  # noqa: S603 — commande fixe, aucune entrée extérieure.
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=answerlineage-destination",
            "-addext",
            f"subjectAltName={names}",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    return cert, key
