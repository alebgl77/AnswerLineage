"""Le corpus d'abus dans un vrai navigateur : une adresse hostile laisse l'explorateur inerte.

L'API sert l'interface construite (``make ui``) devant un graphe rempli par le
chemin réel, et l'adresse du cas est ouverte dans Chromium. Aucun nœud ne doit
porter la valeur, la page ne doit ni l'afficher ni en tirer un élément, et un
message fixe doit s'afficher.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from playwright.async_api import Browser, Error, async_playwright
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.auth.roles import Role
from answerlineage_corpus.abuse import Base, Case, healthy_play, load_abuse_corpus, play_of
from answerlineage_corpus.abuse_requests import Play
from answerlineage_corpus.abuse_verdict import Observation, conclude, judge, judge_healthy
from tests.test_abuse_corpus import ARCHIVE, drain, quiet_client, settle, tables
from tests.test_explorer import DIST, opened, served, unavailable

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.anyio]

CORPUS = load_abuse_corpus()
CASES = [case for case in CORPUS if case.surface == "explorateur"]
BASES = [base for base in CORPUS.bases if base.surface == "explorateur"]

# Le navigateur consigne lui-même, comme une erreur, toute réponse 4xx ou 5xx de l'API :
# ce refus est le message fixe attendu, pas une erreur de la page.
REFUSED_BY_THE_API = "Failed to load resource: the server responded with a status of"
# Ce que la page a de nœuds, de texte, et d'éléments qu'une adresse aurait fait naître.
FACTS = """() => ({
    nodes: [...document.querySelectorAll(".node")].map((node) => node.outerHTML),
    text: document.body.innerText,
    injected: document.querySelectorAll(
        "img, iframe, object, embed, body script, [onerror], [onload], [onclick]",
    ).length,
})"""


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


async def observe_explorer(
    play: Play, engine: AsyncEngine, browser: Browser, caplog: pytest.LogCaptureFixture
) -> Observation:
    """Ouvre l'adresse dans l'explorateur, servi par l'API devant les événements préalables."""
    if not (DIST / "index.html").is_file():
        unavailable("L'explorateur n'est pas construit : lancer make ui.")
    quiet_client(caplog)
    with caplog.at_level(logging.DEBUG):
        async with served(engine) as (url, issuer, _):
            async with httpx.AsyncClient(
                base_url=url, trust_env=False, headers=issuer.headers(Role.EMITTER)
            ) as client:
                await client.post("/api/v1/lineage/batch", json=list(play.seed))
            await drain(engine)
            page, errors = await opened(browser, f"{url}{play.path}")
            # La page a fini de lire l'API : plus aucun message d'attente.
            await page.wait_for_load_state("networkidle")
            await page.wait_for_function("() => !document.body.innerText.includes('Chargement')")
            facts: dict[str, Any] = await page.evaluate(FACTS)
            await page.close()
        stored = await tables(engine)
    return Observation(
        places={
            "nœuds": "\n".join(facts["nodes"]),
            "page": facts["text"],
            "journaux": caplog.text,
            "base": "\n".join(
                row for name, rows in stored.items() if name != ARCHIVE for row in rows
            ),
        },
        nodes=tuple(facts["nodes"]),
        text=facts["text"],
        errors=sum(1 for error in errors if not error.startswith(REFUSED_BY_THE_API)),
        injected=facts["injected"],
    )


@pytest.mark.parametrize("base", BASES, ids=lambda base: base.name)
async def test_the_healthy_address_of_every_base_shows_its_graph(
    base: Base, engine: AsyncEngine, browser: Browser, caplog: pytest.LogCaptureFixture
) -> None:
    seen = await observe_explorer(healthy_play(base), engine, browser, caplog)
    assert judge_healthy(base, seen) == [], base.name


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
async def test_every_hostile_address_of_the_abuse_corpus_leaves_the_explorer_inert(
    case: Case, engine: AsyncEngine, browser: Browser, caplog: pytest.LogCaptureFixture
) -> None:
    seen = await observe_explorer(play_of(case), engine, browser, caplog)
    settle(conclude(case, judge(case, seen)))
