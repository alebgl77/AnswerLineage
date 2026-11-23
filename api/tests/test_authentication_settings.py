"""Le réglage de l'authentification, et la lecture des documents de l'émetteur.

Sans émetteur réglé, l'API ne démarre pas. Elle ne se désactive que par un
réglage nommé, pour la démonstration, sur la boucle locale seule. Les
documents de l'émetteur se lisent en https, sans redirection, bornés : un
serveur local, sur un port libre, joue l'émetteur.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from answerlineage_api.app import create_app
from answerlineage_api.auth.access import (
    ENV_AUDIENCE,
    ENV_EMITTER_CLIENTS,
    ENV_EXPLORER_CLIENT_ID,
    ENV_EXPLORER_SCOPE,
    ENV_ISSUER,
    ENV_MODE,
    ENV_ROLE_MAP,
    ENV_ROLES_CLAIM,
    Authentication,
    authentication_from_settings,
)
from answerlineage_api.auth.fetch import (
    MAX_DOCUMENT_BYTES,
    IssuerUnavailableError,
    fetch_document,
    secure_url,
)
from answerlineage_api.auth.oidc import OidcVerifier
from answerlineage_api.auth.roles import Role
from answerlineage_api.headers import content_security_policy
from api.tests.issuer import EXPLORER_CLIENT, LocalIssuer, authentication

pytestmark = pytest.mark.anyio

ISSUER = "https://idp.exemple.test/realms/lignage"
SETTINGS = {ENV_ISSUER: ISSUER, ENV_AUDIENCE: "answerlineage-api"}


@pytest.fixture(autouse=True)
def no_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (ENV_MODE, ENV_ISSUER, ENV_AUDIENCE, ENV_ROLE_MAP, "ANSWERLINEAGE_ALLOWED_HOSTS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("ANSWERLINEAGE_ALLOWED_ORIGINS", raising=False)


def test_without_an_issuer_the_api_does_not_start() -> None:
    with pytest.raises(ValueError, match=ENV_ISSUER):
        create_app()
    with pytest.raises(ValueError, match=ENV_AUDIENCE):
        authentication_from_settings(None, {ENV_ISSUER: ISSUER})


def test_the_environment_sets_every_part_of_the_authentication() -> None:
    chosen = authentication_from_settings(
        None,
        {
            **SETTINGS,
            ENV_ROLES_CLAIM: "realm_access.roles",
            ENV_ROLE_MAP: json.dumps({"lignage-admins": ["administrator"], "lecteurs": "reader"}),
            ENV_EMITTER_CLIENTS: "proxy-a, proxy-b",
            ENV_EXPLORER_CLIENT_ID: "explorateur",
            ENV_EXPLORER_SCOPE: "openid api://lignage/acces",
        },
    )
    assert isinstance(chosen.verifier, OidcVerifier)
    settings = chosen.verifier.settings
    assert settings.issuer == ISSUER
    assert settings.roles_claim == ("realm_access", "roles")
    assert settings.role_map == {
        "lignage-admins": frozenset({Role.ADMINISTRATOR}),
        "lecteurs": frozenset({Role.READER}),
    }
    assert settings.emitter_clients == {"proxy-a", "proxy-b"}
    assert (settings.explorer_client_id, settings.explorer_scope) == (
        "explorateur",
        "openid api://lignage/acces",
    )


def test_the_defaults_read_the_roles_claim_without_table_and_no_explorer() -> None:
    chosen = authentication_from_settings(None, SETTINGS)
    assert chosen.verifier is not None
    settings = chosen.verifier.settings
    assert (settings.roles_claim, settings.role_map) == (("roles",), None)
    assert (settings.explorer_client_id, settings.emitter_clients) == (None, frozenset())


@pytest.mark.parametrize(
    "changes",
    [
        {ENV_ROLE_MAP: "pas du json"},
        {ENV_ROLE_MAP: "[]"},
        {ENV_ROLE_MAP: json.dumps({"admins": 3})},
        {ENV_ROLE_MAP: json.dumps({"admins": "emitter"})},
        {ENV_ISSUER: "http://idp.exemple.test"},
        {ENV_MODE: "canari-mode"},
    ],
    ids=["illisible", "pas un objet", "rôle d'un autre type", "émetteur", "http", "mode inconnu"],
)
def test_an_unusable_setting_stops_the_api_without_citing_it(changes: dict[str, str]) -> None:
    with pytest.raises(ValueError) as refused:
        authentication_from_settings(None, {**SETTINGS, **changes})
    for value in changes.values():
        assert value not in str(refused.value)


def test_the_authentication_is_disabled_only_by_its_named_setting() -> None:
    assert authentication_from_settings(None, {ENV_MODE: "disabled"}).verifier is None
    assert authentication_from_settings(None, {ENV_MODE: "oidc", **SETTINGS}).verifier is not None


@pytest.mark.parametrize(
    ("hosts", "origins"),
    [(["*"], None), (["lignage.exemple.fr"], None), (None, ["https://console.exemple.fr"])],
    ids=["tout hôte", "un autre hôte", "une autre origine"],
)
def test_disabled_the_api_refuses_to_start_beyond_the_loopback(
    hosts: list[str] | None, origins: list[str] | None
) -> None:
    with pytest.raises(ValueError, match="boucle locale"):
        create_app(
            allowed_hosts=hosts,
            allowed_origins=origins,
            authentication=Authentication.disabled(),
        )


def test_disabled_the_api_starts_on_the_loopback_and_its_origins() -> None:
    app = create_app(
        allowed_origins=["http://127.0.0.1:5173", "http://localhost:5173", "http://[::1]:8000"],
        authentication=Authentication.disabled(),
    )
    assert app.state.authentication.mode == "disabled"


def test_disabled_by_the_environment_the_api_refuses_an_extra_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_MODE, "disabled")
    monkeypatch.setenv("ANSWERLINEAGE_ALLOWED_HOSTS", "lignage.exemple.fr")
    with pytest.raises(ValueError, match="ANSWERLINEAGE_ALLOWED_HOSTS"):
        create_app()


# Ce que l'explorateur lit sans jeton, et ce qu'il peut joindre.


async def settings_route(chosen: Authentication) -> tuple[dict[str, Any], str]:
    engine = create_async_engine("postgresql+psycopg://personne@127.0.0.1:1/aucune")
    try:
        transport = httpx.ASGITransport(app=create_app(engine, authentication=chosen))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            answered = await client.get("/api/v1/authentication")
            live = await client.get("/api/v1/live")
    finally:
        await engine.dispose()
    assert live.json() == {"status": "alive"}
    return answered.json(), answered.headers["content-security-policy"]


async def test_the_explorer_learns_the_issuer_and_may_reach_its_origin_only() -> None:
    found, policy = await settings_route(authentication())
    assert found == {
        "mode": "oidc",
        "issuer": LocalIssuer().url,
        "client_id": EXPLORER_CLIENT,
        "scope": "openid",
    }
    assert policy == content_security_policy(["https://idp.exemple.test"])
    assert "connect-src 'self' https://idp.exemple.test;" in policy


async def test_without_explorer_client_nothing_else_is_reachable() -> None:
    found, policy = await settings_route(authentication(explorer_client_id=None))
    assert (found["client_id"], found["scope"]) == (None, None)
    assert policy == content_security_policy()


# La lecture des documents de l'émetteur.


class Documents(BaseHTTPRequestHandler):
    """L'émetteur local : il rend ce que le test lui a donné, par chemin."""

    served: ClassVar[dict[str, tuple[int, dict[str, str], bytes]]] = {}
    seen: ClassVar[list[dict[str, str]]] = []

    def do_GET(self) -> None:
        type(self).seen.append(dict(self.headers))
        status, headers, body = self.served.get(self.path, (404, {}, b""))
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """Rien sur la sortie des tests."""


@pytest.fixture
def local() -> Iterator[str]:
    Documents.served, Documents.seen = {}, []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Documents)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


async def test_a_document_is_read_from_the_issuer_without_proxy_or_identity(
    local: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Un mandataire de l'environnement n'est pas suivi : la requête irait ailleurs.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    Documents.served["/certs"] = (200, {"content-type": "application/json"}, b'{"keys": []}')
    assert await fetch_document(f"{local}/certs") == b'{"keys": []}'
    (seen,) = Documents.seen
    assert "Authorization" not in seen


@pytest.mark.parametrize(
    ("status", "headers", "body"),
    [
        (302, {"location": "/ailleurs"}, b""),
        (404, {}, b""),
        (500, {}, b""),
        (200, {}, b" " * (MAX_DOCUMENT_BYTES + 1)),
    ],
    ids=["redirection", "absent", "panne", "trop grand"],
)
async def test_an_unusable_answer_of_the_issuer_gives_nothing(
    local: str, status: int, headers: dict[str, str], body: bytes
) -> None:
    Documents.served["/certs"] = (status, headers, body)
    Documents.served["/ailleurs"] = (200, {}, b'{"keys": []}')
    with pytest.raises(IssuerUnavailableError):
        await fetch_document(f"{local}/certs")
    # Une seule requête : une redirection n'est pas suivie.
    assert len(Documents.seen) == 1


async def test_an_issuer_that_does_not_answer_gives_nothing() -> None:
    with pytest.raises(IssuerUnavailableError):
        await fetch_document("http://127.0.0.1:9/certs")


@pytest.mark.parametrize(
    ("url", "secure"),
    [
        ("https://idp.exemple.fr/certs", True),
        ("http://127.0.0.1:8080/certs", True),
        ("http://localhost/certs", True),
        ("http://[::1]/certs", True),
        ("http://idp.exemple.fr/certs", False),
        ("https://u:p@idp.exemple.fr/certs", False),
        ("https://idp.exemple.fr/certs#x", False),
        ("file:///etc/passwd", False),
        ("https://[::1/certs", False),
    ],
)
def test_only_https_or_the_loopback_is_read(url: str, secure: bool) -> None:
    assert secure_url(url) is secure


async def test_a_refused_address_is_never_reached() -> None:
    with pytest.raises(IssuerUnavailableError, match="adresse refusée"):
        await fetch_document("http://idp.exemple.fr/certs")


async def test_the_api_verifies_tokens_with_the_keys_of_a_served_issuer(local: str) -> None:
    """De bout en bout, sans réseau : la découverte et le JWKS lus d'un émetteur local."""
    issuer = LocalIssuer(url=f"{local}/realms/lignage")
    for path in (
        "/realms/lignage/.well-known/openid-configuration",
        "/realms/lignage/protocol/openid-connect/certs",
    ):
        found = issuer.document(local + path)
        assert found is not None
        Documents.served[path] = (200, {"content-type": "application/json"}, found)
    checked = OidcVerifier(issuer.settings())
    assert await checked.roles(issuer.token(Role.READER)) == {Role.READER}
