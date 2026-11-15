"""Le proxy refuse une requête dont l'en-tête Host ou Origin n'est pas attendu.

Une page web ouverte sur le poste peut viser un service local : par
réassociation DNS, un nom d'hôte de l'attaquant finit par désigner la boucle
locale. Le navigateur envoie alors ce nom dans Host, et l'adresse de la page
dans Origin.
"""

from __future__ import annotations

import logging

import pytest

from answerlineage_proxy import cli
from answerlineage_proxy.config import ProxyConfigurationError, ProxySettings
from answerlineage_proxy.guard import LOOPBACK_HOSTS, RequestGuard
from proxy.tests.conftest import (
    MODERN_HEADERS,
    MODERN_META,
    TOOL_RESULT,
    BenchFactory,
    exchange,
    tool_call,
)

GUARD = RequestGuard.create([*LOOPBACK_HOSTS, "mcp.exemple.fr"], ["https://console.exemple.fr"])


def headers(
    host: str | None = "127.0.0.1:8717", origin: str | None = None
) -> list[tuple[bytes, bytes]]:
    items = [(b"content-type", b"application/json")]
    if host is not None:
        items.append((b"Host", host.encode("latin-1")))
    if origin is not None:
        items.append((b"Origin", origin.encode("latin-1")))
    return items


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1:8717",
        "127.0.0.1",
        "localhost:8717",
        "LocalHost:8717",
        "localhost.:8717",
        "[::1]:8717",
        "mcp.exemple.fr",
        "MCP.exemple.fr:443",
    ],
)
def test_expected_host_is_accepted(host: str) -> None:
    assert GUARD.refusal(headers(host)) is None


@pytest.mark.parametrize(
    "host",
    [
        "attaquant.example:8717",
        "127.0.0.1.attaquant.example",
        "localhost.attaquant.example:8717",
        "attaquant.example#127.0.0.1",
        "127.0.0.1:8717@attaquant.example",
        "127.0.0.1:8717, attaquant.example",
        "127.0.0.1:port",
        "[::1",
        "::1",
        "",
        " ",
        None,
    ],
)
def test_unexpected_host_is_refused(host: str | None) -> None:
    assert GUARD.refusal(headers(host)) == "host_not_allowed"


def test_two_host_headers_are_refused() -> None:
    twice = [*headers("127.0.0.1:8717"), (b"host", b"attaquant.example")]
    assert GUARD.refusal(twice) == "host_not_allowed"


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "http://127.0.0.1:8717",
        "http://localhost:3000",
        "HTTP://LOCALHOST:3000",
        "http://[::1]:8717",
        "https://mcp.exemple.fr",
        "https://console.exemple.fr",
    ],
)
def test_no_origin_or_an_expected_one_is_accepted(origin: str | None) -> None:
    assert GUARD.refusal(headers(origin=origin)) is None


@pytest.mark.parametrize(
    "origin",
    [
        "https://attaquant.example",
        "http://attaquant.example:8717",
        "http://127.0.0.1.attaquant.example",
        "http://console.exemple.fr.attaquant.example",
        "https://console.exemple.fr:8443",
        "http://console.exemple.fr",
        "null",
        "file://",
        "127.0.0.1:8717",
        "http://127.0.0.1:8717/chemin",
        "http://utilisateur@127.0.0.1:8717",
        "",
    ],
)
def test_unexpected_origin_is_refused(origin: str) -> None:
    assert GUARD.refusal(headers(origin=origin)) == "origin_not_allowed"


def test_two_origin_headers_are_refused() -> None:
    twice = [*headers(origin="http://127.0.0.1:8717"), (b"origin", b"http://localhost:1")]
    assert GUARD.refusal(twice) == "origin_not_allowed"


def test_the_operator_can_accept_every_host() -> None:
    open_guard = RequestGuard.create(["*"])
    assert open_guard.refusal(headers("attaquant.example", "https://attaquant.example")) is None
    assert open_guard.refusal(headers(None)) is None


@pytest.mark.parametrize(
    "host", ["exemple.fr:8717", "http://exemple.fr", "a b", "", "[::1", "é.fr"]
)
def test_an_accepted_host_is_a_bare_host_name(host: str) -> None:
    with pytest.raises(ValueError, match="hôte"):
        RequestGuard.create([host])


@pytest.mark.parametrize(
    "origin", ["console.exemple.fr", "https://console.exemple.fr/", "ftp://exemple.fr", "null", ""]
)
def test_an_accepted_origin_is_a_scheme_and_a_host(origin: str) -> None:
    with pytest.raises(ValueError, match="origine"):
        RequestGuard.create(LOOPBACK_HOSTS, [origin])


def test_settings_refuse_malformed_hosts_and_origins() -> None:
    with pytest.raises(ProxyConfigurationError, match="hôte"):
        ProxySettings.create("https://mcp.exemple.fr", allowed_hosts=("exemple.fr:80",))
    with pytest.raises(ProxyConfigurationError, match="origine"):
        ProxySettings.create("https://mcp.exemple.fr", allowed_origins=("exemple.fr",))


def test_hosts_and_origins_come_from_the_command_line_or_the_environment() -> None:
    environment = {
        "ANSWERLINEAGE_PROXY_UPSTREAM": "http://127.0.0.1:9000",
        "ANSWERLINEAGE_PROXY_ALLOWED_HOSTS": "proxy.interne, proxy.interne.svc",
        "ANSWERLINEAGE_PROXY_ALLOWED_ORIGINS": "https://console.exemple.fr",
    }
    from_env = cli.settings_from_args([], environment)
    assert from_env.allowed_hosts == ("proxy.interne", "proxy.interne.svc")
    assert from_env.allowed_origins == ("https://console.exemple.fr",)
    assert ProxySettings.from_environment(environment).allowed_hosts == from_env.allowed_hosts

    overridden = cli.settings_from_args(
        [
            "--allow-host",
            "a.interne",
            "--allow-host",
            "b.interne",
            "--allow-origin",
            "https://c.fr",
        ],
        environment,
    )
    assert overridden.allowed_hosts == ("a.interne", "b.interne")
    assert overridden.allowed_origins == ("https://c.fr",)
    assert cli.settings_from_args(["--upstream", "http://h"], {}).allowed_hosts == ()


@pytest.mark.anyio
async def test_a_rebound_host_is_refused_before_anything_reaches_the_server(
    bench: BenchFactory, caplog: pytest.LogCaptureFixture
) -> None:
    body = tool_call(**MODERN_META)
    rebound = [*MODERN_HEADERS, ("host", "attaquant.example:8717")]
    async with bench() as b:
        with caplog.at_level(logging.DEBUG):
            refused = await exchange("POST", b.proxy_url + "/mcp", headers=rebound, content=body)

        assert refused.status == 403
        assert refused.body == b'{"error":"host_not_allowed"}'
        assert refused.header("content-type") == ["application/json"]
        assert b.upstream.requests == []
        await b.proxy.drain()
        assert b.sink.events == []
        assert (b.proxy.stats.refused, b.proxy.stats.relayed, b.proxy.stats.tool_calls) == (1, 0, 0)
        # Le nom reçu n'est pas journalisé : c'est un texte choisi par l'attaquant.
        assert "attaquant" not in caplog.text
        assert "Host" in caplog.text


@pytest.mark.anyio
async def test_a_page_from_another_origin_cannot_call_a_tool_through_the_proxy(
    bench: BenchFactory, caplog: pytest.LogCaptureFixture
) -> None:
    body = tool_call(**MODERN_META)
    foreign = [*MODERN_HEADERS, ("origin", "https://attaquant.example")]
    async with bench() as b:
        with caplog.at_level(logging.DEBUG):
            refused = await exchange("POST", b.proxy_url + "/mcp", headers=foreign, content=body)
            listened = await exchange("GET", b.proxy_url + "/mcp", headers=[foreign[-1]])

        assert (refused.status, refused.body) == (403, b'{"error":"origin_not_allowed"}')
        assert (listened.status, listened.body) == (403, b'{"error":"origin_not_allowed"}')
        assert b.upstream.requests == []
        assert b.proxy.stats.refused == 2
        assert "attaquant" not in caplog.text


@pytest.mark.anyio
async def test_expected_host_and_origin_are_relayed_untouched(bench: BenchFactory) -> None:
    body = tool_call(**MODERN_META)
    async with bench(allowed_origins=("https://console.exemple.fr",)) as b:
        local = [*MODERN_HEADERS, ("origin", b.proxy_url)]
        console = [*MODERN_HEADERS, ("origin", "https://console.exemple.fr")]
        for sent in (local, console):
            relayed = await exchange("POST", b.proxy_url + "/mcp", headers=sent, content=body)
            assert (relayed.status, relayed.body) == (200, TOOL_RESULT)
        assert [request.header("origin") for request in b.upstream.requests] == [
            [b.proxy_url],
            ["https://console.exemple.fr"],
        ]
        assert b.proxy.stats.refused == 0


@pytest.mark.anyio
async def test_an_added_host_is_accepted_and_a_star_accepts_them_all(bench: BenchFactory) -> None:
    body = tool_call(**MODERN_META)
    named = [*MODERN_HEADERS, ("host", "proxy.interne:8717")]
    async with bench() as b:
        assert (
            await exchange("POST", b.proxy_url + "/mcp", headers=named, content=body)
        ).status == 403
    async with bench(allowed_hosts=("proxy.interne",)) as b:
        assert (
            await exchange("POST", b.proxy_url + "/mcp", headers=named, content=body)
        ).status == 200
        assert b.upstream.requests[0].header("host") == ["proxy.interne:8717"]
    async with bench(allowed_hosts=("*",)) as b:
        anything = [
            *MODERN_HEADERS,
            ("host", "nimporte.example"),
            ("origin", "https://nimporte.example"),
        ]
        assert (
            await exchange("POST", b.proxy_url + "/mcp", headers=anything, content=body)
        ).status == 200


@pytest.mark.anyio
async def test_the_name_of_the_mcp_server_is_an_expected_host(bench: BenchFactory) -> None:
    """Déploiement transparent : le client croit parler au serveur MCP et le nomme dans Host."""
    async with bench() as b:
        upstream_host = b.upstream_url.removeprefix("http://")
        relayed = await exchange(
            "POST",
            b.proxy_url + "/mcp",
            headers=[*MODERN_HEADERS, ("host", upstream_host)],
            content=tool_call(**MODERN_META),
        )
        assert relayed.status == 200
