"""Les parcours principaux de l'explorateur, joués dans Chromium contre la démonstration.

La démonstration (``docker compose up``) est lancée à part, et son adresse est
donnée par ``ANSWERLINEAGE_DEMO_URL`` : ``scripts/check_demo.sh browser`` la
fournit. Sans elle, ces tests sont sautés.

La démonstration charge l'entreprise synthétique de la graine par défaut. Ce
que la page affiche est comparé à cette entreprise, générée ici, et à la vérité
de ses incidents : jamais à ce que l'API renvoie.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.async_api import Browser, Error, Page, async_playwright, expect

from answerlineage_api.lineage.trace import answer_key
from answerlineage_corpus.cli import DEFAULT_SEED
from answerlineage_corpus.enterprise import Enterprise, Request, generate
from answerlineage_corpus.truth import truth
from tests.test_explorer import answered, drawn, opened

pytestmark = [pytest.mark.demo, pytest.mark.anyio]

DEMO_URL = "ANSWERLINEAGE_DEMO_URL"
# La démonstration vient parfois de démarrer : la première vue peut tarder.
FIRST_VIEW_MS = 30_000


@pytest.fixture(scope="module")
def enterprise() -> Enterprise:
    return generate(DEFAULT_SEED)


@pytest.fixture
def demo() -> str:
    """Adresse de l'explorateur de la démonstration lancée."""
    url = os.environ.get(DEMO_URL, "").strip()
    if not url:
        pytest.skip(f"Aucune démonstration visée : {DEMO_URL} n'est pas donnée.")
    return url.rstrip("/") + "/"


@pytest.fixture
async def browser(demo: str) -> AsyncIterator[Browser]:
    async with async_playwright() as playwright:
        try:
            launched = await playwright.chromium.launch()
        except Error:
            # La démonstration est visée : sauter ces parcours serait un faux vert.
            pytest.fail("Chromium manque : lancer uv run playwright install chromium.")
        try:
            yield launched
        finally:
            await launched.close()


def plural(count: int, one: str, many: str) -> str:
    return f"{count} {many if count > 1 else one}"


def short(fingerprint: str) -> str:
    """Les huit premiers caractères de l'empreinte, ceux que l'explorateur affiche."""
    return fingerprint.split(":", 1)[1][:8]


async def labels(page: Page, selector: str) -> list[str]:
    """Libellés complets des nœuds, ceux que lit un lecteur d'écran."""
    found: list[str] = await page.locator(selector).evaluate_all(
        "nodes => nodes.map((node) => node.getAttribute('aria-label'))"
    )
    return sorted(found)


def hash_query(url: str) -> dict[str, list[str]]:
    fragment = urlsplit(url).fragment
    return parse_qs(fragment.split("?", 1)[1]) if "?" in fragment else {}


async def test_tracing_back_an_answer_shows_what_it_read_through_which_tools_for_whom(
    demo: str, browser: Browser, enterprise: Enterprise
) -> None:
    """Remonter : en mode Remonter, une réponse choisie et son chemin jusqu'aux données, puis
    sa page, et ses destinataires."""
    newest: Request = answered(enterprise)[0]
    answer = newest.answer
    assert answer is not None
    tables = sorted(
        {f"{read.table.name} ({read.table.namespace})" for c in newest.calls for read in c.reads}
    )
    tools = sorted({f"{call.tool.name}, serveur {call.tool.server}" for call in newest.calls})
    agent = f"{newest.agent.name} ({newest.agent.namespace})"

    page, errors = await opened(browser, demo)
    await drawn(page)
    await page.get_by_role("link", name="Remonter", exact=True).click()
    await expect(page).to_have_url(f"{demo}#/remonter")
    await expect(page.get_by_test_id("summary")).to_contain_text(
        "Choisissez une réponse", timeout=FIRST_VIEW_MS
    )
    # La réponse la plus récente, reconnue à son empreinte.
    chosen = page.locator(f'.node-answer[aria-label^="Réponse {short(answer.fingerprint)} "]')
    await chosen.click(timeout=FIRST_VIEW_MS)

    identifier = answer_key(newest.run_id, answer.fingerprint)
    await expect(page).to_have_url(f"{demo}#/remonter/{identifier}")
    # Seul son chemin jusqu'aux données reste en évidence : ses destinataires s'effacent.
    assert await labels(page, ".node-dataset.is-on") == tables
    assert await labels(page, ".node-tool.is-on") == tools
    assert await labels(page, ".node-agent.is-on") == [agent]
    assert await page.locator(".node-answer.is-on").count() == 1
    assert await page.locator(".node-recipient.is-on").count() == 0
    # La démonstration publie les relevés de ses serveurs : la période du run est couverte.
    await expect(page.get_by_test_id("coverage")).to_have_text(
        "100\N{NO-BREAK SPACE}%", timeout=FIRST_VIEW_MS
    )

    await page.get_by_role("link", name="Ouvrir cette réponse").click()
    await expect(page).to_have_url(f"{demo}#/reponse/{identifier}")
    await expect(page.locator(".node-answer")).to_have_count(1, timeout=FIRST_VIEW_MS)
    await drawn(page)
    # Le pointeur, resté où était le lien, ne survole aucun nœud.
    await page.mouse.move(2, 2)

    # Ce qu'elle a lu, par quels outils, par quel agent, pour qui.
    assert await labels(page, ".node-dataset") == tables
    assert await labels(page, ".node-tool") == tools
    assert await labels(page, ".node-agent") == [agent]
    assert await labels(page, ".node-recipient") == sorted(
        f"Destinataire {pseudonym}" for pseudonym in answer.recipients
    )
    # Toute la chaîne de la réponse est allumée, rien n'est éteint.
    assert await page.locator(".node.is-off").count() == 0
    summary = await page.get_by_test_id("summary").text_content() or ""
    assert summary.startswith(f"Réponse {short(answer.fingerprint)} du ")
    assert summary.endswith(
        f" : {plural(len(tables), 'jeu de données', 'jeux de données')}, "
        f"{plural(len(tools), 'outil', 'outils')}, "
        f"{plural(len(answer.recipients), 'destinataire', 'destinataires')}."
    )
    assert errors == []


async def test_propagating_the_first_incident_lights_the_answers_and_recipients_it_touches(
    demo: str, browser: Browser, enterprise: Enterprise
) -> None:
    """Propager : le premier incident, saisi dans le formulaire, allume ce qu'il touche."""
    incident: dict[str, Any] = truth(enterprise)["incidents"][0]
    dataset = incident["dataset"]
    answers = incident["answers"]
    recipients = incident["recipients"]
    assert 0 < len(answers) < 200

    page, errors = await opened(browser, demo)
    await drawn(page)
    await page.get_by_role("link", name="Propager", exact=True).click()
    form = page.get_by_role("form", name="Incident à propager")
    await expect(form).to_be_visible(timeout=FIRST_VIEW_MS)
    await form.get_by_label("Jeu de données faux").select_option(
        label=f"{dataset['name']} ({dataset['namespace']})"
    )
    # Un champ de date et d'heure prend « 2026-11-11T06:00 » : l'instant, sans secondes ni zone.
    await form.get_by_label("Faux depuis (UTC)").fill(incident["from"][:16])
    await form.get_by_label("Corrigé à (UTC)").fill(incident["to"][:16])
    await form.get_by_role("button", name="Propager").click()

    await expect(page.get_by_test_id("count-answers")).to_have_text(
        str(len(answers)), timeout=FIRST_VIEW_MS
    )
    await expect(page.get_by_test_id("count-recipients")).to_have_text(str(len(recipients)))
    await drawn(page)
    assert hash_query(page.url) == {
        "namespace": [dataset["namespace"]],
        "name": [dataset["name"]],
        "from": [incident["from"]],
        "to": [incident["to"]],
    }

    # Les nœuds allumés sont exactement les réponses et les destinataires de la vérité.
    lit_answers = await labels(page, ".node-answer.is-on")
    assert sorted(label.split()[1] for label in lit_answers) == sorted(
        short(answer["fingerprint"]) for answer in answers
    )
    assert await page.locator(".node-answer.is-off").count() == 0
    assert await labels(page, ".node-recipient.is-on") == sorted(
        f"Destinataire {pseudonym}" for pseudonym in recipients
    )
    assert await labels(page, ".node.is-source") == [f"{dataset['name']} ({dataset['namespace']})"]
    assert await page.locator(".node-dataset.is-on").count() == 1
    assert errors == []
