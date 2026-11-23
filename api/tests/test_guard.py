"""L'API refuse une requête dont l'en-tête Host ou Origin n'est pas attendu."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from api.tests.events import run_event
from api.tests.issuer import authentication, signed_in

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SEARCH = "/api/v1/answers?channel=slack&message_ref=m"


async def call(
    engine: AsyncEngine,
    method: str,
    path: str,
    *,
    host: str = "127.0.0.1:8000",
    origin: str | None = None,
    fetch_site: str | None = None,
    navigation: bool = False,
    json: Any = None,
    allowed_hosts: Sequence[str] | None = None,
    allowed_origins: Sequence[str] | None = None,
) -> httpx.Response:
    headers = {"host": host}
    if origin is not None:
        headers["origin"] = origin
    if fetch_site is not None:
        headers["sec-fetch-site"] = fetch_site
    if navigation:
        headers |= {"sec-fetch-mode": "navigate", "sec-fetch-dest": "document"}
    app = create_app(
        engine,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        authentication=authentication(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:8000", headers=signed_in()
    ) as client:
        return await client.request(method, path, headers=headers, json=json)


async def stored(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(text("SELECT count(*) FROM event_raw"))).scalar_one())


@pytest.mark.parametrize("host", ["127.0.0.1:8000", "localhost:8000", "LOCALHOST", "[::1]:8000"])
async def test_the_loopback_names_are_expected(engine: AsyncEngine, host: str) -> None:
    assert (await call(engine, "GET", SEARCH, host=host)).status_code == 200
    same_site = await call(engine, "GET", SEARCH, host=host, origin=f"http://{host}")
    assert same_site.status_code == 200


@pytest.mark.parametrize(
    "host", ["attaquant.example:8000", "127.0.0.1.attaquant.example", "localhost.attaquant.example"]
)
async def test_a_rebound_host_reads_nothing_and_writes_nothing(
    engine: AsyncEngine, host: str
) -> None:
    for method, path, body in (
        ("GET", SEARCH, None),
        ("GET", "/api/v1/openapi.json", None),
        ("POST", "/api/v1/lineage", run_event()),
        ("POST", "/api/v1/lineage/batch", [run_event()]),
    ):
        refused = await call(engine, method, path, host=host, json=body)
        assert (refused.status_code, refused.json()) == (403, {"error": "host_not_allowed"})
        assert "attaquant" not in refused.text
    assert await stored(engine) == 0


@pytest.mark.parametrize("origin", ["https://attaquant.example", "null", "http://127.0.0.1.evil"])
async def test_a_page_from_another_origin_cannot_send_events(
    engine: AsyncEngine, origin: str
) -> None:
    refused = await call(engine, "POST", "/api/v1/lineage", origin=origin, json=run_event())
    assert (refused.status_code, refused.json()) == (403, {"error": "origin_not_allowed"})
    assert (await call(engine, "GET", SEARCH, origin=origin)).status_code == 403
    assert await stored(engine) == 0

    accepted = await call(engine, "POST", "/api/v1/lineage", json=run_event())
    assert accepted.status_code == 201
    assert await stored(engine) == 1


async def test_a_page_from_another_site_cannot_probe_a_resource_without_origin(
    engine: AsyncEngine,
) -> None:
    """Une balise <img> d'une autre page n'envoie pas Origin, mais apprendrait le statut."""
    probe = "/api/v1/impact?namespace=postgres%3A%2F%2Fentrepot&name=public.clients"
    refused = await call(engine, "GET", probe, fetch_site="cross-site")
    assert (refused.status_code, refused.json()) == (403, {"error": "cross_site_not_allowed"})
    assert refused.headers["cross-origin-resource-policy"] == "same-origin"

    # Une console déclarée reste admise, et un client qui n'est pas un navigateur ne
    # dit pas d'où il vient.
    console = await call(
        engine,
        "GET",
        SEARCH,
        origin="https://console.exemple.fr",
        fetch_site="cross-site",
        allowed_origins=["https://console.exemple.fr"],
    )
    assert console.status_code == 200
    assert (await call(engine, "GET", SEARCH, fetch_site="same-origin")).status_code == 200
    assert (await call(engine, "GET", SEARCH)).status_code == 200
    # Un sous-domaine voisin n'est pas mieux placé qu'un autre site.
    assert (await call(engine, "GET", probe, fetch_site="same-site")).status_code == 403
    # Un lien suivi depuis une autre page est une navigation : il ouvre ce qu'il désigne.
    followed = await call(engine, "GET", SEARCH, fetch_site="cross-site", navigation=True)
    assert followed.status_code == 200
    anything = await call(engine, "GET", SEARCH, fetch_site="cross-site", allowed_hosts=["*"])
    assert anything.status_code == 200


async def test_the_operator_names_the_hosts_and_origins_of_a_deployment(
    engine: AsyncEngine,
) -> None:
    async def deployed(host: str = "127.0.0.1:8000", origin: str | None = None) -> int:
        response = await call(
            engine,
            "GET",
            SEARCH,
            host=host,
            origin=origin,
            allowed_hosts=["lignage.exemple.fr"],
            allowed_origins=["https://console.exemple.fr"],
        )
        return response.status_code

    assert await deployed("lignage.exemple.fr") == 200
    assert await deployed("lignage.exemple.fr", "https://console.exemple.fr") == 200
    # La boucle locale reste acceptée : une sonde de santé y passe.
    assert await deployed() == 200
    assert await deployed("autre.exemple.fr") == 403
    assert await deployed("lignage.exemple.fr", "https://autre.exemple.fr") == 403

    anything = await call(
        engine, "GET", SEARCH, host="nimporte.example", origin="null", allowed_hosts=["*"]
    )
    assert anything.status_code == 200


async def test_hosts_and_origins_come_from_the_environment(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSWERLINEAGE_ALLOWED_HOSTS", "lignage.exemple.fr, api")
    monkeypatch.setenv("ANSWERLINEAGE_ALLOWED_ORIGINS", "https://console.exemple.fr")
    assert (await call(engine, "GET", SEARCH, host="api:8000")).status_code == 200
    console = await call(engine, "GET", SEARCH, host="api", origin="https://console.exemple.fr")
    assert console.status_code == 200
    assert (await call(engine, "GET", SEARCH, host="autre")).status_code == 403


def test_a_malformed_host_or_origin_stops_the_application_from_starting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="hôte"):
        create_app(allowed_hosts=["exemple.fr:8000"], authentication=authentication())
    with pytest.raises(ValueError, match="origine"):
        create_app(allowed_origins=["exemple.fr"], authentication=authentication())
    monkeypatch.setenv("ANSWERLINEAGE_ALLOWED_HOSTS", "http://exemple.fr")
    with pytest.raises(ValueError, match="ANSWERLINEAGE_ALLOWED_HOSTS"):
        create_app(authentication=authentication())
