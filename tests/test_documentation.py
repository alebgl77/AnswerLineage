"""Les exemples de la documentation restent vrais quand le code change."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from answerlineage_api.lineage.trace import answer_key
from answerlineage_corpus.cli import DEFAULT_SEED
from answerlineage_corpus.enterprise import generate

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
