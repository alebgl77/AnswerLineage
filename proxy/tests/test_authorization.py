"""Le proxy ne prend aucune part à l'autorisation.

Il ne termine pas OAuth, ne répond à aucun défi et ne garde aucun jeton.
"""

from __future__ import annotations

import pytest

from proxy.tests.conftest import (
    CHALLENGE,
    MODERN_HEADERS,
    MODERN_META,
    UNAUTHORIZED,
    BenchFactory,
    exchange,
    tool_call,
)

pytestmark = pytest.mark.anyio

TOKEN = "Bearer canari-jeton-2c81e7"  # noqa: S105 — canari de test, pas un secret.


async def test_the_challenge_of_the_server_reaches_the_client_untouched(
    bench: BenchFactory,
) -> None:
    body = tool_call(**MODERN_META)
    async with bench() as b:
        direct = await exchange(
            "POST", b.upstream_url + "/unauthorized", headers=MODERN_HEADERS, content=body
        )
        proxied = await exchange(
            "POST", b.proxy_url + "/unauthorized", headers=MODERN_HEADERS, content=body
        )

        assert proxied.status == direct.status == 401
        assert (
            proxied.header("www-authenticate") == direct.header("www-authenticate") == [CHALLENGE]
        )
        assert proxied.body == direct.body == UNAUTHORIZED
        # Le proxy ne répond pas au défi à la place du client : un seul appel est parti.
        assert len(b.upstream.requests) == 2
        assert all(request.header("authorization") == [] for request in b.upstream.requests)

        await b.proxy.drain()
        (event,) = b.sink.events
        facet = event["run"]["facets"]["answerlineage_toolCall"]
        assert (event["eventType"], facet["httpStatus"]) == ("FAIL", 401)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/.well-known/oauth-protected-resource"),
        ("GET", "/.well-known/oauth-protected-resource/mcp"),
        ("GET", "/.well-known/oauth-authorization-server"),
        ("GET", "/.well-known/openid-configuration"),
        ("GET", "/authorize?response_type=code&client_id=agent"),
        ("POST", "/token"),
        ("POST", "/register"),
    ],
)
async def test_authorization_endpoints_are_those_of_the_server_never_the_proxy_s(
    bench: BenchFactory, method: str, path: str
) -> None:
    async with bench() as b:
        content = b"grant_type=authorization_code&code=canari" if method == "POST" else None
        direct = await exchange(method, b.upstream_url + path, content=content)
        proxied = await exchange(method, b.proxy_url + path, content=content)

        assert (proxied.status, proxied.body) == (direct.status, direct.body)
        relayed = b.upstream.requests[-1]
        target = relayed.raw_path + (b"?" + relayed.query if relayed.query else b"")
        assert (relayed.method, target) == (method, path.encode())
        assert relayed.body == (content or b"")
        # Rien de ce qui ressemble à un flux d'autorisation ne devient un événement.
        await b.proxy.drain()
        assert b.sink.events == []


async def test_a_token_is_relayed_with_its_call_and_never_replayed_on_the_next(
    bench: BenchFactory,
) -> None:
    body = tool_call(**MODERN_META)
    async with bench() as b:
        await exchange(
            "POST",
            b.proxy_url + "/mcp",
            headers=[*MODERN_HEADERS, ("authorization", TOKEN), ("cookie", "session=canari")],
            content=body,
        )
        await exchange("POST", b.proxy_url + "/mcp", headers=MODERN_HEADERS, content=body)
        await exchange("GET", b.proxy_url + "/mcp")

        first, second, third = b.upstream.requests
        assert first.header("authorization") == [TOKEN]
        assert first.header("cookie") == ["session=canari"]
        for later in (second, third):
            assert later.header("authorization") == []
            assert later.header("cookie") == []
