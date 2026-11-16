"""Les exemples de la documentation restent vrais quand le code change."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from answerlineage_api.lineage.trace import answer_key
from answerlineage_cli.doctor import ANOMALIES, CHECKS
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
from scripts.play_guide import extract

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = sorted(
    [
        *ROOT.glob("*.md"),
        *ROOT.glob("docs/*.md"),
        *ROOT.glob("docs/decisions/*.md"),
        *ROOT.glob("docs/securite/*.md"),
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


@pytest.mark.parametrize("document", ["README.md", "docs/lecture.md", "docs/demarrage.md"])
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


@pytest.mark.parametrize("document", ["README.md", "docs/demonstration.md", "docs/demarrage.md"])
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


# La commande d'exemple, écrite sur une ligne ou plusieurs, et ce qu'elle affiche.
_CLI_IMPACT = re.compile(
    rf"answerlineage impact --namespace {_VALUE} --name {_VALUE} --from {_VALUE} --to {_VALUE}"
)
_CLI_COUNTED = re.compile(r"(\d+) réponses touchées, (\d+) destinataires")


@pytest.mark.parametrize("document", ["docs/cli.md", "docs/demarrage.md"])
def test_the_documented_command_propagates_the_first_incident_of_the_demonstration(
    document: str,
) -> None:
    text = re.sub(r"\\\n\s*", "", (ROOT / document).read_text(encoding="utf-8"))
    first = truth(generate(DEFAULT_SEED))["incidents"][0]
    assert set(_CLI_IMPACT.findall(text)) == {
        (first["dataset"]["namespace"], first["dataset"]["name"], first["from"], first["to"])
    }
    assert set(_CLI_COUNTED.findall(text)) == {
        (str(len(first["answers"])), str(len(first["recipients"])))
    }


def test_every_check_and_anomaly_of_doctor_is_documented() -> None:
    text = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    for name in (*CHECKS, *ANOMALIES):
        assert f"| `{name}` |" in text, name


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


# Les ancres : un lien vers un titre d'un document de la documentation mène à ce titre.
_FENCE_LINE = re.compile(r"^\s*(`{3,}|~{3,})")
_HEADING = re.compile(r"^#{1,6} +(.+?) *#*$")
_ANCHOR_LINK = re.compile(r"\]\((?!https?://|mailto:)([^)#\s]*)#([^)\s]+)\)")


def github_slug(heading: str) -> str:
    """L'ancre que GitHub donne à un titre : minuscules, sans ponctuation, tirets pour espaces."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    return re.sub(r"[^\w\- ]", "", text.lower()).replace(" ", "-")


def anchors(document: Path) -> set[str]:
    """Les ancres d'un fichier Markdown, titres répétés compris (`titre`, `titre-1`…)."""
    found: set[str] = set()
    seen: dict[str, int] = {}
    fenced = False
    for line in document.read_text(encoding="utf-8").splitlines():
        if _FENCE_LINE.match(line):
            fenced = not fenced
        heading = None if fenced else _HEADING.match(line)
        if heading is None:
            continue
        slug = github_slug(heading.group(1))
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        found.add(slug if count == 0 else f"{slug}-{count}")
    return found


def test_an_anchor_is_what_github_makes_of_a_heading() -> None:
    assert github_slug("Boîte d'envoi") == "boîte-denvoi"
    assert github_slug("La dérivation, à l'octet près") == "la-dérivation-à-loctet-près"
    assert github_slug("`answerlineage impact`") == "answerlineage-impact"
    assert github_slug("Écrire dans le [graphe](schema.md)") == "écrire-dans-le-graphe"


def test_a_repeated_heading_gets_a_numbered_anchor_and_a_comment_in_a_block_is_no_heading(
    tmp_path: Path,
) -> None:
    document = tmp_path / "doc.md"
    document.write_text(
        "# Titre\n\n## Suite\n\n```bash\n# pas un titre\n```\n\n## Suite\n", encoding="utf-8"
    )
    assert anchors(document) == {"titre", "suite", "suite-1"}


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda path: str(path.relative_to(ROOT)))
def test_every_anchor_of_a_link_leads_to_a_heading(document: Path) -> None:
    text = document.read_text(encoding="utf-8")
    for target, anchor in _ANCHOR_LINK.findall(text):
        destination = document if not target else (document.parent / target)
        if destination.suffix != ".md":
            continue
        assert anchor in anchors(destination), (
            f"{document.name} : l'ancre #{anchor} n'est pas un titre de {destination.name}"
        )


# Les décisions d'architecture : chacune a ses quatre parties, et l'index les liste toutes.
DECISIONS = sorted((ROOT / "docs" / "decisions").glob("[0-9][0-9][0-9][0-9]-*.md"))
_PARTS = (
    "Contexte",
    "Décision",
    r"Alternatives? écartées?",
    "Conséquences",
)
_DECISION_LINK = re.compile(r"^\| \[(\d{4})\]\(([^)]+)\) \|", re.MULTILINE)


def sections(text: str) -> dict[str, str]:
    """Le texte de chaque titre de deuxième niveau, hors des blocs de code."""
    found: dict[str, list[str]] = {}
    current: list[str] | None = None
    fenced = False
    for line in text.splitlines():
        if _FENCE_LINE.match(line):
            fenced = not fenced
        if not fenced and line.startswith("## "):
            current = found.setdefault(line[3:].strip(), [])
        elif current is not None:
            current.append(line)
    return {title: "\n".join(body).strip() for title, body in found.items()}


def test_there_are_decisions_numbered_without_a_gap() -> None:
    numbers = [path.name[:4] for path in DECISIONS]
    assert numbers
    assert numbers == [f"{number:04d}" for number in range(1, len(numbers) + 1)]


@pytest.mark.parametrize("decision", DECISIONS, ids=lambda path: path.name)
def test_a_decision_has_its_four_parts_in_order(decision: Path) -> None:
    text = decision.read_text(encoding="utf-8")
    assert text.startswith(f"# {decision.name[:4]}. "), "le titre porte le numéro du fichier"
    found = sections(text)
    titles = list(found)
    for part in _PARTS:
        matching = [title for title in titles if re.fullmatch(part, title)]
        assert len(matching) == 1, f"{decision.name} : la partie « {part} » manque ou est double"
        assert len(found[matching[0]]) >= 200, f"{decision.name} : « {matching[0]} » est vide"
    order = [
        next(i for i, title in enumerate(titles) if re.fullmatch(part, title)) for part in _PARTS
    ]
    assert order == sorted(order), f"{decision.name} : les parties ne suivent pas l'ordre"


def test_the_index_lists_every_decision_with_its_title() -> None:
    index = (ROOT / "docs" / "decisions" / "README.md").read_text(encoding="utf-8")
    listed = _DECISION_LINK.findall(index)
    assert [number for number, _ in listed] == [path.name[:4] for path in DECISIONS]
    assert [target for _, target in listed] == [path.name for path in DECISIONS]
    for decision in DECISIONS:
        title = decision.read_text(encoding="utf-8").splitlines()[0].split(". ", 1)[1]
        assert f"| {title} |" in index, f"{decision.name} : l'index ne reprend pas son titre"


# Le guide de démarrage : ses commandes se jouent, et celles qui ne se jouent pas le disent.
GUIDE = ROOT / "docs" / "demarrage.md"
_MAKE_TARGET = re.compile(r"\bmake ([a-z][a-z-]*)")
_COMPOSE_EXEC = re.compile(r"docker compose exec (?:-T )?([a-z]+)")


def test_the_guide_has_commands_to_play_and_one_first_lineage() -> None:
    blocks = extract(GUIDE.read_text(encoding="utf-8"))
    played = [block for block in blocks if block.skip_reason is None]
    assert len(played) >= 5
    assert sum(block.first_lineage for block in played) == 1
    # Ce qui ne se joue pas est l'exception, et dit pourquoi.
    assert all(block.skip_reason for block in blocks if block not in played)


def test_the_commands_of_the_guide_name_targets_and_services_that_exist() -> None:
    blocks = extract(GUIDE.read_text(encoding="utf-8"))
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    targets = set(re.findall(r"^([a-z-]+):", makefile, re.MULTILINE))
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    services = set(re.findall(r"^  ([a-z]+):$", compose, re.MULTILINE))
    for block in blocks:
        for target in _MAKE_TARGET.findall(block.code):
            assert target in targets, f"ligne {block.line} : make {target} n'existe pas"
        for service in _COMPOSE_EXEC.findall(block.code):
            assert service in services, f"ligne {block.line} : le service {service} n'existe pas"


@pytest.mark.parametrize("document", ["README.md", "docs/README.md"])
def test_the_overview_documents_lead_to_the_guide_the_concepts_the_architecture_and_the_decisions(
    document: str,
) -> None:
    text = (ROOT / document).read_text(encoding="utf-8")
    prefix = "docs/" if document == "README.md" else ""
    for target in ("demarrage.md", "concepts.md", "architecture.md", "decisions/README.md"):
        assert f"]({prefix}{target})" in text, f"{document} ne renvoie pas à {target}"
