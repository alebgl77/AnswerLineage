"""L'API sert les fichiers de l'explorateur, sous une politique de sécurité du contenu stricte."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from answerlineage_api.app import create_app
from answerlineage_api.auth import matrix
from answerlineage_api.auth.matrix import Access
from answerlineage_api.auth.roles import Role
from answerlineage_api.headers import CONTENT_SECURITY_POLICY
from api.tests.issuer import authentication, signed_in

pytestmark = pytest.mark.anyio

STRICT = authentication(explorer_client_id=None)
SECURITY_HEADERS = (
    "content-security-policy",
    "x-content-type-options",
    "referrer-policy",
    "cross-origin-opener-policy",
    "cross-origin-resource-policy",
)
INDEX = b'<!doctype html><html lang="fr"><body><div id="root"></div></body></html>'
SCRIPT = b"console.log('explorateur');"


@pytest.fixture
def built(tmp_path: Path) -> Path:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_bytes(INDEX)
    (tmp_path / "assets" / "index-abc123.js").write_bytes(SCRIPT)
    (tmp_path.parent / "secret.txt").write_text("hors du dossier")
    return tmp_path


async def get(
    path: str,
    ui_directory: Path | None = None,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    # Sans client de l'explorateur chez l'émetteur : la politique n'ajoute aucune origine.
    transport = httpx.ASGITransport(
        app=create_app(ui_directory=ui_directory, authentication=STRICT)
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="http://localhost", headers=signed_in()
    ) as client:
        return await client.request(method, path, headers=headers)


def assert_secured(response: httpx.Response) -> None:
    for name in SECURITY_HEADERS:
        # Une seule fois chacun : deux politiques se cumuleraient.
        assert len(response.headers.get_list(name)) == 1
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cross-origin-opener-policy"] == "same-origin"
    # Une page d'un autre site ne peut charger aucune réponse par <img> ou <script>.
    assert response.headers["cross-origin-resource-policy"] == "same-origin"


async def test_the_explorer_is_served_at_the_root_under_a_strict_content_policy(
    built: Path,
) -> None:
    response = await get("/", built)
    assert (response.status_code, response.content) == (200, INDEX)
    assert response.headers["content-type"].startswith("text/html")
    assert_secured(response)
    assert response.headers["cache-control"] == "no-cache"


def test_the_content_policy_allows_nothing_inline_and_nothing_from_elsewhere() -> None:
    directives = dict(
        directive.strip().split(" ", 1) for directive in CONTENT_SECURITY_POLICY.split(";")
    )
    assert directives["default-src"] == "'none'"
    assert directives["script-src"] == "'self'"
    assert directives["style-src"] == "'self'"
    assert directives["connect-src"] == "'self'"
    assert directives["frame-ancestors"] == "'none'"
    assert "unsafe" not in CONTENT_SECURITY_POLICY
    assert "http" not in CONTENT_SECURITY_POLICY
    assert "*" not in CONTENT_SECURITY_POLICY


async def test_a_built_asset_is_served_and_kept_by_the_browser(built: Path) -> None:
    response = await get("/assets/index-abc123.js", built)
    assert (response.status_code, response.content) == (200, SCRIPT)
    assert "javascript" in response.headers["content-type"]
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert_secured(response)


@pytest.mark.parametrize(
    "path",
    ["/absent.js", "/assets/", "/../secret.txt", "/assets/../../secret.txt", "/%2e%2e/secret.txt"],
)
async def test_nothing_outside_the_built_files_is_served(built: Path, path: str) -> None:
    response = await get(path, built)
    assert response.status_code == 404
    assert "hors du dossier" not in response.text


async def test_the_routes_of_the_api_keep_their_place(built: Path) -> None:
    response = await get("/api/v1/openapi.json", built)
    assert response.status_code == 200
    assert "/api/v1/graph" in response.json()["paths"]
    assert (await get("/api/v1/absente", built)).status_code == 404


async def test_without_built_files_the_api_serves_no_page() -> None:
    assert (await get("/")).status_code == 404


async def test_the_directory_comes_from_the_environment(
    built: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANSWERLINEAGE_UI_DIR", str(built))
    assert (await get("/")).content == INDEX


@pytest.mark.parametrize(
    ("method", "path", "headers", "status"),
    [
        ("GET", "/absent.js", {}, 404),
        ("POST", "/", {}, 405),
        ("GET", "/assets/index-abc123.js", {"range": "bytes=999-1000"}, 416),
        ("GET", "/api/v1/openapi.json", {}, 200),
        ("GET", "/api/v1/absente", {}, 404),
        ("GET", "/api/v1/answers", {}, 422),
        ("GET", "/", {"host": "attaquant.example"}, 403),
        ("GET", "/", {"origin": "https://attaquant.example"}, 403),
        ("GET", "/api/v1/openapi.json", {"sec-fetch-site": "cross-site"}, 403),
    ],
)
async def test_every_response_carries_the_security_headers(
    built: Path, method: str, path: str, headers: dict[str, str], status: int
) -> None:
    """Fichiers et API, succès comme erreur : Starlette lève 404, 405 et 416 hors de toute route."""
    response = await get(path, built, method=method, headers=headers)
    assert response.status_code == status
    assert_secured(response)


async def test_an_unexpected_failure_carries_the_security_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(authentication=STRICT)
    # Une route ajoutée après coup s'ouvre par la matrice, comme toute route.
    monkeypatch.setitem(
        matrix.ACCESS, ("GET", "/api/v1/panne"), Access(frozenset({Role.READER}), "Une panne.")
    )

    @app.get("/api/v1/panne")
    async def failing() -> None:
        raise RuntimeError

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://localhost", headers=signed_in()
    ) as client:
        response = await client.get("/api/v1/panne")
    assert response.status_code == 500
    assert_secured(response)


def test_a_directory_without_the_explorer_stops_the_application_from_starting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match=r"index\.html"):
        create_app(ui_directory=tmp_path, authentication=authentication())
    monkeypatch.setenv("ANSWERLINEAGE_UI_DIR", str(tmp_path / "absent"))
    with pytest.raises(ValueError, match="ANSWERLINEAGE_UI_DIR"):
        create_app(authentication=authentication())
