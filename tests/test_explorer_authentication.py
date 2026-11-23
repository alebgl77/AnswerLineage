"""La connexion de l'explorateur, dans un vrai navigateur, devant un émetteur OIDC local.

Le flux d'autorisation avec PKCE, la déconnexion et l'expiration se jouent
contre l'émetteur de test servi sur la boucle locale ; la page ne garde son
jeton qu'en mémoire, sous sa politique de sécurité du contenu. La démonstration,
sans authentification, le dit dans un bandeau.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Browser, Error, Page, async_playwright, expect
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_api.auth.access import Authentication
from answerlineage_corpus.cli import DEFAULT_SEED
from answerlineage_corpus.enterprise import generate
from proxy.tests.conftest import running
from tests.test_corpus_to_graph import load
from tests.test_explorer import DIST, drawn, opened, served, unavailable

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.anyio]

ORDERS = "ventes.suivi.commandes"


@pytest.fixture
async def loaded(engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """La base, chargée de l'entreprise synthétique par défaut."""
    if not (DIST / "index.html").is_file():
        unavailable("L'explorateur n'est pas construit : lancer make ui.")
    await load(engine, generate(DEFAULT_SEED))
    yield engine


@pytest.fixture
async def browser() -> AsyncIterator[Browser]:
    async with async_playwright() as playwright:
        try:
            launched = await playwright.chromium.launch()
        except Error:
            unavailable("Chromium manque : lancer uv run playwright install chromium.")
        try:
            yield launched
        finally:
            await launched.close()


async def kept(page: Page) -> dict[str, object]:
    """Ce que le navigateur garde de la page : stockages et cookies."""
    found: dict[str, object] = await page.evaluate(
        """() => ({
            local: Object.keys(window.localStorage),
            session: Object.keys(window.sessionStorage),
            cookie: document.cookie,
            search: window.location.search,
        })"""
    )
    return found


async def test_the_explorer_signs_in_with_pkce_and_keeps_its_token_in_memory_only(
    browser: Browser, loaded: AsyncEngine
) -> None:
    async with served(loaded) as (url, _, issuer):
        page, errors = await opened(browser, f"{url}#/donnees?namespace=bigquery&name={ORDERS}")
        await drawn(page)
        # Le code est échangé une fois, contre un jeton que l'API vérifie.
        assert len(issuer.issued) == 1
        assert issuer.grants == {}
        # L'adresse d'avant la connexion est rouverte, sans le code ni l'état.
        assert page.url == f"{url}#/donnees?namespace=bigquery&name={ORDERS}"
        assert await page.locator('.node[aria-pressed="true"]').all_text_contents() == ["commandes"]
        # Ni stockage ni cookie : le jeton n'est qu'en mémoire.
        assert await kept(page) == {"local": [], "session": [], "cookie": "", "search": ""}
        token = issuer.issued[0]
        storage = await page.context.storage_state()
        assert token not in str(storage)
        assert errors == []


async def test_signing_out_forgets_the_session_and_asks_before_signing_in_again(
    browser: Browser, loaded: AsyncEngine
) -> None:
    async with served(loaded) as (url, _, issuer):
        page, errors = await opened(browser, url)
        await drawn(page)
        await page.get_by_role("button", name="Se déconnecter").click()
        await expect(page.get_by_role("alert")).to_have_text(
            "Vous êtes déconnecté. Le lignage ne se lit qu'avec un compte qui en a le rôle."
        )
        assert issuer.logouts == 1
        assert await page.locator(".column").count() == 0
        # Rien ne se rouvre seul : la personne choisit de se reconnecter.
        assert len(issuer.issued) == 1
        await page.get_by_role("button", name="Se connecter").click()
        await drawn(page)
        assert len(issuer.issued) == 2
        assert errors == []


async def test_an_expired_session_says_so_and_offers_to_sign_in_again(
    browser: Browser, loaded: AsyncEngine
) -> None:
    # Un jeton de 31 s : la session prend fin 30 s avant, soit au bout d'une seconde.
    async with served(loaded, {"lifetime": 31}) as (url, _, issuer):
        page, errors = await opened(browser, url)
        await expect(page.get_by_role("alert")).to_have_text(
            "La session a expiré : reconnectez-vous pour lire le lignage.", timeout=10_000
        )
        await page.get_by_role("button", name="Se connecter").click()
        await page.wait_for_selector("[data-testid=links] path", state="attached")
        assert len(issuer.issued) == 2
        assert errors == []


async def test_a_role_that_does_not_open_the_lineage_is_said_plainly(
    browser: Browser, loaded: AsyncEngine
) -> None:
    async with served(loaded, {"roles": ()}) as (url, _, _):
        page, errors = await opened(browser, url)
        await expect(page.locator(".notice-error")).to_have_text(
            "Votre rôle ne permet pas de lire cette vue."
        )
        assert await page.locator(".column").count() == 0
        # Le navigateur consigne lui-même le refus 403 de l'API ; rien d'autre.
        assert [error for error in errors if "403" not in error] == []


async def test_the_demonstration_shows_a_banner_and_reads_without_token(
    browser: Browser, loaded: AsyncEngine
) -> None:
    app = create_app(loaded, ui_directory=DIST, authentication=Authentication.disabled())
    async with running(app) as url:
        page, errors = await opened(browser, url + "/")
        await drawn(page)
        await expect(page.get_by_role("note")).to_have_text(
            "Démonstration : l'authentification est désactivée. Cette API ne doit être jointe "
            "que par la boucle locale de ce poste."
        )
        assert await page.get_by_role("button", name="Se déconnecter").count() == 0
        assert errors == []
