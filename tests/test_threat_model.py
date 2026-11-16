"""Le modèle de menaces suit le dépôt : chaque composant y a ses menaces, chaque menace sa parade
prouvée par un test ou son acceptation, et une version ne sort que revue et toute signée."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from answerlineage_corpus.abuse import load_abuse_corpus
from scripts import check_threat_model
from scripts.check_agent_config import is_protected
from scripts.check_threat_model import (
    MODEL,
    Reference,
    main,
    owners,
    parse,
    problems,
    read,
    reference,
    release_problems,
    repository_components,
    repository_problems,
    review_problems,
    unsigned,
)
from tests.test_documentation import sections

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "docs" / "securite" / "modele-de-menaces.md"
OWNER = "@mainteneur"
PARADE = "src/parade.py::parer"
PROOF = "tests/test_parade.py::test_la_parade_tient"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def model_text(
    components: Mapping[str, str],
    *,
    version: str = "1.2.3",
    accepted_by: str | None = OWNER,
) -> str:
    """Un modèle conforme : une menace parée par composant, et une acceptée, signée ou non."""
    text = (
        f'[review]\nversion = "{version}"\ndate = 2026-11-16\n\n'
        '[[boundary]]\nid = "frontiere"\nname = "Une frontière"\n'
        'description = "Ce qui la traverse."\n\n'
        '[[asset]]\nid = "actif"\nname = "Un actif"\ndescription = "Ce qu\'il vaut."\n'
    )
    for identifier, path in components.items():
        text += (
            f'\n[[component]]\nid = "{identifier}"\npath = "{path}"\n'
            f'name = "Le composant {identifier}"\n'
        )
    for identifier in components:
        text += (
            f'\n[[threat]]\nid = "{identifier}/menace"\nname = "Une menace"\n'
            'category = "tampering"\nboundary = "frontiere"\nassets = ["actif"]\n'
            'description = "Le scénario."\n\n'
            f'[[threat.mitigation]]\ndescription = "La parade."\ncode = "{PARADE}"\n'
            f'test = "{PROOF}"\n'
        )
    first = next(iter(components))
    text += (
        f'\n[[threat]]\nid = "{first}/risque-accepte"\nname = "Un risque accepté"\n'
        'category = "spoofing"\nboundary = "frontiere"\nassets = ["actif"]\n'
        'description = "Le scénario."\nabuse_cases = ["famille/cas"]\n\n'
        '[threat.acceptance]\nreason = "Le motif."\nconsequence = "La conséquence."\n'
    )
    if accepted_by is not None:
        text += f'accepted_by = "{accepted_by}"\naccepted_on = 2026-11-16\n'
    return text


COMPONENTS = {"api": "api", "workers": "workers", "ui": "ui", "pipeline": ".github/workflows"}


def write_threat_model(
    root: Path,
    components: Mapping[str, str],
    *,
    version: str = "1.2.3",
    accepted_by: str | None = OWNER,
) -> None:
    """Le modèle de menaces d'un dépôt d'essai, son propriétaire, sa parade et son test."""
    write(root / ".github" / "CODEOWNERS", f"/scripts/  @autre\n/{MODEL}  {OWNER}  # le modèle\n")
    write(root / "src" / "parade.py", "def parer() -> None:\n    pass\n")
    write(root / "tests" / "test_parade.py", "def test_la_parade_tient() -> None:\n    pass\n")
    write(root / MODEL, model_text(components, version=version, accepted_by=accepted_by))


def repository(root: Path, *, version: str = "1.2.3", accepted_by: str | None = OWNER) -> Path:
    """Un dépôt à deux membres uv, un paquet npm et un pipeline, avec son modèle de menaces."""
    write(root / "pyproject.toml", '[tool.uv.workspace]\nmembers = ["api", "workers"]\n')
    for member in ("api", "workers"):
        write(root / member / "pyproject.toml", "")
    write(root / "ui" / "package.json", "{}")
    write(root / ".github" / "workflows" / "ci.yml", "name: ci\n")
    write_threat_model(root, COMPONENTS, version=version, accepted_by=accepted_by)
    return root


def document(**options: Any) -> dict[str, Any]:
    return tomllib.loads(model_text(COMPONENTS, **options))


# Un modèle conforme


def test_a_model_that_covers_every_component_of_the_repository_passes(tmp_path: Path) -> None:
    root = repository(tmp_path)
    assert problems(root) == []
    model, found = read(root)
    assert found == []
    assert model is not None
    assert [threat.id for threat in model.threats] == [
        "api/menace",
        "workers/menace",
        "ui/menace",
        "pipeline/menace",
        "api/risque-accepte",
    ]
    assert model.threats[0].component == "api"
    assert model.threats[0].mitigations[0].code == Reference("src/parade.py", "parer")
    assert model.threats[-1].abuse_cases == ("famille/cas",)


def test_the_components_are_the_uv_members_the_npm_packages_and_the_pipeline(
    tmp_path: Path,
) -> None:
    root = repository(tmp_path)
    write(root / "pyproject.toml", '[tool.uv.workspace]\nmembers = ["./api", "paquets/*", 3]\n')
    write(root / "paquets" / "un" / "pyproject.toml", "")
    (root / "paquets" / "pas-un-membre").mkdir()
    assert repository_components(root) == [".github/workflows", "api", "paquets/un", "ui"]


# Liste de contrôle de la merge request : un composant nouveau doit entrer au modèle


def test_a_component_merged_without_updating_the_model_fails_and_is_named(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repository(tmp_path)
    write(root / "pyproject.toml", '[tool.uv.workspace]\nmembers = ["api", "workers", "agents"]\n')
    write(root / "agents" / "pyproject.toml", "")
    write(root / "site" / "package.json", "{}")
    found = problems(root)
    assert len(found) == 2
    assert found[0].startswith("le composant agents n'est pas dans le modèle de menaces")
    assert found[1].startswith("le composant site n'est pas dans le modèle de menaces")
    assert main(root) == 1
    out = capsys.readouterr().out
    assert "agents" in out
    assert "2 écart(s) au modèle de menaces" in out


def test_a_pipeline_added_to_a_repository_without_one_must_enter_the_model(tmp_path: Path) -> None:
    root = repository(tmp_path)
    write(root / MODEL, model_text({"api": "api", "workers": "workers", "ui": "ui"}))
    assert problems(root) == [
        f"le composant .github/workflows n'est pas dans le modèle de menaces : ajoutez-le à "
        f"{MODEL}, avec ses menaces"
    ]


def test_a_component_that_left_the_repository_or_has_no_threat_fails(tmp_path: Path) -> None:
    root = repository(tmp_path)
    text = model_text(COMPONENTS)
    text = text.replace('id = "ui/menace"', 'id = "api/autre-menace"')
    text += '\n[[component]]\nid = "ancien"\npath = "ancien"\nname = "Un composant retiré"\n'
    text += (
        '\n[[threat]]\nid = "ancien/menace"\nname = "Une menace"\ncategory = "tampering"\n'
        'boundary = "frontiere"\nassets = ["actif"]\ndescription = "Le scénario."\n\n'
        '[threat.acceptance]\nreason = "Le motif."\nconsequence = "La conséquence."\n'
    )
    write(root / MODEL, text)
    assert problems(root) == [
        "le composant ui n'a aucune menace",
        "le composant ancien du modèle (ancien) n'est plus un composant du dépôt",
    ]


def test_a_hostile_component_name_cannot_inject_pipeline_commands(tmp_path: Path) -> None:
    root = repository(tmp_path)
    write(
        root / "pyproject.toml",
        '[tool.uv.workspace]\nmembers = ["api", "workers", "x\\n::set-env name=PATH::/tmp"]\n',
    )
    (found,) = problems(root)
    assert "::" not in found
    assert "non affichable" in found


# Parades : le code qui la porte et le test qui la prouve existent


@pytest.mark.parametrize(
    ("field", "value", "problem"),
    [
        ("code", "src/absent.py", "src/absent.py n'est pas un fichier du dépôt"),
        ("test", "tests/absent.py::test_x", "tests/absent.py n'est pas un fichier du dépôt"),
        ("test", "tests", "tests n'est pas un fichier du dépôt"),
        ("code", "src/parade.py::esquiver", "src/parade.py ne définit pas esquiver"),
        ("test", "tests/test_parade.py::test_la_parade", "ne définit pas test_la_parade"),
    ],
)
def test_a_mitigation_whose_code_or_test_does_not_exist_fails(
    tmp_path: Path, field: str, value: str, problem: str
) -> None:
    root = repository(tmp_path)
    old = PARADE if field == "code" else PROOF
    text = model_text(COMPONENTS).replace(f'{field} = "{old}"', f'{field} = "{value}"', 1)
    write(root / MODEL, text)
    (found,) = problems(root)
    assert found.startswith("menace api/menace, parade n°1 : ")
    assert found.endswith(problem)


def test_a_file_too_large_or_unreadable_is_not_searched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = repository(tmp_path)
    (root / "src" / "parade.py").write_bytes(b"\xff\xfe")
    assert all("illisible (UnicodeDecodeError)" in found for found in problems(root))
    monkeypatch.setattr(check_threat_model, "_SOURCE_LIMIT", 1)
    assert all("trop grand pour y chercher" in found for found in problems(root))


@pytest.mark.parametrize(
    "source",
    [
        "def parer(x):\n",
        "async def parer() -> None:\n",
        "class parer:\n",
        "    def parer(self) -> None:\n",
        "parer() {\n  echo\n}\n",
        "export function parer(hash: string): Route {\n",
        "async function parer() {\n",
    ],
)
def test_a_named_definition_is_found_in_python_shell_or_typescript(
    tmp_path: Path, source: str
) -> None:
    root = repository(tmp_path)
    write(root / "src" / "parade.py", source)
    assert problems(root) == []


@pytest.mark.parametrize(
    "source", ["def parera():\n", "# def parer():\n", "x = parer()\n", "parer_aussi() {\n"]
)
def test_a_mere_mention_is_not_a_definition(tmp_path: Path, source: str) -> None:
    root = repository(tmp_path)
    write(root / "src" / "parade.py", source)
    assert len(problems(root)) == len(COMPONENTS)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("api/src/a.py", Reference("api/src/a.py")),
        ("tests/test_a.py::test_b", Reference("tests/test_a.py", "test_b")),
        (".github/workflows/ci.yml", Reference(".github/workflows/ci.yml")),
        ("/etc/passwd", None),
        ("../dehors.py", None),
        ("api/./a.py", None),
        ("api//a.py", None),
        ("api/a.py::", None),
        ("api/a.py::nom-avec-tiret", None),
        ("api/a.py::b::c", None),
        ("api/a b.py", None),
        ("api/a.py\n::set-env", None),
        ("a" * 301, None),
        (3, None),
    ],
)
def test_a_reference_is_a_relative_path_of_the_repository_and_a_name(
    value: object, expected: Reference | None
) -> None:
    found = reference(value)
    assert found == expected
    if found is not None:
        assert str(found) == value


# Acceptations : signées par le mainteneur, propriétaire du modèle


def test_an_acceptance_signed_by_the_owner_of_the_model_holds(tmp_path: Path) -> None:
    root = repository(tmp_path)
    assert owners(root) == {OWNER}
    model, _ = read(root)
    assert model is not None
    assert unsigned(model) == []


def test_an_acceptance_signed_by_someone_else_fails(tmp_path: Path) -> None:
    root = repository(tmp_path, accepted_by="@contributeur")
    assert problems(root) == [
        "menace api/risque-accepte : l'acceptation est signée par @contributeur, qui n'est pas "
        "propriétaire du modèle dans .github/CODEOWNERS"
    ]


def test_without_an_owner_of_the_model_no_acceptance_can_be_signed(tmp_path: Path) -> None:
    root = repository(tmp_path)
    write(root / ".github" / "CODEOWNERS", "/scripts/  @mainteneur\n")
    assert owners(root) == set()
    (root / ".github" / "CODEOWNERS").unlink()
    assert owners(root) == set()
    assert len(problems(root)) == 1


def test_a_folder_rule_of_codeowners_covers_the_model(tmp_path: Path) -> None:
    root = repository(tmp_path)
    write(root / ".github" / "CODEOWNERS", "/docs/  @documentaliste\n/docs/securite/  @rssi\n")
    assert owners(root) == {"@rssi"}


def test_an_acceptance_left_to_sign_is_allowed_on_a_merge_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repository(tmp_path, accepted_by=None)
    assert problems(root) == []
    assert main(root) == 0
    out = capsys.readouterr().out
    assert "Modèle de menaces à jour : 5 menaces, 4 composants, revu pour la version 1.2.3." in out
    assert "1 acceptation(s) à signer par le mainteneur avant la publication :" in out
    assert "  api/risque-accepte" in out


def test_a_model_with_nothing_left_to_sign_says_only_that_it_is_up_to_date(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(repository(tmp_path)) == 0
    assert "à signer" not in capsys.readouterr().out


# Publication d'une version : revue pour elle, rien à signer


def test_a_model_reviewed_for_the_version_with_everything_signed_lets_it_be_published(
    tmp_path: Path,
) -> None:
    assert release_problems("1.2.3", repository(tmp_path)) == []


def test_a_model_reviewed_for_another_version_stops_the_publication(tmp_path: Path) -> None:
    root = repository(tmp_path, version="1.2.2")
    assert release_problems("1.2.3", root) == [
        "le modèle a été revu pour la version 1.2.2, pas pour 1.2.3"
    ]


def test_an_acceptance_left_to_sign_stops_the_publication_and_is_named(tmp_path: Path) -> None:
    root = repository(tmp_path, accepted_by=None)
    assert release_problems("1.2.3", root) == [
        "menace api/risque-accepte : acceptation à signer par le mainteneur"
    ]


def test_a_threat_without_its_proof_stops_the_publication(tmp_path: Path) -> None:
    root = repository(tmp_path)
    (root / "tests" / "test_parade.py").unlink()
    assert len(release_problems("1.2.3", root)) == len(COMPONENTS)


def test_an_unreadable_model_stops_the_publication(tmp_path: Path) -> None:
    root = repository(tmp_path)
    (root / MODEL).unlink()
    assert release_problems("1.2.3", root) == [
        f"{MODEL} absent : le dépôt n'a pas de modèle de menaces"
    ]


def test_a_version_that_was_never_reviewed_says_so() -> None:
    model, _ = parse({})
    assert review_problems(model, "1.2.3") == [
        "le modèle a été revu pour la version (aucune), pas pour 1.2.3"
    ]


# Forme du modèle


def mutated(change: Callable[[dict[str, Any]], object]) -> list[str]:
    raw = document()
    change(raw)
    return parse(raw)[1]


def threat(raw: dict[str, Any], number: int = 0) -> dict[str, Any]:
    found: dict[str, Any] = raw["threat"][number]
    return found


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (lambda raw: raw.pop("review"), "modèle : review manque"),
        (lambda raw: raw.update(extra=1), "modèle : clé inconnue « extra »"),
        (lambda raw: raw.update(review=[]), "[review] : une table est attendue"),
        (lambda raw: raw["review"].pop("date"), "[review] : date manque"),
        (
            lambda raw: raw["review"].update(version="v1"),
            "[review], version : une version des paquets",
        ),
        (lambda raw: raw["review"].update(date="hier"), "[review], date : une date"),
        (lambda raw: raw.update(boundary={}), "frontière : une liste de tables est attendue"),
        (lambda raw: raw["boundary"].append(3), "frontière n°2 : une table est attendue"),
        (lambda raw: raw["asset"][0].update(id="Actif"), "actif n°1, id : un identifiant"),
        (lambda raw: raw["asset"].append(dict(raw["asset"][0])), "identifiant actif répété"),
        (lambda raw: raw["asset"][0].update(name=""), "actif n°1, name : un texte non vide"),
        (lambda raw: raw["asset"][0].update(name="x" * 201), "plus de 200 caractères"),
        (lambda raw: raw["asset"][0].update(description=3), "un texte non vide est attendu"),
        (lambda raw: raw["component"][0].update(path="../api"), "un chemin relatif du dépôt"),
        (lambda raw: raw["component"][0].update(path="api::x"), "un chemin relatif du dépôt"),
        (
            lambda raw: raw["component"][1].update(path="api"),
            "composant n°2 : identifiant ou chemin répété",
        ),
        (lambda raw: raw["component"][0].pop("name"), "composant n°1 : name manque"),
        (lambda raw: threat(raw).update(id="api"), "menace n°1, id : un identifiant composant"),
        (
            lambda raw: threat(raw).update(id="inconnu/menace"),
            "menace inconnu/menace : le composant de son identifiant est inconnu",
        ),
        (
            lambda raw: threat(raw, 1).update(id="api/menace"),
            "menace api/menace : identifiant répété",
        ),
        (lambda raw: threat(raw).update(category="vol"), "category : catégorie inconnue"),
        (lambda raw: threat(raw).update(boundary="mur"), "boundary : frontière inconnue"),
        (lambda raw: threat(raw).update(assets=[]), "une liste d'actifs non vide est attendue"),
        (lambda raw: threat(raw).update(assets="actif"), "une liste d'actifs non vide"),
        (lambda raw: threat(raw).update(assets=["or"]), "assets : actif inconnu"),
        (lambda raw: threat(raw).update(description="x" * 1001), "plus de 1000 caractères"),
        (lambda raw: threat(raw).pop("mitigation"), "menace api/menace : ni parade ni acceptation"),
        (lambda raw: threat(raw).update(mitigation={}), "une liste de tables est attendue"),
        (lambda raw: threat(raw)["mitigation"][0].pop("test"), "parade n°1 : test manque"),
        (
            lambda raw: threat(raw)["mitigation"][0].update(code="/etc/passwd"),
            "parade n°1, code : un chemin du dépôt attendu",
        ),
        (lambda raw: threat(raw)["mitigation"][0].update(note="x"), "clé inconnue « note »"),
        (lambda raw: threat(raw, 4).update(abuse_cases="x"), "une liste de cas du corpus"),
        (lambda raw: threat(raw, 4).update(abuse_cases=["cas"]), "un cas famille/nom attendu"),
        (lambda raw: threat(raw, 4).update(acceptance=[]), "acceptation : une table est attendue"),
        (lambda raw: threat(raw, 4)["acceptance"].pop("reason"), "acceptation : reason manque"),
        (
            lambda raw: threat(raw, 4)["acceptance"].pop("accepted_on"),
            "une signature porte accepted_by et accepted_on, ou aucun des deux",
        ),
        (
            lambda raw: threat(raw, 4)["acceptance"].update(accepted_by="mainteneur"),
            "accepted_by : un compte GitHub (@nom) attendu",
        ),
        (
            lambda raw: threat(raw, 4)["acceptance"].update(accepted_on="2026-11-16T10:00:00"),
            "accepted_on : une date (AAAA-MM-JJ) est attendue",
        ),
    ],
)
def test_a_malformed_model_fails_and_says_where(
    change: Callable[[dict[str, Any]], object], problem: str
) -> None:
    found = mutated(change)
    assert any(problem in item for item in found), found


def test_a_datetime_is_not_a_signature_date() -> None:
    raw = document()
    signed = tomllib.loads("quand = 2026-11-16T10:00:00")["quand"]
    threat(raw, 4)["acceptance"]["accepted_on"] = signed
    assert any("accepted_on : une date" in item for item in parse(raw)[1])


def test_a_malformed_acceptance_still_counts_as_a_treatment() -> None:
    raw = document()
    threat(raw, 4)["acceptance"].pop("accepted_on")
    found = parse(raw)[1]
    assert not any("ni parade ni acceptation" in item for item in found)


def test_a_key_of_the_model_cannot_inject_pipeline_commands() -> None:
    raw = document()
    raw["review"]["x\n::set-env name=PATH::/tmp"] = 1
    (found,) = parse(raw)[1]
    assert "::" not in found


@pytest.mark.parametrize(
    ("content", "problem"),
    [
        (b"[review\n", "illisible (TOMLDecodeError)"),
        (b"\xff\xfe", "illisible (UnicodeDecodeError)"),
    ],
)
def test_an_unreadable_model_fails_without_echoing_its_content(
    tmp_path: Path, content: bytes, problem: str, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repository(tmp_path)
    (root / MODEL).write_bytes(content)
    assert problems(root) == [f"{MODEL} {problem}"]
    assert main(root) == 1
    assert "1 écart(s)" in capsys.readouterr().out


def test_a_model_beyond_its_size_is_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = repository(tmp_path)
    monkeypatch.setattr(check_threat_model, "_MAX_BYTES", 10)
    (found,) = problems(root)
    assert found.endswith("plus de 10 octets, il n'est pas lu")


# Le modèle du dépôt


def repository_model() -> check_threat_model.Model:
    model, found = read(ROOT)
    assert found == []
    assert model is not None
    return model


def test_the_model_of_the_repository_covers_every_component_with_proven_mitigations() -> None:
    assert repository_problems(repository_model(), ROOT) == []


def test_the_model_is_a_protected_path() -> None:
    assert is_protected(MODEL)


def test_every_open_case_of_the_abuse_corpus_is_cited_by_a_threat_that_accepts_it() -> None:
    model = repository_model()
    opened = load_abuse_corpus().open_cases
    assert opened
    for case in opened:
        citing = [threat for threat in model.threats if case.id in threat.abuse_cases]
        assert citing, f"le cas ouvert {case.id} n'est cité par aucune menace"
        assert any(threat.acceptance is not None for threat in citing), case.id


def test_every_abuse_case_cited_by_the_model_is_a_case_of_the_corpus() -> None:
    known = {case.id for case in load_abuse_corpus()}
    for threat in repository_model().threats:
        for case in threat.abuse_cases:
            assert case in known, f"{threat.id} cite {case}, qui n'est pas un cas du corpus"


# La page qui présente le modèle

_ROW = re.compile(r"^\| `([a-z0-9-]+/[a-z0-9-]+)` \|", re.MULTILINE)


def test_the_page_presents_every_threat_of_the_model_by_its_name_and_only_them() -> None:
    model = repository_model()
    parts = sections(PAGE.read_text(encoding="utf-8"))
    presented = parts["Menaces par composant"]
    for threat in model.threats:
        assert f"| `{threat.id}` | {threat.name} |" in presented, threat.id
    assert sorted(_ROW.findall(presented)) == sorted(threat.id for threat in model.threats)


def test_the_page_lists_every_acceptance_with_its_reason_and_whether_it_is_signed() -> None:
    model = repository_model()
    listed = sections(PAGE.read_text(encoding="utf-8"))["Acceptations"]
    accepted = [threat for threat in model.threats if threat.acceptance is not None]
    for threat in accepted:
        acceptance = threat.acceptance
        assert acceptance is not None
        state = (
            f"{acceptance.accepted_by}, {acceptance.accepted_on}"
            if acceptance.signed
            else "à signer"
        )
        row = f"| `{threat.id}` | {acceptance.reason} | {acceptance.consequence} | {state} |"
        assert row in listed, threat.id
    assert sorted(_ROW.findall(listed)) == sorted(threat.id for threat in accepted)


def test_the_page_names_every_boundary_asset_and_component_of_the_model() -> None:
    model = repository_model()
    text = PAGE.read_text(encoding="utf-8")
    for entry in (*model.boundaries, *model.assets):
        assert f"| {entry.name} | {entry.description} |" in text, entry.id
    for component in model.components:
        assert f"### {component.name}\n" in text, component.id


def test_the_page_says_for_which_version_the_model_was_reviewed() -> None:
    model = repository_model()
    text = PAGE.read_text(encoding="utf-8")
    assert f"revu pour la version `{model.version}`, le {model.reviewed_on}" in text
