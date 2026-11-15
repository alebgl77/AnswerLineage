"""L'API sert les fichiers de l'explorateur, sous une politique de sécurité du contenu stricte."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from answerlineage_api.app import create_app
from answerlineage_api.explorer import CONTENT_SECURITY_POLICY

pytestmark = pytest.mark.anyio

INDEX = b'<!doctype html><html lang="fr"><body><div id="root"></div></body></html>'
SCRIPT = b"console.log('explorateur');"


@pytest.fixture
def built(tmp_path: Path) -> Path:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_bytes(INDEX)
    (tmp_path / "assets" / "index-abc123.js").write_bytes(SCRIPT)
    (tmp_path.parent / "secret.txt").write_text("hors du dossier")
    return tmp_path


async def get(path: str, ui_directory: Path | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=create_app(ui_directory=ui_directory))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        return await client.get(path)


async def test_the_explorer_is_served_at_the_root_under_a_strict_content_policy(
    built: Path,
) -> None:
    response = await get("/", built)
    assert (response.status_code, response.content) == (200, INDEX)
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
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
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY


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


def test_a_directory_without_the_explorer_stops_the_application_from_starting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match=r"index\.html"):
        create_app(ui_directory=tmp_path)
    monkeypatch.setenv("ANSWERLINEAGE_UI_DIR", str(tmp_path / "absent"))
    with pytest.raises(ValueError, match="ANSWERLINEAGE_UI_DIR"):
        create_app()
