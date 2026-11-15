"""L'explorateur dans un vrai navigateur, sur l'entreprise synthétique.

L'API sert l'interface construite (``make ui``) devant une base chargée par le
chemin réel. Ce que la page affiche est comparé à l'entreprise générée, pas à
ce que l'API renvoie.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, NoReturn

import pytest
from playwright.async_api import Browser, Error, Page, async_playwright
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from answerlineage_api.app import create_app
from answerlineage_api.lineage import impact
from answerlineage_api.lineage.trace import answer_key
from answerlineage_corpus.cli import DEFAULT_SEED
from answerlineage_corpus.enterprise import Enterprise, Request, generate
from proxy.tests.conftest import running
from tests.test_corpus_to_graph import load

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.anyio]

DIST = Path(__file__).resolve().parents[1] / "ui" / "dist"
SHOWN = 60
ORDERS = "ventes.suivi.commandes"


def unavailable(reason: str) -> NoReturn:
    """En CI, un test de bout en bout sauté serait un faux vert."""
    if os.environ.get("CI"):
        pytest.fail(reason)
    pytest.skip(reason)


@pytest.fixture(scope="module")
def enterprise() -> Enterprise:
    return generate(DEFAULT_SEED)


@pytest.fixture
async def explorer(engine: AsyncEngine, enterprise: Enterprise) -> AsyncIterator[str]:
    """Adresse de l'explorateur, servi par l'API devant l'entreprise synthétique."""
    if not (DIST / "index.html").is_file():
        unavailable("L'explorateur n'est pas construit : lancer make ui.")
    await load(engine, enterprise)
    async with running(create_app(engine, ui_directory=DIST)) as url:
        yield url + "/"


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


async def opened(browser: Browser, url: str, *, width: int = 1280) -> tuple[Page, list[str]]:
    """Ouvre une page neuve et retient ses erreurs, dont les refus de la politique de contenu."""
    page = await browser.new_page(viewport={"width": width, "height": 800})
    errors: list[str] = []
    page.on(
        "console", lambda message: errors.append(message.text) if message.type == "error" else None
    )
    page.on("pageerror", lambda error: errors.append(str(error)))
    await page.goto(url)
    return page, errors


async def drawn(page: Page) -> None:
    # Un lien horizontal a une boîte de hauteur nulle : sa présence suffit.
    await page.wait_for_selector("[data-testid=links] path", state="attached")


def answered(enterprise: Enterprise) -> list[Request]:
    """Demandes qui ont répondu, les réponses les plus récentes d'abord."""
    found = [request for request in enterprise.requests if request.answer is not None]
    return sorted(found, key=lambda request: request.ended_at, reverse=True)


def reads(request: Request, table: str) -> bool:
    return any(read.table.name == table for call in request.calls for read in call.reads)


def recipients_of(requests: list[Request]) -> set[str]:
    return {
        pseudonym
        for request in requests
        if request.answer is not None
        for pseudonym in request.answer.recipients
    }


def links_of(requests: list[Request]) -> set[tuple[Any, Any]]:
    """Les liens d'une colonne à l'autre, d'après l'entreprise générée."""
    links: set[tuple[Any, Any]] = set()
    for request in requests:
        assert request.answer is not None
        agent = ("agent", request.agent.name)
        answer = ("answer", request.answer.fingerprint)
        links.add((agent, answer))
        for call in request.calls:
            tool = ("tool", call.tool.server, call.tool.name)
            links.add((tool, agent))
            links.update((("dataset", read.table.name), tool) for read in call.reads)
        links.update((answer, ("recipient", pseudonym)) for pseudonym in request.answer.recipients)
    return links


async def count(page: Page, selector: str) -> int:
    return await page.locator(selector).count()


async def overflow(page: Page) -> dict[str, Any]:
    result: dict[str, Any] = await page.evaluate(
        """() => {
            const width = document.documentElement.clientWidth;
            const beyond = [];
            for (const element of document.querySelectorAll("body *")) {
                const box = element.getBoundingClientRect();
                if (box.width > 0 && (box.left < -0.5 || box.right > width + 0.5)) {
                    beyond.push(`${element.tagName}.${element.getAttribute("class")}`);
                }
            }
            const spilled = [];
            for (const column of document.querySelectorAll(".column")) {
                const bounds = column.getBoundingClientRect();
                for (const node of column.querySelectorAll(".node, h2")) {
                    const box = node.getBoundingClientRect();
                    if (box.right > bounds.right + 0.5 || box.left < bounds.left - 0.5) {
                        spilled.push(node.textContent);
                    }
                }
            }
            const columns = [...document.querySelectorAll(".column")].map(
                (column) => column.getBoundingClientRect().left,
            );
            return {
                width,
                scrollWidth: document.documentElement.scrollWidth,
                beyond,
                spilled,
                columns,
            };
        }"""
    )
    return result


async def test_the_five_columns_and_their_links_show_in_less_than_two_seconds(
    browser: Browser, explorer: str, enterprise: Enterprise
) -> None:
    recent = answered(enterprise)[:SHOWN]
    page = await browser.new_page(viewport={"width": 1280, "height": 800})
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "console", lambda message: errors.append(message.text) if message.type == "error" else None
    )

    started = time.monotonic()
    await page.goto(explorer)
    await drawn(page)
    elapsed = time.monotonic() - started

    assert await page.locator(".column .column-title").all_text_contents() == [
        "Données",
        "Outils",
        "Agents",
        "Réponses",
        "Destinataires",
    ]
    tables = {read.table.name for r in recent for call in r.calls for read in call.reads}
    assert await count(page, ".node-dataset") == len(tables)
    assert await count(page, ".node-tool") == len(
        {(call.tool.server, call.tool.name) for r in recent for call in r.calls}
    )
    assert await count(page, ".node-agent") == len({r.agent.name for r in recent})
    assert await count(page, ".node-answer") == SHOWN
    assert await count(page, ".node-recipient") == len(recipients_of(recent))
    assert await count(page, "[data-testid=links] path") == len(links_of(recent))
    # Chaque lien relie deux nœuds placés : aucun tracé vide ni mal formé.
    assert await page.evaluate(
        """() => [...document.querySelectorAll("[data-testid=links] path")].every(
            (path) => !/NaN|undefined/.test(path.getAttribute("d")) && path.getTotalLength() > 1,
        )"""
    )
    assert elapsed < 2.0
    # Aucune erreur : la politique de sécurité du contenu n'a rien eu à refuser.
    assert errors == []


async def test_nothing_overflows_on_a_screen_400_pixels_wide(
    browser: Browser, explorer: str, enterprise: Enterprise
) -> None:
    incident = enterprise.incidents[0]
    address = (
        f"{explorer}#/propager?namespace={incident.table.namespace}&name={incident.table.name}"
        f"&from={incident.start:%Y-%m-%dT%H:%M:%SZ}&to={incident.end:%Y-%m-%dT%H:%M:%SZ}"
    )
    for url in (explorer, address):
        page, errors = await opened(browser, url, width=400)
        await drawn(page)
        measured = await overflow(page)

        assert measured["scrollWidth"] <= measured["width"] == 400
        assert measured["beyond"] == []
        # Un libellé long est abrégé dans sa colonne, il ne recouvre pas la suivante.
        assert measured["spilled"] == []
        # Les cinq colonnes restent côte à côte, de gauche à droite.
        assert len(measured["columns"]) == 5
        assert measured["columns"] == sorted(set(measured["columns"]))
        assert errors == []


async def test_choosing_a_dataset_lights_what_it_reaches_and_gives_an_address_to_share(
    browser: Browser, explorer: str, enterprise: Enterprise
) -> None:
    recent = answered(enterprise)[:SHOWN]
    readers = [request for request in recent if reads(request, ORDERS)]
    assert 0 < len(readers) < SHOWN

    page, errors = await opened(browser, explorer)
    await drawn(page)
    await page.locator(".node-dataset", has_text="commandes").click()

    assert await count(page, ".node-answer.is-on") == len(readers)
    assert await count(page, ".node-answer.is-off") == SHOWN - len(readers)
    assert await count(page, ".node-recipient.is-on") == len(recipients_of(readers))
    assert await count(page, ".node-dataset.is-on") == 1
    assert await count(page, ".link.is-on") > 0
    summary = await page.locator("[data-testid=summary]").text_content()
    assert summary == (
        f"{ORDERS} (bigquery) : {len(readers)} réponses, "
        f"{len(recipients_of(readers))} destinataires."
    )
    assert page.url == f"{explorer}#/donnees?namespace=bigquery&name={ORDERS}"

    # L'adresse, ouverte ailleurs, retrouve le même jeu de données choisi.
    shared, _ = await opened(browser, page.url)
    await drawn(shared)
    assert await shared.locator('.node[aria-pressed="true"]').all_text_contents() == ["commandes"]
    assert await count(shared, ".node-answer.is-on") == len(readers)

    # Un second clic rend la vue d'ensemble.
    await page.locator(".node-dataset", has_text="commandes").click()
    await page.mouse.move(2, 2)
    assert await count(page, ".node.is-off") == 0
    assert page.url == f"{explorer}#/"
    assert errors == []


async def test_propagating_an_incident_lights_answers_and_recipients_and_shows_the_counts(
    browser: Browser, explorer: str, enterprise: Enterprise
) -> None:
    incident = enterprise.incidents[0]
    touched = list(enterprise.touched(incident))
    address = (
        f"{explorer}#/propager?namespace={incident.table.namespace}&name={incident.table.name}"
        f"&from={incident.start:%Y-%m-%dT%H:%M:%SZ}&to={incident.end:%Y-%m-%dT%H:%M:%SZ}"
    )

    # L'adresse d'un incident, ouverte dans un nouvel onglet : même incident, même mode.
    page, errors = await opened(browser, address)
    await drawn(page)

    assert await page.locator('nav a[aria-current="page"]').text_content() == "Propager"
    assert await page.locator("[data-testid=count-answers]").text_content() == str(len(touched))
    assert await page.locator("[data-testid=count-recipients]").text_content() == str(
        len(recipients_of(touched))
    )
    # La couverture du lignage est visible à côté des compteurs.
    counters = await page.locator("[data-testid=counters] dt").all_text_contents()
    assert counters == ["Réponses touchées", "Destinataires", "Couverture du lignage"]
    assert await page.locator("[data-testid=coverage]").text_content() == "non mesurée"

    assert await count(page, ".node-answer") == len(touched)
    assert await count(page, ".node-answer.is-on") == len(touched)
    assert await count(page, ".node-recipient.is-on") == len(recipients_of(touched))
    assert await page.locator(".node.is-source").all_text_contents() == ["commandes"]
    assert await count(page, ".node-dataset.is-on") == 1
    assert (
        await page.locator(".incident select").evaluate(
            "select => select.selectedOptions[0].textContent"
        )
        == f"{incident.table.name} ({incident.table.namespace})"
    )

    # Une fenêtre plus courte, saisie dans le formulaire, change l'adresse et le résultat.
    sooner = incident.start.replace(hour=12)
    fewer = [
        request
        for request in touched
        if any(
            read.table == incident.table and incident.start <= call.time < sooner
            for call in request.calls
            for read in call.reads
        )
    ]
    assert 0 < len(fewer) < len(touched)
    await page.locator(".incident input").nth(1).fill(f"{sooner:%Y-%m-%dT%H:%M}")
    await page.locator(".incident button[type=submit]").click()
    # Le temps du rechargement, les compteurs s'effacent : ils reviennent avec le nouveau résultat.
    await page.wait_for_function(
        "expected => document.querySelector('[data-testid=count-answers]')?.textContent"
        " === expected",
        arg=str(len(fewer)),
    )
    assert f"{sooner:%Y-%m-%dT%H}" in page.url
    assert await count(page, ".node-answer.is-on") == len(fewer)
    assert errors == []


async def test_an_incident_is_prepared_from_a_dataset_and_a_window(
    browser: Browser, explorer: str, enterprise: Enterprise
) -> None:
    page, errors = await opened(browser, f"{explorer}#/propager")
    await page.wait_for_selector(".incident")

    # Rien n'est lancé tant que le jeu de données n'est pas choisi.
    assert await count(page, "[data-testid=counters]") == 0
    assert await count(page, ".incident option") == 1 + len(enterprise.tables)
    start, end = [await page.locator(".incident input").nth(rank).input_value() for rank in (0, 1)]
    assert start < end

    await page.locator(".incident select").select_option(label=f"{ORDERS} (bigquery)")
    await page.locator(".incident button[type=submit]").click()
    await page.wait_for_selector("[data-testid=counters]")
    assert f"name={ORDERS}" in page.url
    assert int(await page.locator("[data-testid=count-answers]").text_content() or "0") > 0
    assert errors == []


async def test_the_address_of_an_answer_opens_on_its_chain(
    browser: Browser, explorer: str, enterprise: Enterprise
) -> None:
    oldest = answered(enterprise)[-1]
    assert oldest.answer is not None
    identifier = answer_key(oldest.run_id, oldest.answer.fingerprint)
    tables = sorted({read.table.name for call in oldest.calls for read in call.reads})

    # Une réponse ancienne, absente de la vue d'ensemble, s'ouvre par son adresse.
    page, errors = await opened(browser, f"{explorer}#/reponse/{identifier}")
    await drawn(page)

    assert await count(page, ".node-answer") == 1
    assert await count(page, ".node-dataset") == len(tables)
    assert await count(page, ".node-recipient") == len(oldest.answer.recipients)
    assert await page.locator(".node-agent").all_text_contents() == [oldest.agent.name]
    assert await count(page, ".node.is-off") == 0
    summary = await page.locator("[data-testid=summary]").text_content() or ""
    assert oldest.answer.fingerprint[7:15] in summary
    assert f"{len(tables)} jeu" in summary
    assert errors == []

    # Depuis la vue d'ensemble, une réponse choisie donne cette même adresse.
    overview, _ = await opened(browser, explorer)
    await drawn(overview)
    await overview.locator(".node-answer").first.click()
    newest = answered(enterprise)[0]
    assert newest.answer is not None
    link = overview.locator(".status a.action", has_text="Ouvrir cette réponse")
    assert await link.get_attribute("href") == (
        f"#/reponse/{answer_key(newest.run_id, newest.answer.fingerprint)}"
    )


async def test_an_unknown_answer_or_dataset_is_said_plainly(
    browser: Browser, explorer: str, enterprise: Enterprise
) -> None:
    page, _ = await opened(browser, f"{explorer}#/reponse/{'0' * 48}")
    await page.wait_for_selector(".notice-error")
    assert await page.locator(".notice-error").text_content() == (
        "Cette réponse n'est pas connue du graphe."
    )

    await page.goto(
        f"{explorer}#/propager?namespace=bigquery&name=inconnue"
        "&from=2026-11-11T06:00:00Z&to=2026-11-12T12:00:00Z"
    )
    await page.wait_for_selector(".notice-error")
    assert await page.locator(".notice-error").text_content() == (
        "Ce jeu de données n'est pas connu du graphe."
    )

    await page.goto(f"{explorer}#/donnees?namespace=bigquery&name=inconnue")
    await drawn(page)
    notice = await page.locator(".notice").first.text_content() or ""
    assert notice.startswith("Ce jeu de données n'apparaît pas dans les réponses affichées.")


async def test_an_empty_graph_says_so(browser: Browser, engine: AsyncEngine) -> None:
    if not (DIST / "index.html").is_file():
        unavailable("L'explorateur n'est pas construit : lancer make ui.")
    async with running(create_app(engine, ui_directory=DIST)) as url:
        page, errors = await opened(browser, url + "/")
        # Le message d'attente passe d'abord : c'est celui du graphe vide qui est attendu.
        await page.locator(
            ".notice", has_text="Aucune réponse d'agent n'a encore été captée."
        ).wait_for()
        assert await count(page, ".column") == 0
        assert errors == []


async def test_a_truncated_incident_gives_minimums_and_never_says_nothing_is_touched(
    browser: Browser, explorer: str, enterprise: Enterprise, monkeypatch: pytest.MonkeyPatch
) -> None:
    incident = enterprise.incidents[0]
    address = (
        f"{explorer}#/propager?namespace={incident.table.namespace}&name={incident.table.name}"
        f"&from={incident.start:%Y-%m-%dT%H:%M:%SZ}&to={incident.end:%Y-%m-%dT%H:%M:%SZ}"
    )
    # Une seule lecture suivie : le résultat est partiel, ses décomptes des minimums.
    monkeypatch.setattr(impact, "MAX_READS", 1)
    page, errors = await opened(browser, address)
    await page.wait_for_selector("[data-testid=counters]")
    answers = await page.locator("[data-testid=count-answers]").text_content() or ""
    assert answers.startswith("au moins ")
    assert int(answers.removeprefix("au moins ")) < len(list(enterprise.touched(incident)))
    recipients = await page.locator("[data-testid=count-recipients]").text_content() or ""
    assert recipients.startswith("au moins ")
    assert await count(page, "[data-testid=partial]") == 1

    # Aucune lecture suivie : zéro réponse trouvée ne se lit pas comme « rien n'est touché ».
    monkeypatch.setattr(impact, "MAX_RUNS", 0)
    await page.reload()
    await page.wait_for_selector("[data-testid=counters]")
    assert await page.locator("[data-testid=count-answers]").text_content() == "au moins 0"
    assert await count(page, "[data-testid=partial]") == 1
    assert await page.locator(".notice", has_text="Aucune réponse ne s'appuie").count() == 0
    notices = " ".join(await page.locator(".notice").all_text_contents())
    assert "ne permet pas de conclure qu'aucune réponse n'est touchée" in notices
    assert errors == []


async def test_a_read_beyond_the_time_limit_is_said_plainly(
    browser: Browser, engine: AsyncEngine
) -> None:
    if not (DIST / "index.html").is_file():
        unavailable("L'explorateur n'est pas construit : lancer make ui.")
    async with (
        running(create_app(engine, ui_directory=DIST, read_timeout_ms=50)) as url,
        engine.connect() as holder,
    ):
        # Une autre connexion tient la table des réponses : la lecture dépasse son délai.
        await holder.execute(text("LOCK TABLE answer IN ACCESS EXCLUSIVE MODE"))
        page, _ = await opened(browser, url + "/")
        await page.wait_for_selector(".notice-error")
        message = await page.locator(".notice-error").text_content() or ""
        assert message.startswith("La lecture du graphe a dépassé son délai maximal")
        await holder.rollback()
