"""Un émetteur OIDC de test : ses clés générées ici, ses documents servis sans réseau.

``LocalIssuer`` tient les clés (RSA, EC P-256, Ed25519), sa découverte, son
JWKS et frappe des jetons. Les tests de l'API lisent ses documents en mémoire
(``fetch``) ; les tests de bout en bout le servent en HTTP sur la boucle locale
(``IssuerApp``), avec son point d'autorisation, son point de jeton et sa
déconnexion, pour l'explorateur et le proxy.

Aucun jeton de ce module n'est un secret : les clés naissent à chaque session
de tests et meurent avec elle.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import parse_qs, unquote_plus, urlencode, urlsplit

import jwt
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from jwt.algorithms import ECAlgorithm, OKPAlgorithm, RSAAlgorithm
from starlette.types import Receive, Scope, Send

from answerlineage_api.auth.access import Authentication
from answerlineage_api.auth.fetch import IssuerUnavailableError
from answerlineage_api.auth.oidc import OidcSettings
from answerlineage_api.auth.roles import PERSON_ROLES, Role

ISSUER: Final = "https://idp.exemple.test/realms/lignage"
AUDIENCE: Final = "answerlineage-api"
EMITTER_CLIENT: Final = "proxy-essai"
EMITTER_SECRET: Final = "secret-du-proxy-essai"  # noqa: S105 — secret de test, né et mort ici.
EXPLORER_CLIENT: Final = "explorateur-essai"
EVERY_ROLE: Final = frozenset(Role)

# Les clés de la session de tests : une par algorithme, et une clé que l'émetteur ne
# publie pas, celle d'un faussaire.
RSA_KEY: Final = rsa.generate_private_key(public_exponent=65537, key_size=2048)
EC_KEY: Final = ec.generate_private_key(ec.SECP256R1())
ED_KEY: Final = ed25519.Ed25519PrivateKey.generate()
FORGER_KEY: Final = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KIDS: Final = {"RS256": "cle-rsa", "ES256": "cle-ec", "EdDSA": "cle-ed"}
_PRIVATE: Final[Mapping[str, Any]] = {"RS256": RSA_KEY, "ES256": EC_KEY, "EdDSA": ED_KEY}


def _jwk(algorithm: str) -> dict[str, Any]:
    public = _PRIVATE[algorithm].public_key()
    converter: Any = {"RS256": RSAAlgorithm, "ES256": ECAlgorithm, "EdDSA": OKPAlgorithm}[algorithm]
    jwk: dict[str, Any] = json.loads(converter.to_jwk(public))
    return {**jwk, "kid": KIDS[algorithm], "use": "sig", "alg": algorithm}


def public_pem() -> bytes:
    """La clé publique RSA de l'émetteur, telle qu'un faussaire la trouverait."""
    return RSA_KEY.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


@dataclass
class LocalIssuer:
    """L'émetteur, ses documents et ses jetons. ``url`` se fixe une fois le serveur lancé."""

    url: str = ISSUER
    audience: str = AUDIENCE
    # Documents lus en mémoire, et pannes simulées.
    reads: list[str] = field(default_factory=list)
    unavailable: bool = False
    published: tuple[str, ...] = ("RS256", "ES256", "EdDSA")

    def discovery(self) -> dict[str, Any]:
        return {
            "issuer": self.url,
            "jwks_uri": f"{self.url}/protocol/openid-connect/certs",
            "authorization_endpoint": f"{self.url}/protocol/openid-connect/auth",
            "token_endpoint": f"{self.url}/protocol/openid-connect/token",
            "end_session_endpoint": f"{self.url}/protocol/openid-connect/logout",
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
            "id_token_signing_alg_values_supported": list(KIDS),
        }

    def jwks(self) -> dict[str, Any]:
        return {"keys": [_jwk(algorithm) for algorithm in self.published]}

    def document(self, url: str) -> bytes | None:
        """Le document servi à cette adresse, ou ``None``."""
        path = urlsplit(url).path
        if url == f"{self.url}/.well-known/openid-configuration" or path.endswith(
            "/.well-known/openid-configuration"
        ):
            return json.dumps(self.discovery()).encode()
        if path.endswith("/protocol/openid-connect/certs"):
            return json.dumps(self.jwks()).encode()
        return None

    async def fetch(self, url: str) -> bytes:
        """Les documents de l'émetteur, lus sans réseau par l'API."""
        self.reads.append(url)
        found = None if self.unavailable else self.document(url)
        if found is None:
            raise IssuerUnavailableError("émetteur de test indisponible")
        return found

    def claims(
        self,
        roles: tuple[str, ...] = (),
        *,
        client: str | None = None,
        lifetime: int = 300,
    ) -> dict[str, Any]:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": self.url,
            "aud": self.audience,
            "sub": "personne-essai",
            "iat": now,
            "exp": now + lifetime,
            "roles": list(roles),
        }
        if client is not None:
            claims["azp"] = client
        return claims

    def sign(
        self,
        claims: Mapping[str, Any],
        *,
        algorithm: str = "RS256",
        kid: str | None = None,
        key: Any = None,
        headers: Mapping[str, Any] | None = None,
    ) -> str:
        """Un jeton signé ; par défaut de la clé publiée pour l'algorithme."""
        return jwt.encode(
            dict(claims),
            key if key is not None else _PRIVATE[algorithm],
            algorithm=algorithm,
            headers={
                "kid": KIDS.get(algorithm, "inconnue") if kid is None else kid,
                **(headers or {}),
            },
        )

    def token(self, *roles: Role | str, algorithm: str = "RS256", **changes: Any) -> str:
        """Un jeton juste, qui porte ces rôles ; ``Role.EMITTER`` en fait celui d'un émetteur."""
        person = tuple(str(role) for role in roles if role is not Role.EMITTER)
        client = EMITTER_CLIENT if Role.EMITTER in roles else None
        claims = {**self.claims(person, client=client), **changes}
        return self.sign(claims, algorithm=algorithm)

    def headers(self, *roles: Role | str) -> dict[str, str]:
        """L'en-tête d'un appelant qui a ces rôles ; sans rôle nommé, tous les rôles."""
        chosen = roles or tuple(EVERY_ROLE)
        return {"authorization": f"Bearer {self.token(*chosen)}"}

    def settings(self, **changes: Any) -> OidcSettings:
        options: dict[str, Any] = {
            "emitter_clients": [EMITTER_CLIENT],
            "explorer_client_id": EXPLORER_CLIENT,
        }
        return OidcSettings.create(self.url, self.audience, **{**options, **changes})

    def authentication(self, **changes: Any) -> Authentication:
        """L'authentification de l'API devant cet émetteur, ses documents lus en mémoire."""
        return Authentication.oidc(self.settings(**changes), fetch=self.fetch)


# L'émetteur de la session de tests, lu en mémoire.
TEST_ISSUER: Final = LocalIssuer()


def forged(forge: str, subject: str = "personne-essai", issuer: LocalIssuer = TEST_ISSUER) -> str:
    """Le jeton d'une contrefaçon du corpus d'abus (``FORGES``), qui porte ce sujet.

    Chacun ressemble à un jeton d'émetteur, qui ouvrirait l'ingestion s'il était juste."""
    now = int(time.time())
    claims = {**issuer.claims(PERSONS, client=EMITTER_CLIENT), "sub": subject}
    if forge == "alg-none":
        header = {"alg": "none", "typ": "JWT", "kid": KIDS["RS256"]}
        parts = (json.dumps(header).encode(), json.dumps(claims).encode())
        return ".".join(b64(part) for part in parts) + "."
    if forge == "hs256-cle-publique":
        # À la main : la bibliothèque refuse elle-même une clé publique comme secret HMAC.
        header = {"alg": "HS256", "typ": "JWT", "kid": KIDS["RS256"]}
        signing = ".".join(b64(json.dumps(part).encode()) for part in (header, claims))
        signature = hmac.new(public_pem(), signing.encode(), hashlib.sha256).digest()
        return f"{signing}.{b64(signature)}"
    if forge == "emetteur-etranger":
        return issuer.sign({**claims, "iss": "https://attaquant.example/realms/lignage"})
    if forge == "audience-etrangere":
        return issuer.sign({**claims, "aud": "une-autre-api"})
    if forge == "expire":
        return issuer.sign({**claims, "iat": now - 7200, "exp": now - 3600})
    if forge == "pas-encore-valide":
        return issuer.sign({**claims, "nbf": now + 600})
    if forge == "kid-inconnu":
        return issuer.sign(claims, kid="cle-inconnue", key=FORGER_KEY)
    if forge == "signature-alteree":
        head, payload, mark = issuer.sign(claims).split(".")
        flipped = bytearray(base64.urlsafe_b64decode(mark + "=="))
        flipped[-1] ^= 0x01
        return f"{head}.{payload}.{b64(bytes(flipped))}"
    if forge == "demesure":
        return issuer.sign({**claims, "remplissage": "x" * (17 * 1024)})
    if forge == "lecteur-seul":
        return issuer.sign({**issuer.claims((Role.READER.value,)), "sub": subject})
    raise ValueError("contrefaçon inconnue")


@dataclass
class EmitterTokens:
    """Les jetons d'un émetteur, comme le proxy les obtiendrait de l'émetteur de test."""

    issuer: LocalIssuer = field(default_factory=lambda: TEST_ISSUER)
    invalidated: int = 0

    async def token(self) -> str:
        return self.issuer.token(Role.EMITTER)

    def invalidate(self) -> None:
        self.invalidated += 1


def authentication(**changes: Any) -> Authentication:
    """L'authentification de l'API dans les tests : l'émetteur de test, sans réseau."""
    return TEST_ISSUER.authentication(**changes)


def signed_in(*roles: Role | str) -> dict[str, str]:
    """L'en-tête d'un appelant de l'émetteur de test ; sans rôle nommé, tous les rôles."""
    return TEST_ISSUER.headers(*roles)


PERSONS: Final = tuple(sorted(PERSON_ROLES))


@dataclass
class _Grant:
    client: str
    redirect_uri: str
    challenge: str
    nonce: str | None


@dataclass
class IssuerApp:
    """L'émetteur servi en HTTP : découverte, JWKS, autorisation avec PKCE, jetons,
    déconnexion. L'autorisation accorde aussitôt, à une personne qui a ``roles``."""

    issuer: LocalIssuer
    roles: tuple[str, ...] = PERSONS
    lifetime: int = 300
    clients: Mapping[str, str] = field(default_factory=lambda: {EMITTER_CLIENT: EMITTER_SECRET})
    grants: dict[str, _Grant] = field(default_factory=dict)
    issued: list[str] = field(default_factory=list)
    logouts: int = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        path = scope["path"]
        query = parse_qs(scope["query_string"].decode())
        headers = {key.decode().lower(): value.decode() for key, value in scope["headers"]}
        status, extra, content = self.answer(scope["method"], path, query, headers, body)
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"access-control-allow-origin", b"*"),
                    (b"cache-control", b"no-store"),
                    *((key.encode(), value.encode()) for key, value in extra.items()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": content})

    def answer(
        self,
        method: str,
        path: str,
        query: Mapping[str, list[str]],
        headers: Mapping[str, str],
        body: bytes,
    ) -> tuple[int, dict[str, str], bytes]:
        found = self.issuer.document(self.issuer.url + path) if method == "GET" else None
        if found is not None:
            return 200, {"content-type": "application/json"}, found
        if path.endswith("/auth") and method == "GET":
            return self._authorize(query)
        if path.endswith("/token") and method == "POST":
            return self._token(parse_qs(body.decode()), headers)
        if path.endswith("/logout") and method == "GET":
            self.logouts += 1
            target = query.get("post_logout_redirect_uri", [""])[0]
            return 302, {"location": target}, b""
        if method == "OPTIONS":
            return 204, {"access-control-allow-headers": "content-type"}, b""
        return 404, {}, b""

    def _authorize(self, query: Mapping[str, list[str]]) -> tuple[int, dict[str, str], bytes]:
        def one(name: str) -> str | None:
            values = query.get(name, [])
            return values[0] if len(values) == 1 else None

        redirect, client, state = one("redirect_uri"), one("client_id"), one("state")
        challenge = one("code_challenge")
        if (
            redirect is None
            or client != EXPLORER_CLIENT
            or state is None
            or challenge is None
            or one("code_challenge_method") != "S256"
            or one("response_type") != "code"
        ):
            return 400, {"content-type": "text/plain"}, b"demande refusee"
        code = secrets.token_urlsafe(16)
        self.grants[code] = _Grant(client, redirect, challenge, one("nonce"))
        return 302, {"location": f"{redirect}?{urlencode({'code': code, 'state': state})}"}, b""

    def _refused(self, error: str) -> tuple[int, dict[str, str], bytes]:
        return 400, {"content-type": "application/json"}, json.dumps({"error": error}).encode()

    def _token(
        self, form: Mapping[str, list[str]], headers: Mapping[str, str]
    ) -> tuple[int, dict[str, str], bytes]:
        grant = form.get("grant_type", [""])[0]
        if grant == "client_credentials":
            scheme, _, encoded = headers.get("authorization", "").partition(" ")
            user, _, password = base64.b64decode(encoded or "").decode().partition(":")
            client, secret = unquote_plus(user), unquote_plus(password)
            if scheme != "Basic" or self.clients.get(client) != secret:
                return self._refused("invalid_client")
            token = self.issuer.sign(self.issuer.claims(client=client, lifetime=self.lifetime))
            return self._granted(token)
        if grant != "authorization_code":
            return self._refused("unsupported_grant_type")
        code = form.get("code", [""])[0]
        held = self.grants.pop(code, None)
        verifier = form.get("code_verifier", [""])[0]
        challenge = b64(hashlib.sha256(verifier.encode()).digest())
        if (
            held is None
            or form.get("client_id", [""])[0] != held.client
            or form.get("redirect_uri", [""])[0] != held.redirect_uri
            or challenge != held.challenge
        ):
            return self._refused("invalid_grant")
        token = self.issuer.sign(self.issuer.claims(self.roles, lifetime=self.lifetime))
        identity = self.issuer.sign(
            {**self.issuer.claims(lifetime=self.lifetime), "aud": held.client, "nonce": held.nonce}
        )
        return self._granted(token, id_token=identity)

    def _granted(self, token: str, **more: str) -> tuple[int, dict[str, str], bytes]:
        self.issued.append(token)
        answer = {"access_token": token, "token_type": "Bearer", "expires_in": self.lifetime}
        return 200, {"content-type": "application/json"}, json.dumps({**answer, **more}).encode()


@asynccontextmanager
async def serving_issuer(**options: Any) -> AsyncIterator[tuple[LocalIssuer, IssuerApp]]:
    """Un émetteur servi en HTTP sur un port libre de la boucle locale, et son application.

    Ses clés sont celles de la session : l'API de test le lit en mémoire comme en HTTP."""
    issuer = LocalIssuer()
    app = IssuerApp(issuer, **options)
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="warning", access_log=False, lifespan="off"
    )
    server = uvicorn.Server(config)
    task = asyncio.ensure_future(server.serve())
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.005)
    port = server.servers[0].sockets[0].getsockname()[1]
    issuer.url = f"http://127.0.0.1:{port}/realms/lignage"
    try:
        yield issuer, app
    finally:
        server.should_exit = True
        await task
