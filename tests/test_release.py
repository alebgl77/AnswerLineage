"""Rien n'est publié tant que l'étiquette, les versions et les paquets ne concordent pas."""

from __future__ import annotations

import io
import json
import tarfile
import zipfile
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest

from scripts import check_release
from scripts.check_release import (
    Component,
    Expectations,
    Version,
    classifier_problems,
    declared_version,
    dist_problems,
    distribution_names,
    expectations,
    main,
    parse_python_version,
    parse_tag,
    publication_report,
    publishable_components,
    rehearse,
    requirement,
    tag_problems,
)

ROOT = Path(__file__).resolve().parent.parent
LICENSE = b"Texte de la licence.\n"
SOURCE = "https://github.com/exemple/Depot"
CLASSIFIERS = (
    "Development Status :: 3 - Alpha",
    "Programming Language :: Python :: 3.13",
    "Typing :: Typed",
)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def member(name: str, version: str, dependencies: list[str], *, built: bool = True) -> str:
    module = name.replace("-", "_")
    text = (
        f'[project]\nname = "{name}"\nversion = "{version}"\n'
        f"dependencies = {json.dumps(dependencies)}\n"
    )
    if built:
        text += (
            '\n[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
            f'\n[tool.hatch.build.targets.wheel]\npackages = ["src/{module}"]\n'
        )
    return text


def project(
    tmp_path: Path,
    *,
    api: str = "1.2.3",
    workers: str = "1.2.3",
    pin: str = "==1.2.3",
) -> Path:
    """Un dépôt à deux composants publiables, l'un dépendant de l'autre, et un outil interne."""
    root = tmp_path / "depot"
    write(
        root / "pyproject.toml",
        '[project]\nname = "exemple"\nversion = "0.0.0"\nlicense = "Apache-2.0"\n'
        f'requires-python = ">=3.13"\n\n[project.urls]\nSource = "{SOURCE}"\n\n'
        '[tool.uv.workspace]\nmembers = ["workers", "api", "outil"]\n',
    )
    write(root / "api" / "pyproject.toml", member("exemple-api", api, ["fastapi>=0.1"]))
    write(
        root / "workers" / "pyproject.toml",
        member("exemple-workers", workers, [f"exemple-api{pin}", "sqlalchemy>=2"]),
    )
    write(root / "outil" / "pyproject.toml", member("exemple-outil", "0.1.0", [], built=False))
    (root / "LICENSE").write_bytes(LICENSE)
    write(root / "ui" / "package.json", '{"name": "exemple-ui", "private": true}')
    return root


Headers = Mapping[str, object]
Files = Mapping[str, bytes | None]


def metadata(component: Component, version: Version, headers: Headers | None = None) -> bytes:
    """Les métadonnées d'un paquet conforme ; un en-tête à None est retiré, une liste le répète."""
    fields: dict[str, object] = {
        "Metadata-Version": "2.4",
        "Name": component.name,
        "Version": version.python,
        "Summary": "Un composant de l'exemple.",
        "License-Expression": "Apache-2.0",
        "License-File": "LICENSE",
        "Requires-Python": ">=3.13",
        "Classifier": list(CLASSIFIERS),
        "Project-URL": [f"Source, {SOURCE}"],
        "Requires-Dist": [
            requirement_text.replace("==1.2.3", f"=={version.python}")
            for requirement_text in component.dependencies
        ],
    }
    fields.update(headers or {})
    lines = []
    for key, value in fields.items():
        for item in value if isinstance(value, list) else [value]:
            if item is not None:
                lines.append(f"{key}: {item}")
    return ("\n".join(lines) + "\n\n").encode()


def wheel(
    directory: Path,
    component: Component,
    version: Version,
    *,
    files: Files | None = None,
    headers: Headers | None = None,
) -> Path:
    stem = f"{component.stem}-{version.python}"
    path = directory / f"{stem}-py3-none-any.whl"
    contents: dict[str, bytes | None] = {
        f"{component.module}/__init__.py": b"",
        f"{component.module}/py.typed": b"",
        f"{stem}.dist-info/METADATA": metadata(component, version, headers),
        f"{stem}.dist-info/licenses/LICENSE": LICENSE,
    }
    contents.update(files or {})
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in contents.items():
            if data is not None:
                archive.writestr(name, data)
    return path


def sdist(
    directory: Path,
    component: Component,
    version: Version,
    *,
    files: Files | None = None,
    headers: Headers | None = None,
) -> Path:
    stem = f"{component.stem}-{version.python}"
    path = directory / f"{stem}.tar.gz"
    contents: dict[str, bytes | None] = {
        f"{stem}/pyproject.toml": b"",
        f"{stem}/src/{component.module}/__init__.py": b"",
        f"{stem}/PKG-INFO": metadata(component, version, headers),
        f"{stem}/LICENSE": LICENSE,
    }
    contents.update(files or {})
    with tarfile.open(path, "w:gz") as archive:
        for name, data in contents.items():
            if data is None:
                continue
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path


def dist(directory: Path, components: list[Component], version: Version) -> Path:
    """Une roue et des sources conformes pour chaque composant, et le .gitignore de uv."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ".gitignore").write_text("*\n", encoding="utf-8")
    for component in components:
        wheel(directory, component, version)
        sdist(directory, component, version)
    return directory


EXPECTED = Expectations("Apache-2.0", LICENSE, ">=3.13", SOURCE)
VERSION = Version(1, 2, 3)


@pytest.fixture
def components(tmp_path: Path) -> list[Component]:
    return publishable_components(project(tmp_path))


# Étiquettes et versions


@pytest.mark.parametrize(
    ("tag", "python", "image", "final"),
    [
        ("v1.2.3", "1.2.3", "1.2.3", True),
        ("v0.0.0", "0.0.0", "0.0.0", True),
        ("v10.20.30", "10.20.30", "10.20.30", True),
        ("v1.2.3-alpha.1", "1.2.3a1", "1.2.3-alpha.1", False),
        ("v1.2.3-beta.2", "1.2.3b2", "1.2.3-beta.2", False),
        ("v1.2.3-rc.0", "1.2.3rc0", "1.2.3-rc.0", False),
    ],
)
def test_a_tag_gives_the_version_of_the_packages_and_of_the_image(
    tag: str, python: str, image: str, final: bool
) -> None:
    version = parse_tag(tag)
    assert version is not None
    assert (version.tag, version.python, version.image, version.final) == (
        tag,
        python,
        image,
        final,
    )
    assert parse_python_version(python) == version


@pytest.mark.parametrize(
    "tag",
    [
        "1.2.3",
        "v1.2",
        "v1.2.3.4",
        "v01.2.3",
        "v1.2.3-rc1",
        "v1.2.3-rc",
        "v1.2.3-dev.1",
        "v1.2.3+build",
        "v1.2.3\n",
        " v1.2.3",
        "V1.2.3",
        "v" + "1" * 70 + ".0.0",
        "",
    ],
)
def test_any_other_tag_is_refused(tag: str) -> None:
    assert parse_tag(tag) is None


@pytest.mark.parametrize("text", ["1.2.3-rc.1", "1.2.3.post1", "1.2.3.dev0", "1.2", "v1.2.3"])
def test_a_package_version_outside_the_publishable_forms_is_refused(text: str) -> None:
    assert parse_python_version(text) is None


def test_a_prerelease_precedes_its_final_version() -> None:
    order = ["v1.2.2", "v1.2.3-alpha.1", "v1.2.3-alpha.2", "v1.2.3-beta.1", "v1.2.3-rc.1", "v1.2.3"]
    versions = [parse_tag(tag) for tag in order]
    assert versions == sorted(versions, key=lambda version: version or Version(0, 0, 0))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("exemple-api==1.2.3", ("exemple-api", "==1.2.3")),
        ("Exemple_API == 1.2.3", ("exemple-api", "==1.2.3")),
        ("psycopg[binary]>=3.2", ("psycopg", ">=3.2")),
        ("exemple-api ; python_version >= '3.13'", ("exemple-api", "")),
        ("!!!", ("!!!", "")),
    ],
)
def test_a_dependency_reads_as_its_normalized_name_and_constraint(
    text: str, expected: tuple[str, str]
) -> None:
    assert requirement(text) == expected


# Composants publiables


def test_the_publishable_components_are_the_members_that_build(tmp_path: Path) -> None:
    found = publishable_components(project(tmp_path))
    assert [component.name for component in found] == ["exemple-api", "exemple-workers"]
    assert [component.module for component in found] == ["exemple_api", "exemple_workers"]
    assert found[1].dependencies == ("exemple-api==1.2.3", "sqlalchemy>=2")


def test_every_publishable_component_of_the_repository_ships_its_module() -> None:
    found = publishable_components(ROOT)
    assert found
    for component in found:
        assert (ROOT / component.directory / "src" / component.module / "__init__.py").is_file()


def test_the_repository_declares_one_version_and_pins_the_siblings_to_it() -> None:
    version = declared_version(ROOT)
    assert tag_problems(version, publishable_components(ROOT)) == []


def test_the_declared_version_is_the_one_of_every_component(tmp_path: Path) -> None:
    assert declared_version(project(tmp_path)) == VERSION


def test_components_that_disagree_have_no_declared_version(tmp_path: Path) -> None:
    root = project(tmp_path, workers="1.2.4")
    with pytest.raises(ValueError, match="plusieurs versions"):
        declared_version(root)


def test_a_declared_version_outside_the_publishable_forms_is_refused(tmp_path: Path) -> None:
    root = project(tmp_path, api="1.2.3.dev0", workers="1.2.3.dev0", pin="==1.2.3.dev0")
    with pytest.raises(ValueError, match="forme publiable"):
        declared_version(root)


# Concordance de l'étiquette


def test_a_tag_of_the_declared_version_is_accepted(components: list[Component]) -> None:
    assert tag_problems(VERSION, components) == []


def test_a_tag_of_another_version_is_refused(components: list[Component]) -> None:
    problems = tag_problems(Version(1, 2, 4), components)
    assert len(problems) == 3
    assert "exemple-api déclare la version 1.2.3, l'étiquette v1.2.4 demande 1.2.4" in problems
    assert any("exemple-workers doit dépendre de exemple-api==1.2.4" in p for p in problems)


@pytest.mark.parametrize("pin", ["", ">=1.2.3", "==1.2", "~=1.2.3"])
def test_a_sibling_must_be_pinned_to_the_same_version(tmp_path: Path, pin: str) -> None:
    found = publishable_components(project(tmp_path, pin=pin))
    (problem,) = tag_problems(VERSION, found)
    assert problem.startswith("exemple-workers doit dépendre de exemple-api==1.2.3")


def test_without_publishable_component_there_is_nothing_to_publish() -> None:
    assert tag_problems(VERSION, []) == ["aucun composant publiable : rien à publier"]


# Classifieurs


def test_consistent_classifiers_pass() -> None:
    assert classifier_problems(CLASSIFIERS, ">=3.13", typed=True) == []


@pytest.mark.parametrize(
    ("classifiers", "typed", "fragment"),
    [
        ((), True, "aucun classifieur"),
        (("Typing :: Typed",), True, "aucune version de Python annoncée"),
        ((*CLASSIFIERS, "License :: OSI Approved :: Apache Software License"), True, "deux fois"),
        ((*CLASSIFIERS, "Private :: Do Not Upload"), True, "PyPI refuse le paquet"),
        ((*CLASSIFIERS, "Programming Language :: Python :: 3.12"), True, "Python 3.12 annoncé"),
        (CLASSIFIERS[:2], True, "py.typed livré sans le classifieur"),
        (CLASSIFIERS, False, "Typing :: Typed sans py.typed"),
    ],
)
def test_classifiers_that_pypi_refuses_or_that_contradict_the_package_fail(
    classifiers: tuple[str, ...], typed: bool, fragment: str
) -> None:
    problems = classifier_problems(classifiers, ">=3.13", typed=typed)
    assert any(fragment in problem for problem in problems), problems


# Paquets construits


def test_conforming_packages_pass(tmp_path: Path, components: list[Component]) -> None:
    assert (
        dist_problems(dist(tmp_path / "dist", components, VERSION), VERSION, components, EXPECTED)
        == []
    )


def test_a_directory_that_does_not_exist_fails(tmp_path: Path, components: list[Component]) -> None:
    (problem,) = dist_problems(tmp_path / "absent", VERSION, components, EXPECTED)
    assert "n'est pas un dossier" in problem


def test_a_missing_or_unexpected_file_fails(tmp_path: Path, components: list[Component]) -> None:
    directory = dist(tmp_path / "dist", components, VERSION)
    (directory / "exemple_api-1.2.3.tar.gz").unlink()
    (directory / "exemple_outil-0.1.0-py3-none-any.whl").write_bytes(b"")
    assert dist_problems(directory, VERSION, components, EXPECTED) == [
        "exemple_outil-0.1.0-py3-none-any.whl : fichier inattendu",
        "exemple_api-1.2.3.tar.gz : absent",
    ]


def test_packages_of_another_version_fail(tmp_path: Path, components: list[Component]) -> None:
    directory = dist(tmp_path / "dist", components, Version(1, 2, 4))
    problems = dist_problems(directory, VERSION, components, EXPECTED)
    assert "exemple_api-1.2.3-py3-none-any.whl : absent" in problems
    assert "exemple_api-1.2.4-py3-none-any.whl : fichier inattendu" in problems


def test_an_unreadable_package_fails(tmp_path: Path, components: list[Component]) -> None:
    directory = dist(tmp_path / "dist", components, VERSION)
    (directory / "exemple_api-1.2.3-py3-none-any.whl").write_bytes(b"pas une archive")
    (directory / "exemple_api-1.2.3.tar.gz").write_bytes(b"pas une archive")
    assert dist_problems(directory, VERSION, components, EXPECTED) == [
        "exemple_api-1.2.3-py3-none-any.whl : illisible (BadZipFile)",
        "exemple_api-1.2.3.tar.gz : illisible (ReadError)",
    ]


@pytest.mark.parametrize(
    ("headers", "fragment"),
    [
        ({"Metadata-Version": "2.3"}, "Metadata-Version 2.3 : 2.4 au moins"),
        ({"Metadata-Version": None}, "Metadata-Version (absente)"),
        ({"Name": "exemple-autre"}, "Name exemple-autre, exemple-api attendu"),
        ({"Version": "1.2.4"}, "Version 1.2.4, 1.2.3 attendu"),
        ({"License-Expression": "MIT"}, "License-Expression MIT, Apache-2.0 attendu"),
        ({"License-Expression": None}, "License-Expression (absent), Apache-2.0 attendu"),
        ({"Requires-Python": ">=3.12"}, "Requires-Python >=3.12, >=3.13 attendu"),
        ({"License-File": None}, "aucun fichier de licence déclaré"),
        ({"Summary": None}, "aucune description"),
        ({"Summary": "x" * 513}, "description de plus de 512 caractères"),
        ({"Classifier": None}, "aucun classifieur"),
        ({"Project-URL": None}, "Project-URL Source"),
        ({"Project-URL": ["Source, https://exemple.org/autre"]}, "Project-URL Source"),
        ({"Project-URL": [f"Source, {SOURCE}", "Issues, http://exemple.org"]}, "Issues sans https"),
        ({"Project-URL": [f"Source, {SOURCE}", "Brouillon"]}, "Brouillon sans https"),
    ],
)
def test_wrong_metadata_fail_in_the_wheel_and_in_the_sources(
    tmp_path: Path, components: list[Component], headers: dict[str, object], fragment: str
) -> None:
    directory = dist(tmp_path / "dist", components, VERSION)
    api = components[0]
    wheel(directory, api, VERSION, headers=headers)
    sdist(directory, api, VERSION, headers=headers)
    problems = dist_problems(directory, VERSION, components, EXPECTED)
    assert any(
        p.startswith("exemple_api-1.2.3-py3-none-any.whl") and fragment in p for p in problems
    )
    assert any(p.startswith("exemple_api-1.2.3.tar.gz") and fragment in p for p in problems)


def test_a_sibling_not_pinned_in_the_metadata_fails(
    tmp_path: Path, components: list[Component]
) -> None:
    directory = dist(tmp_path / "dist", components, VERSION)
    workers = components[1]
    wheel(directory, workers, VERSION, headers={"Requires-Dist": ["exemple-api>=1.2.3"]})
    assert dist_problems(directory, VERSION, components, EXPECTED) == [
        "exemple_workers-1.2.3-py3-none-any.whl : Requires-Dist « exemple-api>=1.2.3 » : "
        "exemple-api==1.2.3 attendu"
    ]


@pytest.mark.parametrize(
    ("files", "problem"),
    [
        ({"tests/test_api.py": b""}, "fichier hors du module exemple_api : tests/test_api.py"),
        ({"exemple_api/__init__.py": None}, "module exemple_api absent"),
        ({"exemple_api-1.2.3.dist-info/METADATA": None}, "métadonnées absentes (METADATA)"),
        (
            {"exemple_api-1.2.3.dist-info/licenses/LICENSE": None},
            "fichier de licence LICENSE déclaré mais absent",
        ),
        (
            {"exemple_api-1.2.3.dist-info/licenses/LICENSE": b"Une autre licence.\n"},
            "fichier de licence LICENSE différent de LICENSE",
        ),
        ({"exemple_api/py.typed": None}, "classifieur Typing :: Typed sans py.typed livré"),
    ],
)
def test_a_wheel_holds_its_module_its_metadata_and_its_license(
    tmp_path: Path,
    components: list[Component],
    files: dict[str, bytes | None],
    problem: str,
) -> None:
    directory = dist(tmp_path / "dist", components, VERSION)
    (directory / "exemple_api-1.2.3-py3-none-any.whl").unlink()
    wheel(directory, components[0], VERSION, files=files)
    assert dist_problems(directory, VERSION, components, EXPECTED) == [
        f"exemple_api-1.2.3-py3-none-any.whl : {problem}"
    ]


@pytest.mark.parametrize(
    ("files", "problem"),
    [
        ({"autre/fichier": b""}, "fichier hors du dossier exemple_api-1.2.3 : autre/fichier"),
        ({"exemple_api-1.2.3/pyproject.toml": None}, "pyproject.toml absent"),
        (
            {"exemple_api-1.2.3/src/exemple_api/__init__.py": None},
            "src/exemple_api/__init__.py absent",
        ),
        ({"exemple_api-1.2.3/PKG-INFO": None}, "métadonnées absentes (PKG-INFO)"),
        ({"exemple_api-1.2.3/LICENSE": None}, "fichier de licence LICENSE déclaré mais absent"),
        (
            {"exemple_api-1.2.3/LICENSE": b"Une autre licence.\n"},
            "fichier de licence LICENSE différent de LICENSE",
        ),
    ],
)
def test_sources_hold_their_project_their_metadata_and_their_license(
    tmp_path: Path,
    components: list[Component],
    files: dict[str, bytes | None],
    problem: str,
) -> None:
    directory = dist(tmp_path / "dist", components, VERSION)
    sdist(directory, components[0], VERSION, files=files)
    assert dist_problems(directory, VERSION, components, EXPECTED) == [
        f"exemple_api-1.2.3.tar.gz : {problem}"
    ]


def test_an_oversized_member_is_not_read(
    tmp_path: Path, components: list[Component], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(check_release, "_MEMBER_LIMIT", 10)
    directory = dist(tmp_path / "dist", components, VERSION)
    problems = dist_problems(directory, VERSION, components, EXPECTED)
    assert problems == [
        f"{name} : illisible (ValueError)"
        for name in sorted(path.name for path in directory.iterdir() if path.name[0] != ".")
    ]


# Répétition à chaque merge request


def test_the_rehearsal_passes_on_a_consistent_project(tmp_path: Path) -> None:
    root = project(tmp_path)
    directory = dist(tmp_path / "dist", publishable_components(root), VERSION)
    assert rehearse(directory, root) == (VERSION, [])


def test_the_rehearsal_fails_without_a_declared_version(tmp_path: Path) -> None:
    root = project(tmp_path, workers="1.2.4")
    version, problems = rehearse(tmp_path / "dist", root)
    assert version is None
    assert len(problems) == 1
    assert "plusieurs versions" in problems[0]


def test_the_rehearsal_fails_when_the_concordance_would_accept_any_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project(tmp_path)
    directory = dist(tmp_path / "dist", publishable_components(root), VERSION)
    monkeypatch.setattr(check_release, "tag_problems", lambda version, components: [])
    assert rehearse(directory, root) == (
        VERSION,
        [
            "l'étiquette v1.2.4 serait acceptée pour la version 1.2.3 : "
            "la concordance ne contrôle rien"
        ],
    )


def test_the_rehearsal_checks_the_built_packages(tmp_path: Path) -> None:
    root = project(tmp_path)
    directory = dist(tmp_path / "dist", publishable_components(root), VERSION)
    (directory / "exemple_api-1.2.3.tar.gz").unlink()
    assert rehearse(directory, root) == (VERSION, ["exemple_api-1.2.3.tar.gz : absent"])


# Noms de distribution


def test_the_distribution_names_come_from_the_project(tmp_path: Path) -> None:
    names = distribution_names(project(tmp_path))
    assert names.pypi == ("exemple", "exemple-api", "exemple-workers")
    assert names.npm == ("exemple", "exemple-ui")
    assert names.images == ("ghcr.io/exemple/depot",)


def test_the_expectations_come_from_the_project(tmp_path: Path) -> None:
    assert expectations(project(tmp_path)) == EXPECTED


# Bilan d'une publication

ALL = {"PREPARE": "success", "IMAGE": "success", "PYPI": "success", "RELEASE": "success"}
IMAGE = "ghcr.io/exemple/depot@sha256:" + "a" * 64


def test_a_complete_publication_says_so() -> None:
    code, summary = publication_report(ALL, "v1.2.3", IMAGE)
    assert code == 0
    assert summary.startswith("## Publication de v1.2.3 : complète")
    assert f"Image : `{IMAGE}`." in summary


def test_a_refused_preparation_publishes_nothing() -> None:
    results = {"PREPARE": "failure", "IMAGE": "skipped", "PYPI": "skipped", "RELEASE": "skipped"}
    code, summary = publication_report(results, "v1.2.3", "")
    assert code == 1
    assert "## Publication de v1.2.3 : rien n'est publié" in summary
    assert "| Préparation | échouée |" in summary
    assert "| PyPI | sautée |" in summary


def test_an_interrupted_pypi_upload_says_what_is_published() -> None:
    results = {**ALL, "PYPI": "failure", "RELEASE": "skipped"}
    code, summary = publication_report(results, "v1.2.3", IMAGE)
    assert code == 1
    assert "## Publication de v1.2.3 : partielle" in summary
    assert f"- l'image est dans le registre : `{IMAGE}` ;" in summary
    assert "une version incomplète se remplace par la suivante" in summary
    assert "- la version GitHub n'est pas créée." in summary


def test_an_unfinished_image_says_it_may_lack_its_proofs() -> None:
    results = {**ALL, "IMAGE": "failure", "PYPI": "skipped", "RELEASE": "skipped"}
    code, summary = publication_report(results, "v1.2.3", IMAGE)
    assert code == 1
    assert "il lui manque sa signature ou ses attestations" in summary
    assert "- les paquets ne sont pas sur PyPI ;" in summary


def test_an_interrupted_github_release_says_a_draft_may_remain() -> None:
    results = {**ALL, "RELEASE": "cancelled"}
    code, summary = publication_report(results, "v1.2.3", IMAGE)
    assert code == 1
    assert "- les paquets sont sur PyPI ;" in summary
    assert "un brouillon a pu rester" in summary
    assert "| Version GitHub | annulée |" in summary


def test_the_report_copies_neither_a_refused_tag_nor_a_malformed_image() -> None:
    hostile = "v1.2.3\n<script>"
    code, summary = publication_report({}, hostile, "ghcr.io/x:latest\n| injecté |")
    assert code == 1
    assert "<script>" not in summary
    assert "injecté" not in summary
    assert summary.startswith("## Publication : rien n'est publié")
    assert "| Préparation | inconnue |" in summary


# Ligne de commande, sur le dépôt lui-même


def test_the_tag_command_accepts_the_declared_version_and_writes_the_outputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    version = declared_version()
    output = tmp_path / "github-output"
    assert main(["tag", version.tag, "--github-output", str(output)]) == 0
    assert output.read_text(encoding="utf-8") == (
        f"version={version.python}\nimage-version={version.image}\nprerelease=false\n"
        if version.final
        else f"version={version.python}\nimage-version={version.image}\nprerelease=true\n"
    )
    assert f"Étiquette {version.tag}" in capsys.readouterr().out


def test_the_tag_command_refuses_another_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    current = declared_version()
    other = replace(current, patch=current.patch + 1)
    output = tmp_path / "github-output"
    assert main(["tag", other.tag, "--github-output", str(output)]) == 1
    assert not output.exists()
    assert "rien ne doit être publié" in capsys.readouterr().out


def test_the_tag_command_refuses_a_malformed_tag(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["tag", "v1.2.3;rm"]) == 2
    out = capsys.readouterr().out
    assert "Étiquette refusée" in out
    assert ";rm" not in out


def test_the_dist_command_checks_the_built_packages(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    version = declared_version()
    components = publishable_components()
    directory = tmp_path / "dist"
    directory.mkdir()
    assert main(["dist", str(directory), "--tag", version.tag]) == 1
    assert f"{len(components) * 2} écart(s)" in capsys.readouterr().out


def test_the_rehearse_command_names_the_fictitious_tag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["rehearse", str(tmp_path / "absent")]) == 1
    assert f"Étiquette fictive {declared_version().tag}" in capsys.readouterr().out


def test_the_rehearse_command_passes_on_conforming_packages(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    version = declared_version()
    components = publishable_components()
    expected = expectations()
    directory = tmp_path / "dist"
    directory.mkdir()
    for component in components:
        stem = f"{component.stem}-{version.python}"
        files: dict[str, bytes | None] = {f"{stem}/LICENSE": expected.license_text}
        headers: dict[str, object] = {
            "License-Expression": expected.license,
            "Requires-Python": expected.requires_python,
            "Project-URL": [f"Source, {expected.source}"],
            "Requires-Dist": list(component.dependencies),
        }
        wheel(
            directory,
            component,
            version,
            files={f"{stem}.dist-info/licenses/LICENSE": expected.license_text},
            headers=headers,
        )
        sdist(directory, component, version, files=files, headers=headers)
    assert main(["rehearse", str(directory)]) == 0
    assert "Publication éprouvée" in capsys.readouterr().out


def test_the_report_command_writes_the_job_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    summary = tmp_path / "summary.md"
    environment = {
        "RESULT_PREPARE": "success",
        "RESULT_IMAGE": "success",
        "RESULT_PYPI": "failure",
        "RESULT_RELEASE": "skipped",
        "TAG": "v1.2.3",
        "IMAGE": IMAGE,
        "GITHUB_STEP_SUMMARY": str(summary),
    }
    assert main(["report"], environment) == 1
    assert "## Publication de v1.2.3 : partielle" in summary.read_text(encoding="utf-8")
    assert "partielle" in capsys.readouterr().out


def test_the_report_command_runs_without_a_job_summary(capsys: pytest.CaptureFixture[str]) -> None:
    environment = {f"RESULT_{key}": "success" for key in ALL} | {"TAG": "v1.2.3"}
    assert main(["report"], environment) == 0
    assert "complète" in capsys.readouterr().out
