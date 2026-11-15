"""Les exemples de la documentation restent vrais quand le code change."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from answerlineage_api.lineage.trace import answer_key
from answerlineage_corpus.abuse import (
    CASES_DIRECTORY,
    FAMILIES,
    VERDICTS,
    abuse_texts,
    load_abuse_corpus,
)
from answerlineage_corpus.abuse_requests import RECIPES
from answerlineage_corpus.cli import DEFAULT_SEED
from answerlineage_corpus.enterprise import generate
from answerlineage_corpus.truth import truth

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = sorted(
    [
        *ROOT.glob("*.md"),
        *ROOT.glob("docs/*.md"),
        *ROOT.glob("spec/**/*.md"),
    ]
)
# Lien Markdown vers un fichier du dépôt : ni adresse web, ni ancre seule.
_LINK = re.compile(r"\]\((?!https?://|#|mailto:)([^)#\s]+)(?:#[^)\s]*)?\)")
_SEARCH = re.compile(r"/api/v1/answers\?channel=(\w+)&message_ref=(\w+)")
_SOURCES = re.compile(r"/api/v1/answers/([0-9a-f]+)/sources")
# Un exemple chiffré, pas la ligne du tableau des routes, qui écrit « … ».
_VALUE = r"([A-Za-z0-9_.:/-]+)"
_IMPACT = re.compile(rf"/api/v1/impact\?namespace={_VALUE}&name={_VALUE}&from={_VALUE}&to={_VALUE}")
_INCIDENT_ADDRESS = re.compile(
    rf"#/propager\?namespace={_VALUE}&name={_VALUE}&from={_VALUE}&to={_VALUE}"
)
_COUNTED = re.compile(r"(\d+) réponses(?: touchées)? et (\d+) destinataires")
# Une ligne du tableau des cas ouverts, et le cas d'abus donné en exemple.
_OPEN_CASE = re.compile(r"^\| `([a-z0-9-]+/[a-z0-9-]+)` \|", re.MULTILINE)
_ABUSE_CASE = re.compile(
    r'```json\n(\{\n  "name": "[a-z0-9-]+",\n  "title": .*?\n\})\n```', re.DOTALL
)


@pytest.mark.parametrize("document", ["README.md", "docs/lecture.md"])
def test_the_documented_answer_exists_in_the_default_synthetic_enterprise(document: str) -> None:
    text = (ROOT / document).read_text(encoding="utf-8")
    messages = set(_SEARCH.findall(text))
    identifiers = set(_SOURCES.findall(text))
    assert len(messages) == 1
    assert len(identifiers) == 1

    answers = {
        (request.answer.channel, request.answer.message_ref): answer_key(
            request.run_id, request.answer.fingerprint
        )
        for request in generate(DEFAULT_SEED).requests
        if request.answer is not None
    }
    (message,) = messages
    assert {answers[message]} == identifiers


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda path: str(path.relative_to(ROOT)))
def test_every_link_to_a_file_of_the_repository_leads_somewhere(document: Path) -> None:
    for target in _LINK.findall(document.read_text(encoding="utf-8")):
        assert (document.parent / target).exists(), f"{document.name} : lien mort vers {target}"


def test_the_readme_leads_to_the_contribution_guide_and_the_security_policy() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "](CONTRIBUTING.md)" in readme
    assert "](SECURITY.md)" in readme


@pytest.mark.parametrize("document", ["README.md", "docs/lecture.md"])
def test_the_documented_incident_is_the_first_of_the_default_synthetic_enterprise(
    document: str,
) -> None:
    text = (ROOT / document).read_text(encoding="utf-8")
    first = truth(generate(DEFAULT_SEED))["incidents"][0]
    assert set(_IMPACT.findall(text)) == {
        (first["dataset"]["namespace"], first["dataset"]["name"], first["from"], first["to"])
    }
    assert set(_COUNTED.findall(text)) == {
        (str(len(first["answers"])), str(len(first["recipients"])))
    }


@pytest.mark.parametrize("document", ["README.md", "docs/demonstration.md"])
def test_the_documented_address_opens_the_first_incident_of_the_demonstration(
    document: str,
) -> None:
    text = (ROOT / document).read_text(encoding="utf-8")
    first = truth(generate(DEFAULT_SEED))["incidents"][0]
    assert set(_INCIDENT_ADDRESS.findall(text)) == {
        (first["dataset"]["namespace"], first["dataset"]["name"], first["from"], first["to"])
    }
    assert set(_COUNTED.findall(text)) == {
        (str(len(first["answers"])), str(len(first["recipients"])))
    }


def test_every_open_case_of_the_abuse_corpus_is_documented_with_its_reason() -> None:
    text = (ROOT / "docs" / "corpus.md").read_text(encoding="utf-8")
    opened = load_abuse_corpus().open_cases
    assert opened
    for case in opened:
        assert f"| `{case.id}` | {case.open_reason} |" in text, case.id
    # Un cas qui n'est plus ouvert ne reste pas dans le tableau.
    assert sorted(_OPEN_CASE.findall(text)) == sorted(case.id for case in opened)


def test_the_documented_abuse_case_is_a_case_of_the_corpus() -> None:
    text = (ROOT / "docs" / "corpus.md").read_text(encoding="utf-8")
    (example,) = _ABUSE_CASE.findall(text)
    # L'exemple montre l'échappement, pas le caractère invisible.
    assert "\\u200b" in example
    family = json.loads(abuse_texts()[f"{CASES_DIRECTORY}/invisibles.json"])
    assert json.loads(example) in family["cases"]


def test_the_documentation_names_every_family_verdict_and_recipe_of_the_abuse_corpus() -> None:
    text = (ROOT / "docs" / "corpus.md").read_text(encoding="utf-8")
    verdicts = {verdict for admitted in VERDICTS.values() for verdict in admitted}
    for name in (*FAMILIES, *sorted(verdicts), *RECIPES):
        assert f"| `{name}` |" in text or f"`{name}` |" in text, name
