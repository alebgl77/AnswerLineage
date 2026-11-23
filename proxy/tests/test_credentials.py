"""Le jeton du proxy envers l'API : octroi client_credentials, en mémoire, renouvelé.

L'émetteur OIDC est simulé par un transport HTTP en mémoire : sa découverte et
son point de jeton. La boîte d'envoi et la synchronisation des quarantaines
portent le jeton ; une panne de l'émetteur ne retarde aucun appel.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote_plus

import httpx
import pytest

from answerlineage_proxy import cli
from answerlineage_proxy.app import McpProxy
from answerlineage_proxy.config import ProxyConfigurationError, ProxySettings
from answerlineage_proxy.credentials import (
    COOL_DOWN,
    ENV_AUDIENCE,
    ENV_CLIENT_ID,
    ENV_CLIENT_SECRET,
    ENV_CLIENT_SECRET_FILE,
    ENV_ISSUER,
    ENV_SCOPE,
    MAX_RESPONSE_BYTES,
    ClientCredentials,
    ClientCredentialsTokens,
    TokenUnavailableError,
    client_secret,
)
from answerlineage_proxy.outbox import Outbox, OutboxSettings
from answerlineage_proxy.quarantine import QuarantineSettings, QuarantineSync
from proxy.tests.conftest import FakeMcpServer, exchange, running, tool_call

pytestmark = pytest.mark.anyio

ISSUER = "https://idp.exemple.fr/realms/lignage"
CLIENT = "proxy-entrepot"
# Canaris : un secret et des jetons inventés, cherchés ensuite dans les journaux.
SECRET = "canari-secret-d41f &=:"  # noqa: S105 — canari de test, pas un secret.
TOKEN = "canari-jeton-proxy-1"  # noqa: S105 — canari de test, pas un secret.
API = "https://lignage.exemple.fr"


@dataclass
class Clock:
    now: float = 1000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class FakeIssuer:
    """L'émetteur simulé : sa découverte, puis des jetons numérotés, ou les réponses prévues."""

    discovery: dict[str, Any] = field(
        default_factory=lambda: {"issuer": ISSUER, "token_endpoint": ISSUER + "/token"}
    )
    replies: list[httpx.Response | BaseException] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)
    issued: int = 0
    lifetime: int | None = 300

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=self.discovery)
        if self.replies:
            reply = self.replies.pop(0)
            if isinstance(reply, BaseException):
                raise reply
            return reply
        self.issued += 1
        answer: dict[str, Any] = {"access_token": f"{TOKEN}-{self.issued}", "token_type": "Bearer"}
        if self.lifetime is not None:
            answer["expires_in"] = self.lifetime
        return httpx.Response(200, json=answer)

    @property
    def token_requests(self) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.path.endswith("/token")]


def credentials(**changes: Any) -> ClientCredentials:
    return ClientCredentials.create(ISSUER, CLIENT, SECRET, **changes)


def tokens_for(issuer: FakeIssuer, clock: Clock | None = None, **changes: Any) -> Any:
    return ClientCredentialsTokens(
        credentials(**changes), transport=httpx.MockTransport(issuer), clock=clock or Clock()
    )


async def test_the_token_comes_from_the_discovered_endpoint_by_client_credentials() -> None:
    issuer = FakeIssuer()
    assert await tokens_for(issuer).token() == f"{TOKEN}-1"
    discovery, granted = issuer.requests
    assert discovery.url == httpx.URL(ISSUER + "/.well-known/openid-configuration")
    assert granted.method == "POST"
    assert granted.url == httpx.URL(ISSUER + "/token")
    assert parse_qs(granted.content.decode()) == {"grant_type": ["client_credentials"]}
    # client_secret_basic : identifiant et secret encodés en formulaire, puis en base64.
    scheme, encoded = granted.headers["authorization"].split(" ")
    user, _, password = base64.b64decode(encoded).decode().partition(":")
    assert scheme == "Basic"
    assert (unquote_plus(user), unquote_plus(password)) == (CLIENT, SECRET)


async def test_scope_and_audience_are_asked_when_set() -> None:
    issuer = FakeIssuer()
    await tokens_for(issuer, scope="api://lignage/.default", audience="answerlineage").token()
    (granted,) = issuer.token_requests
    assert parse_qs(granted.content.decode()) == {
        "grant_type": ["client_credentials"],
        "scope": ["api://lignage/.default"],
        "audience": ["answerlineage"],
    }


async def test_the_token_is_kept_in_memory_then_renewed_before_it_expires() -> None:
    issuer, clock = FakeIssuer(lifetime=300), Clock()
    tokens = tokens_for(issuer, clock)
    assert await tokens.token() == f"{TOKEN}-1"
    clock.now += 239
    assert await tokens.token() == f"{TOKEN}-1"
    # Une minute avant l'expiration, un nouveau jeton est demandé ; la découverte est gardée.
    clock.now += 1
    assert await tokens.token() == f"{TOKEN}-2"
    assert len(issuer.token_requests) == 2
    assert len(issuer.requests) == 3


async def test_a_short_lived_token_is_renewed_at_four_fifths_of_its_life() -> None:
    issuer, clock = FakeIssuer(lifetime=10), Clock()
    tokens = tokens_for(issuer, clock)
    await tokens.token()
    clock.now += 7.9
    assert await tokens.token() == f"{TOKEN}-1"
    clock.now += 0.1
    assert await tokens.token() == f"{TOKEN}-2"


async def test_without_announced_lifetime_the_token_lives_five_minutes() -> None:
    issuer, clock = FakeIssuer(lifetime=None), Clock()
    tokens = tokens_for(issuer, clock)
    await tokens.token()
    clock.now += 239
    assert await tokens.token() == f"{TOKEN}-1"
    clock.now += 1
    assert await tokens.token() == f"{TOKEN}-2"


async def test_an_invalidated_token_is_replaced() -> None:
    issuer = FakeIssuer()
    tokens = tokens_for(issuer)
    await tokens.token()
    tokens.invalidate()
    assert await tokens.token() == f"{TOKEN}-2"


async def test_concurrent_callers_share_one_request_to_the_issuer() -> None:
    issuer = FakeIssuer()
    tokens = tokens_for(issuer)
    found = await asyncio.gather(*(tokens.token() for _ in range(5)))
    assert set(found) == {f"{TOKEN}-1"}
    assert len(issuer.token_requests) == 1


def granted(token: str, kind: str = "Bearer", lifetime: object = 60) -> httpx.Response:
    """Une réponse du point de jeton, telle que l'émetteur la donnerait."""
    return httpx.Response(
        200, json={"access_token": token, "token_type": kind, "expires_in": lifetime}
    )


@pytest.mark.parametrize(
    ("discovery", "reply"),
    [
        ({"issuer": "https://ailleurs.exemple.fr", "token_endpoint": ISSUER + "/token"}, None),
        ({"issuer": ISSUER, "token_endpoint": "http://idp.exemple.fr/token"}, None),
        ({"issuer": ISSUER, "token_endpoint": "https://u:p@idp.exemple.fr/token"}, None),
        ({"issuer": ISSUER}, None),
        (None, httpx.Response(401, json={"error": "invalid_client"})),
        (None, granted(TOKEN, "mac")),
        (None, granted("jeton avec espace")),
        (None, granted("x" * 20_000)),
        (None, granted(TOKEN, lifetime="60")),
        (None, granted(TOKEN, lifetime=0)),
        (None, httpx.Response(200, content=b"[]")),
        (None, httpx.Response(200, content=b"pas du json")),
        (None, httpx.Response(200, content=b" " * (MAX_RESPONSE_BYTES + 1))),
        (None, httpx.ConnectError("refus")),
    ],
    ids=[
        "autre émetteur",
        "point de jeton en http",
        "point de jeton avec identifiant",
        "sans point de jeton",
        "client refusé",
        "jeton d'un autre type",
        "jeton illisible",
        "jeton démesuré",
        "durée de vie en texte",
        "durée de vie nulle",
        "réponse qui n'est pas un objet",
        "réponse illisible",
        "réponse trop grande",
        "émetteur injoignable",
    ],
)
async def test_an_issuer_that_does_not_give_a_proper_token_gives_none(
    discovery: dict[str, Any] | None,
    reply: httpx.Response | BaseException | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    issuer = FakeIssuer(replies=[] if reply is None else [reply])
    if discovery is not None:
        issuer.discovery = discovery
    tokens = tokens_for(issuer)
    with pytest.raises(TokenUnavailableError), caplog.at_level(logging.DEBUG):
        await tokens.token()
    assert tokens.failures == 1
    for secret in (SECRET, TOKEN, ISSUER, CLIENT, "idp.exemple.fr"):
        assert secret not in caplog.text


async def test_after_a_failure_the_issuer_is_not_asked_again_before_a_delay() -> None:
    issuer, clock = FakeIssuer(replies=[httpx.Response(503)]), Clock()
    tokens = tokens_for(issuer, clock)
    with pytest.raises(TokenUnavailableError):
        await tokens.token()
    clock.now += COOL_DOWN - 0.1
    with pytest.raises(TokenUnavailableError):
        await tokens.token()
    assert len(issuer.token_requests) == 1
    clock.now += 0.1
    assert await tokens.token() == f"{TOKEN}-1"
    # La découverte est refaite après un échec : l'émetteur a pu changer de point de jeton.
    assert len(issuer.requests) == 4


async def test_a_black_hole_issuer_is_cut_by_the_request_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("answerlineage_proxy.credentials.REQUEST_TIMEOUT", 0.05)

    async def silent(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)
        raise AssertionError("jamais atteint")  # pragma: no cover

    tokens = ClientCredentialsTokens(credentials(), transport=httpx.MockTransport(silent))
    with pytest.raises(TokenUnavailableError):
        await tokens.token()


def test_the_secret_is_never_shown() -> None:
    held = credentials()
    tokens = ClientCredentialsTokens(held)
    assert SECRET not in repr(held)
    assert SECRET not in repr(tokens)


# Les réglages.


@pytest.mark.parametrize(
    ("issuer", "client", "secret"),
    [
        ("http://idp.exemple.fr", CLIENT, SECRET),
        ("ftp://idp.exemple.fr", CLIENT, SECRET),
        ("https://u:p@idp.exemple.fr", CLIENT, SECRET),
        ("https://idp.exemple.fr?x=1", CLIENT, SECRET),
        ("https://", CLIENT, SECRET),
        ("https://idp.exemple.fr", "client\nfaux", SECRET),
        ("https://idp.exemple.fr", CLIENT, ""),
        ("https://idp.exemple.fr", CLIENT, "secret\x00nul"),
    ],
    ids=[
        "http hors boucle",
        "autre schéma",
        "identifiant dans l'adresse",
        "paramètres",
        "sans hôte",
        "client illisible",
        "secret vide",
        "secret illisible",
    ],
)
def test_an_unusable_setting_is_refused_without_being_cited(
    issuer: str, client: str, secret: str
) -> None:
    with pytest.raises(ProxyConfigurationError) as refused:
        ClientCredentials.create(issuer, client, secret)
    for value in (issuer, client, secret):
        if value:
            assert value not in str(refused.value)


def test_an_issuer_on_the_loopback_may_be_reached_in_http() -> None:
    held = ClientCredentials.create("http://127.0.0.1:8080/", CLIENT, SECRET)
    assert held.issuer == "http://127.0.0.1:8080"


def test_the_secret_comes_from_the_environment_or_a_file(tmp_path: Path) -> None:
    held = tmp_path / "secret"
    held.write_text(SECRET + "\n", encoding="utf-8")
    assert client_secret({}) is None
    assert client_secret({ENV_CLIENT_SECRET: SECRET}) == SECRET
    assert client_secret({ENV_CLIENT_SECRET_FILE: str(held)}) == SECRET
    assert client_secret({}, str(held)) == SECRET


def test_an_unusable_secret_source_is_refused_without_being_cited(tmp_path: Path) -> None:
    big, binary = tmp_path / "grand", tmp_path / "binaire"
    big.write_bytes(b"x" * 5000)
    binary.write_bytes(b"\xff\xfe")
    for environment, secret_file in (
        ({ENV_CLIENT_SECRET: SECRET, ENV_CLIENT_SECRET_FILE: str(big)}, None),
        ({}, str(tmp_path / "absent")),
        ({}, str(big)),
        ({}, str(binary)),
    ):
        with pytest.raises(ProxyConfigurationError) as refused:
            client_secret(environment, secret_file)
        assert SECRET not in str(refused.value)
        assert str(tmp_path) not in str(refused.value)


def test_the_proxy_identity_is_set_by_option_or_environment(tmp_path: Path) -> None:
    held = tmp_path / "secret"
    held.write_text(SECRET, encoding="utf-8")
    outbox = OutboxSettings.create(API)
    assert cli.credentials_from_args([], {}, outbox) is None
    by_options = cli.credentials_from_args(
        [
            "--oidc-issuer",
            ISSUER,
            "--oidc-client-id",
            CLIENT,
            "--oidc-client-secret-file",
            str(held),
            "--oidc-scope",
            "lignage",
            "--oidc-audience",
            "answerlineage",
        ],
        {},
        outbox,
    )
    by_environment = cli.credentials_from_args(
        [],
        {
            ENV_ISSUER: ISSUER,
            ENV_CLIENT_ID: CLIENT,
            ENV_CLIENT_SECRET: SECRET,
            ENV_SCOPE: "lignage",
            ENV_AUDIENCE: "answerlineage",
        },
        outbox,
    )
    expected = credentials(scope="lignage", audience="answerlineage")
    assert by_options == by_environment == expected


def test_the_secret_itself_is_never_taken_from_the_command_line() -> None:
    """Seul le chemin d'un fichier passe par la ligne de commande : une valeur donnée là
    n'est jamais prise pour le secret, et le refus ne la cite pas."""
    with pytest.raises(ProxyConfigurationError) as refused:
        cli.credentials_from_args(
            ["--oidc-issuer", ISSUER, "--oidc-client-id", CLIENT, "--oidc-client-secret", SECRET],
            {},
            OutboxSettings.create(API),
        )
    assert SECRET not in str(refused.value)


@pytest.mark.parametrize(
    ("environment", "api"),
    [
        ({ENV_ISSUER: ISSUER}, API),
        ({ENV_ISSUER: ISSUER, ENV_CLIENT_ID: CLIENT}, API),
        ({ENV_CLIENT_SECRET: SECRET}, API),
        ({ENV_ISSUER: ISSUER, ENV_CLIENT_ID: CLIENT, ENV_CLIENT_SECRET: SECRET}, None),
        (
            {ENV_ISSUER: ISSUER, ENV_CLIENT_ID: CLIENT, ENV_CLIENT_SECRET: SECRET},
            "http://lignage.exemple.fr",
        ),
    ],
    ids=["émetteur seul", "sans secret", "secret seul", "sans API", "API en http"],
)
def test_an_incomplete_or_unsafe_identity_is_refused(
    environment: dict[str, str], api: str | None
) -> None:
    outbox = None if api is None else OutboxSettings.create(api)
    with pytest.raises(ProxyConfigurationError) as refused:
        cli.credentials_from_args([], environment, outbox)
    assert SECRET not in str(refused.value)


def test_an_api_on_the_loopback_may_receive_the_token_in_http() -> None:
    outbox = OutboxSettings.create("http://127.0.0.1:8000")
    environment = {ENV_ISSUER: ISSUER, ENV_CLIENT_ID: CLIENT, ENV_CLIENT_SECRET: SECRET}
    assert cli.credentials_from_args([], environment, outbox) == credentials()


def test_without_identity_the_proxy_calls_the_api_without_a_token() -> None:
    assert cli.token_source(None) is None
    assert isinstance(cli.token_source(credentials()), ClientCredentialsTokens)


def test_an_incomplete_identity_stops_the_proxy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(ENV_CLIENT_SECRET, SECRET)
    assert cli.main(["--upstream", "http://127.0.0.1:9", "--api-url", API]) == 2
    assert SECRET not in capsys.readouterr().err


# Les envois à l'API portent le jeton.


@dataclass
class FakeTokens:
    """Des jetons à la demande : ``down`` simule un émetteur en panne."""

    down: bool = False
    issued: int = 0
    invalidated: int = 0
    asked: list[float] = field(default_factory=list)

    async def token(self) -> str:
        self.asked.append(asyncio.get_running_loop().time())
        if self.down:
            raise TokenUnavailableError("panne simulée")
        if self.issued == self.invalidated:
            self.issued += 1
        return f"{TOKEN}-{self.issued}"

    def invalidate(self) -> None:
        self.invalidated = self.issued


@dataclass
class Recorded:
    """L'API simulée : elle note l'en-tête d'autorisation de chaque requête."""

    statuses: list[int] = field(default_factory=list)
    seen: list[str | None] = field(default_factory=list)

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request.headers.get("authorization"))
        status = self.statuses.pop(0) if self.statuses else None
        if request.url.path.endswith("/quarantine"):
            if status is not None:
                return httpx.Response(status)
            return httpx.Response(
                200,
                json={"version": "a" * 64, "quarantines": []},
                headers={"content-type": "application/json"},
            )
        if status is not None:
            return httpx.Response(status)
        if request.url.path.endswith("/coverage"):
            return httpx.Response(201, json={})
        return httpx.Response(202, json={"accepted": 1, "duplicates": 0, "rejected": []})


@asynccontextmanager
async def box(api: Recorded, tokens: FakeTokens) -> AsyncIterator[Outbox]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(api))
    outbox = Outbox(
        OutboxSettings.create(API, flush_seconds=0),
        client=client,
        backoff=(0.01, 0.02),
        tokens=tokens,
    )
    await outbox.start()
    try:
        yield outbox
    finally:
        await outbox.aclose()
        await client.aclose()


def event() -> dict[str, Any]:
    return {"eventTime": "2026-11-11T09:00:00.000000Z", "run": {"runId": "r"}}


async def until(condition: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "condition jamais remplie"
        await asyncio.sleep(0.005)


async def test_the_outbox_sends_events_and_reports_with_the_token() -> None:
    api, tokens = Recorded(), FakeTokens()
    async with box(api, tokens) as outbox:
        outbox.report_coverage({"emitter": "mcp://x", "window": {"from": "a", "to": "b"}})
        outbox.emit(event())
        await until(lambda: outbox.stats.sent == 1 and outbox.stats.reports_sent == 1)
    assert api.seen == [f"Bearer {TOKEN}-1", f"Bearer {TOKEN}-1"]


async def test_taking_an_event_in_charge_never_asks_for_a_token() -> None:
    api, tokens = Recorded(), FakeTokens()
    client = httpx.AsyncClient(transport=httpx.MockTransport(api))
    outbox = Outbox(OutboxSettings.create(API, flush_seconds=0), client=client, tokens=tokens)
    for _ in range(100):
        outbox.emit(event())
    assert tokens.asked == []
    await outbox.aclose()
    await client.aclose()


async def test_events_wait_in_the_outbox_while_the_issuer_is_down(
    caplog: pytest.LogCaptureFixture,
) -> None:
    api, tokens = Recorded(), FakeTokens(down=True)
    with caplog.at_level(logging.DEBUG):
        async with box(api, tokens) as outbox:
            outbox.emit(event())
            await until(lambda: len(tokens.asked) >= 3)
            assert (api.seen, outbox.queued, outbox.stats.sent) == ([], 1, 0)
            tokens.down = False
            await until(lambda: outbox.stats.sent == 1)
    assert api.seen == [f"Bearer {TOKEN}-1"]
    assert "jeton indisponible" in caplog.text
    assert TOKEN not in caplog.text


async def test_a_token_refused_by_the_api_is_replaced_and_the_batch_sent_again() -> None:
    api, tokens = Recorded(statuses=[401]), FakeTokens()
    async with box(api, tokens) as outbox:
        outbox.emit(event())
        await until(lambda: outbox.stats.sent == 1)
    assert api.seen == [f"Bearer {TOKEN}-1", f"Bearer {TOKEN}-2"]
    assert outbox.stats.rejected == 0


async def test_a_report_waits_for_the_token_too() -> None:
    api, tokens = Recorded(statuses=[401]), FakeTokens(down=True)
    async with box(api, tokens) as outbox:
        outbox.report_coverage({"emitter": "mcp://x", "window": {"from": "a", "to": "b"}})
        await until(lambda: len(tokens.asked) >= 2)
        tokens.down = False
        await until(lambda: outbox.stats.reports_sent == 1)
    assert api.seen == [f"Bearer {TOKEN}-1", f"Bearer {TOKEN}-2"]
    assert outbox.stats.reports_dropped == 0


async def test_the_quarantine_sync_carries_the_token() -> None:
    api, tokens = Recorded(statuses=[401]), FakeTokens()
    client = httpx.AsyncClient(transport=httpx.MockTransport(api))
    sync = QuarantineSync(QuarantineSettings.create(httpx.URL(API)), client=client, tokens=tokens)
    try:
        assert not await sync.sync_once()
        assert await sync.sync_once()
    finally:
        await sync.aclose()
        await client.aclose()
    assert api.seen == [f"Bearer {TOKEN}-1", f"Bearer {TOKEN}-2"]
    assert sync.status().mode == "synced"


async def test_without_token_the_quarantine_sync_keeps_observing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    api, tokens = Recorded(), FakeTokens(down=True)
    client = httpx.AsyncClient(transport=httpx.MockTransport(api))
    sync = QuarantineSync(QuarantineSettings.create(httpx.URL(API)), client=client, tokens=tokens)
    with caplog.at_level(logging.DEBUG):
        try:
            assert not await sync.sync_once()
        finally:
            await sync.aclose()
            await client.aclose()
    assert api.seen == []
    assert sync.status().mode == "observe"
    assert "jeton indisponible" in caplog.text


async def test_an_issuer_that_hangs_never_delays_a_relayed_call() -> None:
    """La capture reste ouverte : l'appel relayé passe pendant que la boîte attend son jeton."""
    hanging = asyncio.Event()

    @dataclass
    class HangingTokens:
        def invalidate(self) -> None:  # pragma: no cover — jamais refusé ici.
            pass

        async def token(self) -> str:
            hanging.set()
            await asyncio.sleep(30)
            raise AssertionError("jamais atteint")  # pragma: no cover

    upstream = FakeMcpServer()
    outbox = Outbox(OutboxSettings.create(API, flush_seconds=0), tokens=HangingTokens())
    async with running(upstream) as upstream_url:
        proxy = McpProxy(ProxySettings.create(upstream_url, "meteo"), outbox)
        async with running(proxy, transparent=True) as proxy_url:
            # Un premier appel lance l'envoi, qui reste suspendu à l'émetteur.
            await exchange("POST", f"{proxy_url}/mcp", content=tool_call("get_weather", {}))
            await asyncio.wait_for(hanging.wait(), 5)
            started = asyncio.get_running_loop().time()
            relayed = await exchange(
                "POST", f"{proxy_url}/mcp", content=tool_call("get_weather", {})
            )
            elapsed = asyncio.get_running_loop().time() - started
            waiting = outbox.queued
    assert relayed.status == 200
    assert elapsed < 1.0
    # Les deux événements attendent le jeton dans la boîte ; l'arrêt les compte perdus.
    assert waiting == 2
    assert outbox.stats.dropped_shutdown == 2
