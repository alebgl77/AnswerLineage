"""Modes transparent et passerelle, devant un serveur MCP protégé par OAuth.

En mode transparent, le client joint le proxy à l'URL du serveur : le jeton qu'il a
obtenu pour cette URL passe tel quel, et le serveur l'accepte. En mode passerelle, le
client joint le proxy à sa propre URL : un jeton lié à l'URL du serveur ne peut plus
passer, et la configuration est refusée, avec son explication.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
import pytest

from answerlineage_proxy import cli
from answerlineage_proxy.app import McpProxy, Receive, Scope, Send
from answerlineage_proxy.config import (
    GATEWAY_REFUSED_BY_DECLARATION,
    GATEWAY_REFUSED_BY_METADATA,
    ProxyConfigurationError,
    ProxySettings,
)
from answerlineage_proxy.fingerprint import FingerprintKey
from answerlineage_proxy.resource import (
    MAX_METADATA_BYTES,
    MAX_RESOURCE_LENGTH,
    MetadataReading,
    check_gateway,
    read_metadata,
)
from answerlineage_proxy.sink import MemorySink
from proxy.tests.conftest import MODERN_HEADERS, MODERN_META, exchange, running, tool_call
from proxy.tests.oauth import (
    AUTHORIZATION_SERVER,
    CHALLENGE,
    METADATA,
    METADATA_PATH,
    RESOURCE,
    RESOURCE_HOST,
    RESULT,
    ProtectedMcpServer,
    forge,
)

pytestmark = pytest.mark.anyio

CALL = tool_call(**MODERN_META)
# Canari placé dans les réponses hostiles : il ne doit ressortir nulle part.
CANARY = "canari-metadonnees-4b7e"
App = Callable[[Scope, Receive, Send], Awaitable[None]]


@dataclass
class Protected:
    server: ProtectedMcpServer
    upstream_url: str
    proxy: McpProxy
    proxy_url: str
    sink: MemorySink


@asynccontextmanager
async def protected(*, preserve_host: bool = True) -> AsyncIterator[Protected]:
    """Le serveur protégé, et le proxy devant lui ; le serveur se croit publié sous son nom."""
    server = ProtectedMcpServer()
    sink = MemorySink()
    async with running(server) as upstream_url:
        settings = ProxySettings.create(
            upstream_url,
            "meteo",
            preserve_host=preserve_host,
            allowed_hosts=(RESOURCE_HOST,),
            fingerprint_key=FingerprintKey(bytes(range(32))),
        )
        proxy = McpProxy(settings, sink)
        async with running(proxy, transparent=True) as proxy_url:
            yield Protected(server, upstream_url, proxy, proxy_url, sink)


def as_the_server(token: str | None = None) -> list[tuple[str, str]]:
    """Les en-têtes d'un client qui joint le serveur sous son nom, ici à travers le proxy."""
    headers = [("host", RESOURCE_HOST), *MODERN_HEADERS]
    if token is not None:
        headers.append(("authorization", f"Bearer {token}"))
    return headers


async def test_in_transparent_mode_the_token_of_the_client_passes_untouched_and_is_accepted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    token = forge(RESOURCE, subject="canari-sujet-91d3")
    with caplog.at_level(logging.DEBUG):
        async with protected() as p:
            answered = await exchange(
                "POST", p.proxy_url + "/mcp", headers=as_the_server(token), content=CALL
            )
            await p.proxy.drain()

    assert (answered.status, answered.body) == (200, RESULT)
    # Le serveur a reçu le jeton du client, octet pour octet, sous son propre nom.
    assert p.server.accepted == [f"Bearer {token}".encode()]
    assert p.server.hosts == [RESOURCE_HOST.encode()]
    assert p.server.refusals == []
    (event,) = p.sink.events
    assert event["eventType"] == "COMPLETE"
    # Le proxy n'en garde rien : ni le jeton, ni sa signature, ni ce qu'il dit.
    signature = token.rsplit(".", 1)[1]
    for place in (json.dumps(event), repr(p.proxy.stats), caplog.text):
        for trace in (token, signature, "canari-sujet-91d3"):
            assert trace not in place


async def test_in_transparent_mode_the_challenge_and_the_metadata_designate_the_url_used() -> None:
    async with protected() as p:
        challenged = await exchange(
            "POST", p.proxy_url + "/mcp", headers=as_the_server(), content=CALL
        )
        metadata = await exchange(
            "GET", p.proxy_url + METADATA_PATH, headers=[("host", RESOURCE_HOST)]
        )
        refused = await exchange(
            "POST",
            p.proxy_url + "/mcp",
            headers=as_the_server(forge("https://ailleurs.exemple/mcp")),
            content=CALL,
        )

    assert challenged.status == 401
    assert challenged.header("www-authenticate") == [CHALLENGE]
    # Les métadonnées passent telles quelles et désignent l'URL que le client a jointe.
    assert (metadata.status, metadata.body) == (200, METADATA)
    assert json.loads(metadata.body)["resource"] == f"https://{RESOURCE_HOST}/mcp"
    # Le serveur vérifie bien l'audience : un jeton pour une autre ressource est refusé.
    assert refused.status == 401
    assert p.server.refusals == ["missing", "audience"]


async def test_through_a_gateway_a_token_bound_to_the_url_of_the_server_cannot_pass() -> None:
    async with protected(preserve_host=False) as p:
        joined = p.proxy_url + "/mcp"
        # Le client joint le proxy à sa propre URL : les métadonnées relayées désignent une
        # autre ressource, qu'il doit écarter (RFC 9728, section 3.3).
        metadata = await exchange("GET", p.proxy_url + METADATA_PATH)
        assert json.loads(metadata.body)["resource"] != joined
        # Le jeton qu'il obtiendrait pour l'URL qu'il joint (RFC 8707) est refusé.
        refused = await exchange(
            "POST",
            joined,
            headers=[*MODERN_HEADERS, ("authorization", f"Bearer {forge(joined)}")],
            content=CALL,
        )

        assert refused.status == 401
        assert p.server.refusals == ["audience"]
        # Et la configuration elle-même est refusée, avec son explication : le proxy lit les
        # métadonnées à l'adresse du point MCP du serveur.
        gateway = ProxySettings.create(p.upstream_url + "/mcp", "meteo", preserve_host=False)
        with pytest.raises(ProxyConfigurationError) as raised:
            await check_gateway(gateway)
    message = str(raised.value)
    assert message == GATEWAY_REFUSED_BY_METADATA
    for value in (RESOURCE_HOST, AUTHORIZATION_SERVER, p.upstream_url):
        assert value not in message


async def test_the_gateway_mode_is_refused_when_the_operator_declares_bound_tokens() -> None:
    with pytest.raises(ProxyConfigurationError) as raised:
        ProxySettings.create(
            "http://127.0.0.1:9000", preserve_host=False, audience_bound_tokens=True
        )
    assert str(raised.value) == GATEWAY_REFUSED_BY_DECLARATION
    assert "mode transparent" in str(raised.value)
    # Le mode transparent, lui, sert un tel serveur ; la passerelle, un serveur sans jetons liés.
    assert ProxySettings.create("http://h", audience_bound_tokens=True).mode == "transparent"
    assert ProxySettings.create("http://h", preserve_host=False).mode == "gateway"


async def test_in_transparent_mode_nothing_is_read_from_the_server_at_start() -> None:
    server = ProtectedMcpServer()
    async with running(server) as upstream_url:
        assert await check_gateway(ProxySettings.create(upstream_url, "meteo")) is None
    assert server.hosts == []


async def test_the_metadata_are_read_without_credentials_nor_any_added_header() -> None:
    seen: list[list[tuple[bytes, bytes]]] = []
    server = ProtectedMcpServer()

    async def recording(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            seen.append([(bytes(name), bytes(value)) for name, value in scope["headers"]])
        await server(scope, receive, send)

    async with running(recording) as upstream_url:
        reading = await read_metadata(httpx.URL(upstream_url + "/mcp"))
    assert reading == MetadataReading("bound", "resource_declared")
    ((headers),) = seen
    assert sorted(name for name, _ in headers) == [b"accept", b"accept-encoding", b"host"]
    assert (b"accept-encoding", b"identity") in headers


def answering(
    status: int,
    body: bytes = b"",
    headers: list[tuple[bytes, bytes]] | None = None,
    *,
    at: str | None = None,
) -> tuple[App, list[str]]:
    """Une application qui répond ainsi, à l'adresse ``at`` seulement (404 ailleurs) ; elle
    note les chemins demandés."""
    asked: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        asked.append(scope["path"])
        found = at is None or scope["path"] == at
        answer = (status, body, headers or []) if found else (404, b"", [])
        await send(
            {
                "type": "http.response.start",
                "status": answer[0],
                "headers": [*answer[2], (b"content-length", str(len(answer[1])).encode())],
            }
        )
        await send({"type": "http.response.body", "body": answer[1], "more_body": False})

    return app, asked


JSON = [(b"content-type", b"application/json; charset=utf-8")]


async def test_a_server_without_metadata_is_asked_at_both_addresses_then_said_absent() -> None:
    app, asked = answering(404)
    async with running(app) as upstream_url:
        settings = ProxySettings.create(upstream_url + "/mcp", "meteo", preserve_host=False)
        reading = await check_gateway(settings)
    assert reading == MetadataReading("absent", "not_published")
    assert asked == [
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/oauth-protected-resource",
    ]


async def test_metadata_published_at_the_root_also_bind_the_tokens() -> None:
    app, asked = answering(200, METADATA, JSON, at="/.well-known/oauth-protected-resource")
    async with running(app) as upstream_url:
        reading = await read_metadata(httpx.URL(upstream_url + "/mcp/"))
    assert reading == MetadataReading("bound", "resource_declared")
    assert len(asked) == 2


def document(text: str) -> bytes:
    return text.replace("CANARI", CANARY).encode()


@pytest.mark.parametrize(
    ("status", "body", "headers", "reason"),
    [
        (500, document('{"error":"CANARI"}'), JSON, "status"),
        (401, document('{"error":"CANARI"}'), JSON, "status"),
        (200, document("<p>CANARI</p>"), [(b"content-type", b"text/html")], "not_json"),
        (
            200,
            gzip.compress(document('{"resource":"https://CANARI/"}')),
            [*JSON, (b"content-encoding", b"gzip")],
            "encoded",
        ),
        (
            200,
            document('{"resource":"https://CANARI/"' + " " * MAX_METADATA_BYTES + "}"),
            JSON,
            "too_large",
        ),
        (200, document('{"resource":"https://a/","resource":"https://CANARI/"}'), JSON, "not_json"),
        (200, document('{"resource":"https://CANARI/","n":NaN}'), JSON, "not_json"),
        (200, document("[" * 30_000 + "]" * 30_000), JSON, "not_json"),
        # Une profondeur que tout interpréteur lit sans erreur est refusée quand même.
        (
            200,
            document('{"resource":' + "[" * 40 + '"https://CANARI/"' + "]" * 40 + "}"),
            JSON,
            "not_json",
        ),
        (
            200,
            document('{"resource":"ftp://CANARI/' + "[" * 40 + '"}'),
            JSON,
            "invalid_document",
        ),
        (200, b'{"resource":"https://\xff/"}', JSON, "not_json"),
        (200, document('["https://CANARI/"]'), JSON, "invalid_document"),
        (200, document('{"authorization_servers":["https://CANARI/"]}'), JSON, "invalid_document"),
        (200, document('{"resource":"CANARI sans schema"}'), JSON, "invalid_document"),
        (200, document('{"resource":"ftp://CANARI/"}'), JSON, "invalid_document"),
        (200, document('{"resource":"https://CANARI:port/"}'), JSON, "invalid_document"),
        (200, document('{"resource":"https://CANARI/mcp#fragment"}'), JSON, "invalid_document"),
        (200, document('{"resource":["https://CANARI/"]}'), JSON, "invalid_document"),
        (
            200,
            document('{"resource":"https://CANARI/' + "a" * MAX_RESOURCE_LENGTH + '"}'),
            JSON,
            "invalid_document",
        ),
    ],
)
async def test_a_hostile_metadata_answer_says_nothing_and_is_never_copied(
    status: int,
    body: bytes,
    headers: list[tuple[bytes, bytes]],
    reason: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app, _ = answering(status, body, headers)
    async with running(app) as upstream_url:
        settings = ProxySettings.create(upstream_url, "meteo", preserve_host=False)
        with caplog.at_level(logging.DEBUG):
            reading = await check_gateway(settings)
    # Rien de sûr : la passerelle démarre, et le journal dit le motif par son code.
    assert reading == MetadataReading("unknown", reason)
    assert f"illisibles ({reason})" in caplog.text
    assert CANARY not in caplog.text


async def test_a_redirect_of_the_metadata_is_not_followed() -> None:
    target, reached = answering(200, METADATA, JSON)
    async with running(target) as elsewhere:
        app, _ = answering(307, b"", [(b"location", f"{elsewhere}/metadonnees".encode())])
        async with running(app) as upstream_url:
            reading = await read_metadata(httpx.URL(upstream_url))
    assert reading == MetadataReading("unknown", "redirect")
    assert reached == []


async def test_a_server_that_never_answers_is_given_up_within_the_deadline() -> None:
    async def silent(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            await asyncio.Event().wait()

    async with running(silent) as upstream_url:
        reading = await asyncio.wait_for(
            read_metadata(httpx.URL(upstream_url), deadline=0.3), timeout=5
        )
    assert reading == MetadataReading("unknown", "timeout")


async def test_an_unreachable_server_says_nothing_and_the_gateway_starts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app, _ = answering(404)
    async with running(app) as upstream_url:
        pass
    # Le serveur s'est arrêté : son port refuse la connexion.
    settings = ProxySettings.create(upstream_url, "meteo", preserve_host=False)
    with caplog.at_level(logging.INFO):
        reading = await check_gateway(settings)
    assert reading == MetadataReading("unknown", "unreachable")
    assert "--audience-bound-tokens" in caplog.text


async def test_the_command_line_refuses_the_gateway_before_serving_with_the_explanation() -> None:
    async with running(ProtectedMcpServer()) as upstream_url:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "answerlineage_proxy.cli",
            "--upstream",
            upstream_url + "/mcp",
            "--no-preserve-host",
            "--port",
            "0",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={k: v for k, v in os.environ.items() if not k.startswith("ANSWERLINEAGE_")},
        )
        _, error = await asyncio.wait_for(process.communicate(), 60)
    assert process.returncode == 2
    assert error.decode().endswith(f"Configuration invalide : {GATEWAY_REFUSED_BY_METADATA}\n")


def test_the_command_line_refuses_a_declared_gateway_without_reading_anything(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def never(*_: object, **__: object) -> MetadataReading:
        raise AssertionError("métadonnées lues")

    monkeypatch.setattr("answerlineage_proxy.resource.read_metadata", never)
    monkeypatch.setattr(
        "answerlineage_proxy.lifecycle.run", lambda app, settings: pytest.fail("démarré")
    )
    arguments = [
        "--upstream",
        "http://127.0.0.1:9",
        "--no-preserve-host",
        "--audience-bound-tokens",
    ]
    assert cli.main(arguments) == 2
    assert capsys.readouterr().err == f"Configuration invalide : {GATEWAY_REFUSED_BY_DECLARATION}\n"


@pytest.mark.parametrize(
    ("reading", "said"),
    [
        (MetadataReading("absent", "not_published"), "ne publie pas de métadonnées"),
        (MetadataReading("unknown", "timeout"), "illisibles (timeout)"),
    ],
)
def test_a_gateway_whose_tokens_are_not_known_bound_starts_in_gateway_mode(
    reading: MetadataReading,
    said: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def read(*_: object, **__: object) -> MetadataReading:
        return reading

    served: list[ProxySettings] = []

    def run(app: McpProxy, settings: ProxySettings) -> int:
        served.append(settings)
        return 0

    monkeypatch.setattr("answerlineage_proxy.resource.read_metadata", read)
    monkeypatch.setattr("answerlineage_proxy.lifecycle.run", run)
    caplog.set_level(logging.INFO)
    assert cli.main(["--upstream", "http://127.0.0.1:9", "--no-preserve-host"]) == 0
    (settings,) = served
    assert settings.mode == "gateway"
    assert said in caplog.text
    assert "mode passerelle." in caplog.text


def test_the_transparent_mode_is_said_at_start(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def never(*_: object, **__: object) -> MetadataReading:
        raise AssertionError("métadonnées lues")

    monkeypatch.setattr("answerlineage_proxy.resource.read_metadata", never)
    monkeypatch.setattr("answerlineage_proxy.lifecycle.run", lambda app, settings: 0)
    caplog.set_level(logging.INFO)
    assert cli.main(["--upstream", "http://127.0.0.1:9", "--audience-bound-tokens"]) == 0
    assert "mode transparent." in caplog.text
