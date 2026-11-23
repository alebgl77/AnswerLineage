"""Serveur MCP de test protégé par OAuth : une ressource protégée qui vérifie le jeton
porteur et son audience, et publie ses métadonnées (RFC 9728).

Le serveur se croit publié à ``https://mcp.exemple.fr/mcp`` : c'est sa ressource, celle
pour laquelle un client obtient un jeton (RFC 8707). Il n'accepte qu'un jeton signé par
son serveur d'autorisation, pas expiré, dont l'audience est cette ressource. Le jeton
est un JWT signé HS256 par une clé de test : sa vérification tient en bibliothèque
standard, sans rien emprunter au proxy.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any

from answerlineage_proxy.app import Receive, Scope, Send
from proxy.tests.conftest import Received

RESOURCE_HOST = "mcp.exemple.fr"
RESOURCE = f"https://{RESOURCE_HOST}/mcp"
METADATA_PATH = "/.well-known/oauth-protected-resource/mcp"
AUTHORIZATION_SERVER = "https://auth.exemple.fr"
# Clé de signature du serveur d'autorisation de test : une valeur de test, pas un secret.
SIGNING_KEY = b"cle-de-test-du-serveur-d-autorisation-0001"
METADATA = json.dumps(
    {
        "resource": RESOURCE,
        "authorization_servers": [AUTHORIZATION_SERVER],
        "bearer_methods_supported": ["header"],
    }
).encode()
CHALLENGE = (
    f'Bearer error="invalid_token", resource_metadata="https://{RESOURCE_HOST}{METADATA_PATH}"'
)
RESULT = b'{"jsonrpc":"2.0","id":1,"result":{"content":[],"isError":false}}'


def _encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decode(text: bytes) -> bytes:
    return base64.urlsafe_b64decode(text + b"=" * (-len(text) % 4))


def forge(audience: str, subject: str = "agent", *, key: bytes = SIGNING_KEY) -> str:
    """Un jeton du serveur d'autorisation de test, valable cinq minutes."""
    header = _encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    claims = {
        "iss": AUTHORIZATION_SERVER,
        "aud": audience,
        "sub": subject,
        "exp": int(time.time()) + 300,
    }
    payload = _encode(json.dumps(claims).encode())
    signature = hmac.new(key, f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{_encode(signature)}"


@dataclass
class ProtectedMcpServer:
    """Ressource protégée : un appel n'aboutit qu'avec un jeton valide pour sa ressource."""

    resource: str = RESOURCE
    # Résultat rendu à un appel accepté : réglable, pour y placer un canari.
    result: bytes = RESULT
    # Ce que le serveur a reçu ; l'en-tête Host et l'en-tête Authorization de chaque
    # requête, octet pour octet ; les jetons acceptés et les motifs des refus.
    requests: list[Received] = field(default_factory=list)
    hosts: list[bytes] = field(default_factory=list)
    authorizations: list[list[bytes]] = field(default_factory=list)
    accepted: list[bytes] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        headers = [(bytes(name), bytes(value)) for name, value in scope["headers"]]
        self.requests.append(
            Received(
                method=scope["method"],
                raw_path=scope["raw_path"],
                query=scope["query_string"],
                headers=[(k.decode("latin-1"), v.decode("latin-1")) for k, v in headers],
                body=body,
            )
        )
        self.hosts.extend(value for name, value in headers if name == b"host")
        self.authorizations.append([value for name, value in headers if name == b"authorization"])
        if scope["method"] == "GET" and scope["path"] == METADATA_PATH:
            await _respond(send, 200, METADATA, [(b"content-type", b"application/json")])
            return
        if scope["path"] != "/mcp":
            await _respond(send, 404, b"")
            return
        refusal = self._refusal(self.authorizations[-1])
        if refusal is not None:
            self.refusals.append(refusal)
            await _respond(
                send,
                401,
                b'{"error":"invalid_token"}',
                [
                    (b"www-authenticate", CHALLENGE.encode()),
                    (b"content-type", b"application/json"),
                ],
            )
            return
        self.accepted.append(self.authorizations[-1][0])
        await _respond(send, 200, self.result, [(b"content-type", b"application/json")])

    def _refusal(self, authorizations: list[bytes]) -> str | None:
        """Le motif du refus d'un jeton, ou ``None`` quand il est valide pour la ressource."""
        if len(authorizations) != 1:
            return "missing"
        scheme, _, token = authorizations[0].partition(b" ")
        parts = token.split(b".")
        if scheme.lower() != b"bearer" or len(parts) != 3:
            return "malformed"
        expected = hmac.new(SIGNING_KEY, parts[0] + b"." + parts[1], hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _decode(parts[2])):
            return "signature"
        claims: dict[str, Any] = json.loads(_decode(parts[1]))
        audience = claims.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        if self.resource not in audiences:
            return "audience"
        if claims.get("exp", 0) <= time.time():
            return "expired"
        return None


async def _respond(
    send: Send, status: int, body: bytes, headers: list[tuple[bytes, bytes]] | None = None
) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [*(headers or []), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})
