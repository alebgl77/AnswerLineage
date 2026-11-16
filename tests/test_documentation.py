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
from scripts.check_release import distribution_names, normalize

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


# Un paquet ou une image cités : adresse d'un registre, commande d'installation, image.
_REGISTRY_ADDRESSES = (
    ("pypi", re.compile(r"pypi\.org/(?:project|pypi|simple)/([A-Za-z0-9][A-Za-z0-9._-]*)")),
    ("npm", re.compile(r"(?:npmjs\.com/package|registry\.npmjs\.org)/((?:@[\w.-]+/)?[\w.-]+)")),
)
# Une commande s'arrête au bout de sa ligne, ou de son code en ligne.
_INSTALLS = (
    (
        "pypi",
        re.compile(
            r"\b(?:(?:pip3?|pipx|uv pip|uv tool) (?:install|download)|uv add|uvx)\b([^`\n]*)"
        ),
    ),
    ("npm", re.compile(r"\b(?:npm (?:install|i|add)|npx|yarn add|pnpm add)\b([^`\n]*)")),
)
_IMAGE = re.compile(r"\b(?:ghcr\.io|docker\.io|quay\.io|gcr\.io|public\.ecr\.aws)/[a-z0-9._/-]+")
# Options de pip qui prennent une valeur dans le mot suivant : la valeur n'est pas un paquet.
_OPTIONS_WITH_VALUE = frozenset(
    {"-c", "-d", "-f", "-i", "-r", "-t", "--constraint", "--dest", "--extra-index-url"}
    | {"--find-links", "--index-url", "--prefix", "--python", "--requirement", "--root"}
)
_PACKAGE = re.compile(r"((?:@[\w.-]+/)?[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?(?:[=<>!~;@].*)?")


def installed_packages(arguments: str) -> list[str]:
    """Les paquets qu'une commande d'installation nomme, sans ses options ni ses fichiers."""
    names = []
    value = False
    for word in re.split(r"[\s`|;&>]+", arguments):
        token = word.strip("'\"")
        if value or not token:
            value = False
            continue
        if token.startswith("-"):
            value = "=" not in token and token in _OPTIONS_WITH_VALUE
            continue
        is_file = token.endswith((".whl", ".tar.gz", ".txt", ".toml"))
        if (
            token.startswith(("$", ".", "/"))
            or is_file
            or "/" in token.lstrip("@").partition("/")[2]
        ):
            continue
        match = _PACKAGE.fullmatch(token)
        if match is not None:
            names.append(match.group(1))
    return names


def test_an_install_command_names_its_packages_without_options_or_files() -> None:
    command = (
        '--no-index --find-links paquets --dest=sortie -r exigences.txt "exemple-api==1.2.3" '
        "exemple[extra]>=2 ./local paquet-1.0-py3-none-any.whl $PAQUET @portee/outil"
    )
    assert installed_packages(command) == ["exemple-api", "exemple", "@portee/outil"]


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda path: str(path.relative_to(ROOT)))
def test_the_documentation_cites_only_the_published_distribution_names(document: Path) -> None:
    """Un nom cité mais non réservé, comme un ancien nom de travail, peut être pris par un tiers."""
    names = distribution_names(ROOT)
    allowed = {"pypi": {normalize(name) for name in names.pypi}, "npm": set(names.npm)}
    # Une commande écrite sur plusieurs lignes n'en fait qu'une.
    text = document.read_text(encoding="utf-8").replace("\\\n", " ")
    cited = [
        (registry, name)
        for registry, pattern in _REGISTRY_ADDRESSES
        for name in pattern.findall(text)
    ]
    cited += [
        (registry, name)
        for registry, pattern in _INSTALLS
        for arguments in pattern.findall(text)
        for name in installed_packages(arguments)
    ]
    for registry, name in cited:
        key = normalize(name) if registry == "pypi" else name
        assert key in allowed[registry], f"{document.name} cite {name}, qui n'est pas publié"
    namespaces = tuple(image.rpartition("/")[0] + "/" for image in names.images)
    for found in _IMAGE.findall(text):
        image = found.rstrip("./-")
        if image.startswith(namespaces) or "answerlineage" in image:
            assert image in names.images, f"{document.name} cite l'image {image}, non publiée"


def test_the_publication_guide_lists_every_name_to_publish_or_reserve() -> None:
    text = (ROOT / "docs" / "publication.md").read_text(encoding="utf-8")
    rows = re.findall(
        r"^\| (PyPI|npm|Registre de conteneurs de GitHub) \| `([^`]+)` \|", text, re.MULTILINE
    )
    names = distribution_names(ROOT)
    listed = {
        registry: sorted(name for found, name in rows if found == registry)
        for registry in ("PyPI", "npm", "Registre de conteneurs de GitHub")
    }
    assert listed == {
        "PyPI": sorted(names.pypi),
        "npm": sorted(names.npm),
        "Registre de conteneurs de GitHub": sorted(names.images),
    }
