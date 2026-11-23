"""Contrôles de la publication d'une version : étiquette, paquets, bilan.

Une version sort quand le mainteneur pose une étiquette ``v1.2.3`` ; le
workflow ``release`` ne publie rien tant que ces contrôles ne sont pas verts :

- l'étiquette a la forme d'une version : ``v1.2.3``, ou une préversion
  ``v1.2.3-alpha.1``, ``v1.2.3-beta.1``, ``v1.2.3-rc.1`` ;
- chaque composant publiable, membre de l'espace de travail qui se construit,
  déclare la version de l'étiquette et dépend de ses frères à cette version
  exactement : un paquet publié n'installe jamais un frère d'une autre version ;
- le modèle de menaces (``scripts/check_threat_model.py``) est bien formé et
  suit le dépôt, il a été revu pour la version de l'étiquette, et aucune
  acceptation n'y reste à signer ;
- la roue et les sources de chaque composant portent les bonnes métadonnées :
  nom, version, licence et son texte, description, classifieurs cohérents,
  adresse des sources, dépendances entre frères ; elles ne contiennent que le
  module du composant ;
- le relevé des vulnérabilités connues de la version
  (``scripts/known_vulnerabilities.py``) est là, à son format, pour l'image,
  les paquets Python et l'explorateur, et n'a ni vulnérabilité bloquante ni
  exception refusée : ``vulnerabilities``, avant que l'image soit poussée, et à
  chaque merge request sur le relevé du commit.

À chaque merge request, ``rehearse`` éprouve les mêmes contrôles avec une
étiquette fictive, celle de la version déclarée ; les acceptations qui restent à
signer y sont dites sans faire échouer la répétition, puisqu'elles se signent en
préparant la version. À la fin d'une publication, ``report`` dit ce qui est
publié et ce qui ne l'est pas.

Usage :
  ``python3 -m scripts.check_release tag ÉTIQUETTE [--github-output FICHIER]``
  ``python3 -m scripts.check_release dist DOSSIER --tag ÉTIQUETTE``
  ``python3 -m scripts.check_release rehearse DOSSIER``
  ``python3 -m scripts.check_release vulnerabilities RELEVÉ.json [--tag ÉTIQUETTE]``
  ``python3 -m scripts.check_release report``

Codes de sortie : 0 conforme, 1 écart, 2 étiquette illisible ou fichier absent.

Bibliothèque standard seule, Python 3.12 au moins : le pipeline le lance avec
l'interpréteur du runner.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tarfile
import tomllib
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from email.message import Message
from email.parser import BytesHeaderParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from scripts import check_threat_model, known_vulnerabilities

ROOT = Path(__file__).resolve().parent.parent

_NUMBER = r"(0|[1-9][0-9]{0,8})"
_TAG = re.compile(rf"v{_NUMBER}\.{_NUMBER}\.{_NUMBER}(?:-(alpha|beta|rc)\.{_NUMBER})?")
_PYTHON_VERSION = re.compile(rf"{_NUMBER}\.{_NUMBER}\.{_NUMBER}(?:(a|b|rc){_NUMBER})?")
_PHASES = ("alpha", "beta", "rc")
_SHORT_PHASES = ("a", "b", "rc")
# Rang d'une version finale : après ses préversions alpha (0), beta (1) et rc (2).
_FINAL = 3
_TAG_LIMIT = 64

# Une dépendance déclarée : nom, extras, contrainte, marqueur d'environnement.
_REQUIREMENT = re.compile(
    r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*([^;]*?)\s*(?:;.*)?"
)
_PYTHON_CLASSIFIER = re.compile(r"Programming Language :: Python :: 3\.([0-9]+)")
_MINIMUM_PYTHON = re.compile(r">=\s*3\.([0-9]+)")
# La licence se déclare par son expression SPDX à partir de la version 2.4 des métadonnées.
_MINIMUM_METADATA = (2, 4)
_SUMMARY_LIMIT = 512
_MEMBER_LIMIT = 4 * 1024 * 1024
_WHEEL_TAG = "py3-none-any"


@dataclass(frozen=True, order=True)
class Version:
    """Une version publiable : finale, ou préversion alpha, beta ou rc."""

    major: int
    minor: int
    patch: int
    phase: int = _FINAL
    number: int = 0

    @property
    def final(self) -> bool:
        return self.phase == _FINAL

    @property
    def image(self) -> str:
        """La version telle que l'étiquette l'écrit, sans son « v » : celle de l'image."""
        base = f"{self.major}.{self.minor}.{self.patch}"
        return base if self.final else f"{base}-{_PHASES[self.phase]}.{self.number}"

    @property
    def tag(self) -> str:
        return f"v{self.image}"

    @property
    def python(self) -> str:
        """La version dans sa forme canonique (PEP 440), celle des paquets."""
        base = f"{self.major}.{self.minor}.{self.patch}"
        return base if self.final else f"{base}{_SHORT_PHASES[self.phase]}{self.number}"


def _version(match: re.Match[str] | None, phases: tuple[str, ...]) -> Version | None:
    if match is None:
        return None
    major, minor, patch, phase, number = match.groups()
    if phase is None:
        return Version(int(major), int(minor), int(patch))
    return Version(int(major), int(minor), int(patch), phases.index(phase), int(number))


def parse_tag(tag: str) -> Version | None:
    """La version d'une étiquette ``v1.2.3`` ou ``v1.2.3-rc.1`` ; ``None`` pour une autre forme."""
    return _version(_TAG.fullmatch(tag) if len(tag) <= _TAG_LIMIT else None, _PHASES)


def parse_python_version(text: str) -> Version | None:
    """La version d'un paquet, ``1.2.3`` ou ``1.2.3rc1`` ; ``None`` pour une autre forme."""
    return _version(
        _PYTHON_VERSION.fullmatch(text) if len(text) <= _TAG_LIMIT else None, _SHORT_PHASES
    )


def normalize(name: str) -> str:
    """Nom de distribution normalisé (PEP 503)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement(text: str) -> tuple[str, str]:
    """Nom normalisé et contrainte de version, sans espaces, d'une dépendance."""
    match = _REQUIREMENT.fullmatch(text)
    if match is None:
        return normalize(text.strip()), ""
    return normalize(match.group(1)), re.sub(r"\s+", "", match.group(2))


@dataclass(frozen=True)
class Component:
    """Un composant publiable : un membre de l'espace de travail qui se construit."""

    directory: str
    name: str
    version: str
    dependencies: tuple[str, ...]
    module: str

    @property
    def stem(self) -> str:
        """Préfixe des fichiers de ses paquets pour une version (PEP 427, PEP 625)."""
        return re.sub(r"[-_.]+", "_", self.name).lower()


def _load(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _wheel_module(document: Mapping[str, Any], name: str) -> str:
    hatch = document.get("tool", {}).get("hatch", {})
    packages = hatch.get("build", {}).get("targets", {}).get("wheel", {}).get("packages", [])
    return Path(packages[0]).name if packages else name.replace("-", "_")


def publishable_components(root: Path = ROOT) -> list[Component]:
    """Les membres de l'espace de travail qui se construisent en paquets, par nom."""
    members = _load(root / "pyproject.toml")["tool"]["uv"]["workspace"]["members"]
    components = []
    for member in members:
        document = _load(root / member / "pyproject.toml")
        if "build-system" not in document:
            continue
        project = document["project"]
        components.append(
            Component(
                directory=member,
                name=project["name"],
                version=str(project.get("version", "")),
                dependencies=tuple(project.get("dependencies", [])),
                module=_wheel_module(document, project["name"]),
            )
        )
    return sorted(components, key=lambda component: component.name)


def declared_version(root: Path = ROOT) -> Version:
    """La version que déclarent tous les composants publiables ; ``ValueError`` sinon."""
    declared = sorted({component.version for component in publishable_components(root)})
    if len(declared) != 1:
        raise ValueError(
            "les composants publiables déclarent plusieurs versions "
            f"({', '.join(declared) or 'aucune'}) : une étiquette n'en désigne qu'une"
        )
    version = parse_python_version(declared[0])
    if version is None:
        raise ValueError(
            f"la version déclarée {declared[0]!r} n'a pas une forme publiable : "
            "1.2.3, 1.2.3a1, 1.2.3b1 ou 1.2.3rc1"
        )
    return version


@dataclass(frozen=True)
class Expectations:
    """Ce que chaque paquet doit porter, d'après le projet."""

    license: str
    license_text: bytes
    requires_python: str
    source: str


def expectations(root: Path = ROOT) -> Expectations:
    project = _load(root / "pyproject.toml")["project"]
    return Expectations(
        license=project["license"],
        license_text=(root / "LICENSE").read_bytes(),
        requires_python=project["requires-python"],
        source=project["urls"]["Source"],
    )


@dataclass(frozen=True)
class Distributions:
    """Les noms sous lesquels le projet publie, ou se réserve, sur chaque registre."""

    pypi: tuple[str, ...]
    npm: tuple[str, ...]
    images: tuple[str, ...]


def distribution_names(root: Path = ROOT) -> Distributions:
    """Le nom de distribution du projet, puis les noms de ce qu'il publie.

    Sur PyPI, le nom du projet et ceux de ses composants publiables ; sur npm,
    le nom du projet et celui de l'explorateur ; au registre de conteneurs de
    GitHub, l'image que le workflow de publication nomme d'après le dépôt.
    """
    project = _load(root / "pyproject.toml")["project"]
    explorer = json.loads((root / "ui" / "package.json").read_text(encoding="utf-8"))
    repository = urlsplit(project["urls"]["Source"]).path.strip("/").lower()
    return Distributions(
        pypi=(project["name"], *(component.name for component in publishable_components(root))),
        npm=(project["name"], explorer["name"]),
        images=(f"ghcr.io/{repository}",),
    )


def tag_problems(version: Version, components: Sequence[Component]) -> list[str]:
    """Écarts entre une étiquette et les versions déclarées par les composants."""
    if not components:
        return ["aucun composant publiable : rien à publier"]
    problems = []
    siblings = {normalize(component.name) for component in components}
    pinned = f"=={version.python}"
    for component in components:
        if component.version != version.python:
            problems.append(
                f"{component.name} déclare la version {component.version or '(aucune)'}, "
                f"l'étiquette {version.tag} demande {version.python}"
            )
        for dependency in component.dependencies:
            name, constraint = requirement(dependency)
            if name in siblings and constraint != pinned:
                problems.append(
                    f"{component.name} doit dépendre de {name}{pinned}, pas de « {dependency} »"
                )
    return problems


def classifier_problems(
    classifiers: Sequence[str], requires_python: str, typed: bool | None
) -> list[str]:
    """Classifieurs absents, refusés par PyPI ou contraires aux autres métadonnées.

    ``typed`` dit si le paquet livre ``py.typed`` ; ``None`` quand on ne le sait pas.
    """
    if not classifiers:
        return ["aucun classifieur"]
    problems = []
    for classifier in classifiers:
        if classifier.startswith("License ::"):
            problems.append(
                f"classifieur « {classifier} » : la licence se déclare par son expression "
                "SPDX, et PyPI refuse qu'elle le soit deux fois"
            )
        elif classifier.startswith("Private ::"):
            problems.append(f"classifieur « {classifier} » : PyPI refuse le paquet")
    pythons = [
        int(match.group(1))
        for match in map(_PYTHON_CLASSIFIER.fullmatch, classifiers)
        if match is not None
    ]
    if not pythons:
        problems.append("aucune version de Python annoncée (Programming Language :: Python :: 3.x)")
    minimum = _MINIMUM_PYTHON.fullmatch(requires_python.strip())
    for minor in pythons:
        if minimum is not None and minor < int(minimum.group(1)):
            problems.append(
                f"Python 3.{minor} annoncé, que Requires-Python {requires_python} exclut"
            )
    announced = "Typing :: Typed" in classifiers
    if typed is True and not announced:
        problems.append("py.typed livré sans le classifieur Typing :: Typed")
    if typed is False and announced:
        problems.append("classifieur Typing :: Typed sans py.typed livré")
    return problems


def _header(metadata: Message, name: str) -> str:
    value = metadata.get(name)
    return "" if value is None else str(value).strip()


def _headers(metadata: Message, name: str) -> list[str]:
    return [str(value).strip() for value in metadata.get_all(name) or []]


def _metadata_version(text: str) -> tuple[int, int]:
    match = re.fullmatch(r"([0-9]{1,3})\.([0-9]{1,3})", text)
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


def metadata_problems(
    metadata: Message,
    component: Component,
    version: Version,
    siblings: set[str],
    expected: Expectations,
    *,
    typed: bool | None,
) -> list[str]:
    """Écarts des métadonnées d'une roue (METADATA) ou de ses sources (PKG-INFO)."""
    problems = []
    found = _header(metadata, "Metadata-Version")
    if _metadata_version(found) < _MINIMUM_METADATA:
        problems.append(
            f"Metadata-Version {found or '(absente)'} : 2.4 au moins pour déclarer la licence "
            "par son expression SPDX"
        )
    checks = (
        ("Name", component.name),
        ("Version", version.python),
        ("License-Expression", expected.license),
        ("Requires-Python", expected.requires_python),
    )
    for header, wanted in checks:
        found = _header(metadata, header)
        if found != wanted:
            problems.append(f"{header} {found or '(absent)'}, {wanted} attendu")
    if not _headers(metadata, "License-File"):
        problems.append("aucun fichier de licence déclaré (License-File)")
    summary = _header(metadata, "Summary")
    if not summary:
        problems.append("aucune description (Summary)")
    elif len(summary) > _SUMMARY_LIMIT:
        problems.append(f"description de plus de {_SUMMARY_LIMIT} caractères (Summary)")
    problems.extend(
        classifier_problems(_headers(metadata, "Classifier"), expected.requires_python, typed)
    )
    urls = {}
    for entry in _headers(metadata, "Project-URL"):
        label, _, url = entry.partition(",")
        urls[label.strip()] = url.strip()
    if urls.get("Source") != expected.source:
        problems.append(f"adresse des sources (Project-URL Source) : {expected.source} attendue")
    problems.extend(
        f"adresse {label} sans https"
        for label, url in sorted(urls.items())
        if not url.startswith("https://")
    )
    pinned = f"=={version.python}"
    for dependency in _headers(metadata, "Requires-Dist"):
        name, constraint = requirement(dependency)
        if name in siblings and constraint != pinned:
            problems.append(f"Requires-Dist « {dependency} » : {name}{pinned} attendu")
    return problems


def _bounded(size: int) -> None:
    if size > _MEMBER_LIMIT:
        raise ValueError(f"fichier de plus de {_MEMBER_LIMIT} octets")


def _parse(data: bytes) -> Message:
    return BytesHeaderParser().parsebytes(data)


def wheel_problems(
    path: Path,
    component: Component,
    version: Version,
    siblings: set[str],
    expected: Expectations,
) -> list[str]:
    stem = f"{component.stem}-{version.python}"
    info = f"{stem}.dist-info/"
    with zipfile.ZipFile(path) as wheel:
        names = set(wheel.namelist())

        def read(name: str) -> bytes | None:
            if name not in names:
                return None
            _bounded(wheel.getinfo(name).file_size)
            return wheel.read(name)

        problems = [
            f"fichier hors du module {component.module} : {name}"
            for name in sorted(names)
            if not name.startswith((f"{component.module}/", info))
        ]
        if f"{component.module}/__init__.py" not in names:
            problems.append(f"module {component.module} absent")
        data = read(f"{info}METADATA")
        if data is None:
            return [*problems, "métadonnées absentes (METADATA)"]
        metadata = _parse(data)
        typed = f"{component.module}/py.typed" in names
        problems.extend(
            metadata_problems(metadata, component, version, siblings, expected, typed=typed)
        )
        for license_file in _headers(metadata, "License-File"):
            content = read(f"{info}licenses/{license_file}")
            if content is None:
                problems.append(f"fichier de licence {license_file} déclaré mais absent")
            elif content != expected.license_text:
                problems.append(f"fichier de licence {license_file} différent de LICENSE")
    return problems


def sdist_problems(
    path: Path,
    component: Component,
    version: Version,
    siblings: set[str],
    expected: Expectations,
) -> list[str]:
    stem = f"{component.stem}-{version.python}"
    with tarfile.open(path, "r:gz") as sdist:
        members = {member.name: member for member in sdist.getmembers()}

        def read(name: str) -> bytes | None:
            member = members.get(name)
            handle = sdist.extractfile(member) if member is not None else None
            if member is None or handle is None:
                return None
            _bounded(member.size)
            return handle.read()

        problems = [
            f"fichier hors du dossier {stem} : {name}"
            for name in sorted(members)
            if not name.startswith(f"{stem}/")
        ]
        for required in ("pyproject.toml", f"src/{component.module}/__init__.py"):
            if f"{stem}/{required}" not in members:
                problems.append(f"{required} absent")
        data = read(f"{stem}/PKG-INFO")
        if data is None:
            return [*problems, "métadonnées absentes (PKG-INFO)"]
        metadata = _parse(data)
        problems.extend(
            metadata_problems(metadata, component, version, siblings, expected, typed=None)
        )
        for license_file in _headers(metadata, "License-File"):
            content = read(f"{stem}/{license_file}")
            if content is None:
                problems.append(f"fichier de licence {license_file} déclaré mais absent")
            elif content != expected.license_text:
                problems.append(f"fichier de licence {license_file} différent de LICENSE")
    return problems


def dist_problems(
    directory: Path,
    version: Version,
    components: Sequence[Component],
    expected: Expectations,
) -> list[str]:
    """Écarts des paquets construits : une roue et des sources par composant, rien d'autre."""
    if not directory.is_dir():
        return [f"{directory} n'est pas un dossier"]
    siblings = {normalize(component.name) for component in components}
    wanted = {}
    for component in components:
        stem = f"{component.stem}-{version.python}"
        wanted[f"{stem}-{_WHEEL_TAG}.whl"] = (component, wheel_problems)
        wanted[f"{stem}.tar.gz"] = (component, sdist_problems)
    # Un fichier caché (le .gitignore que uv écrit) n'est pas publié.
    present = sorted(path.name for path in directory.iterdir() if not path.name.startswith("."))
    problems = [f"{name} : fichier inattendu" for name in present if name not in wanted]
    problems.extend(f"{name} : absent" for name in sorted(wanted) if name not in present)
    for name in present:
        if name not in wanted:
            continue
        component, check = wanted[name]
        try:
            found = check(directory / name, component, version, siblings, expected)
        except (OSError, ValueError, KeyError, zipfile.BadZipFile, tarfile.TarError) as error:
            found = [f"illisible ({type(error).__name__})"]
        problems.extend(f"{name} : {problem}" for problem in found)
    return problems


def threat_model_problems(version: Version, root: Path = ROOT) -> list[str]:
    """Ce que le modèle de menaces oppose à la publication de cette version."""
    return [
        f"modèle de menaces : {problem}"
        for problem in check_threat_model.release_problems(version.python, root)
    ]


def _review_rehearsal(version: Version, other: Version, root: Path) -> list[str]:
    """La revue du modèle de menaces, éprouvée comme la publication la contrôlera.

    Le modèle doit avoir été revu pour la version déclarée, et sa revue doit
    refuser une autre version. Ses formes et ses parades sont contrôlées à
    chaque merge request par ``make policy`` ; ses acceptations à signer, à la
    publication seulement.
    """
    model, found = check_threat_model.read(root)
    if model is None:
        return [f"modèle de menaces : {problem}" for problem in found]
    problems = [
        f"modèle de menaces : {problem}"
        for problem in check_threat_model.review_problems(model, version.python)
    ]
    if not check_threat_model.review_problems(model, other.python):
        problems.append(
            f"le modèle de menaces serait tenu pour revu pour l'étiquette {other.tag} : "
            "sa revue ne contrôle rien"
        )
    return problems


def rehearse(directory: Path, root: Path = ROOT) -> tuple[Version | None, list[str]]:
    """Les contrôles de la publication, avec l'étiquette fictive de la version déclarée.

    Une étiquette d'une autre version doit être refusée : sans quoi le
    contrôle de concordance ne contrôlerait rien.
    """
    try:
        version = declared_version(root)
    except ValueError as error:
        return None, [str(error)]
    components = publishable_components(root)
    problems = tag_problems(version, components)
    other = Version(version.major, version.minor, version.patch + 1)
    if not tag_problems(other, components):
        problems.append(
            f"l'étiquette {other.tag} serait acceptée pour la version {version.python} : "
            "la concordance ne contrôle rien"
        )
    problems.extend(_review_rehearsal(version, other, root))
    problems.extend(dist_problems(directory, version, components, expectations(root)))
    return version, problems


def unsigned_acceptances(root: Path = ROOT) -> list[str]:
    """Les menaces dont l'acceptation attend la signature du mainteneur avant de publier."""
    model, _ = check_threat_model.read(root)
    return [] if model is None else check_threat_model.unsigned(model)


def vulnerability_problems(document: object, version: Version | None) -> list[str]:
    """Ce qui, dans le relevé des vulnérabilités connues, interdit de publier.

    Le relevé doit être à son format, couvrir chaque source, porter la version
    de l'étiquette quand elle est donnée, et n'avoir ni vulnérabilité bloquante
    ni exception refusée. Les valeurs citées ont la forme que son format admet.
    """
    found = known_vulnerabilities.problems(document)
    if found or not isinstance(document, Mapping):
        return [f"relevé : {problem}" for problem in found]
    names = {source["name"] for source in document["sources"]}
    problems = [
        f"relevé : source {name} absente"
        for name in known_vulnerabilities.SOURCES
        if name not in names
    ]
    if version is not None and document["version"] != version.image:
        problems.append(
            f"relevé de la version {document['version']}, l'étiquette {version.tag} "
            f"demande {version.image}"
        )
    problems.extend(
        f"vulnérabilité bloquante : {row['vulnerability']}, {row['package']} "
        f"{row['package_version']} ({row['package_type']}, source {row['source']}), "
        f"corrigée dans : {known_vulnerabilities.fixed_in(row)}"
        for row in document["findings"]
        if row["state"] == "blocking"
    )
    problems.extend(f"exception refusée : {problem}" for problem in document["refused_exceptions"])
    return problems


def _vulnerabilities(path: Path, tag: str | None) -> int:
    version = None if tag is None else parse_tag(tag)
    if tag is not None and version is None:
        print("Étiquette refusée : une version s'écrit v1.2.3 ou v1.2.3-rc.1.")
        return 2
    try:
        document = known_vulnerabilities.read_document(path)
    except known_vulnerabilities.InputError as error:
        print(f"Relevé des vulnérabilités refusé : {error}.")
        return 2
    problems = vulnerability_problems(document, version)
    if problems or not isinstance(document, Mapping):
        return _print(problems, "")
    summary = document["summary"]
    built = ", ".join(sorted({source["database_built_at"] for source in document["sources"]}))
    return _print(
        [],
        f"Relevé des vulnérabilités connues de {document['version']} : aucune bloquante ; "
        f"{summary['excepted']} écartée(s) par une exception, {summary['non_blocking']} non "
        f"bloquante(s) ; base de Grype construite le {built}.",
    )


# Les jobs du workflow de publication, dans l'ordre, et ce que chacun publie.
STAGES = (
    ("PREPARE", "Préparation", "rien : étiquette, modèle de menaces, paquets et notes contrôlés"),
    ("IMAGE", "Image", "l'image, sa signature, son SBOM et sa provenance"),
    ("PYPI", "PyPI", "les paquets et leurs attestations"),
    ("RELEASE", "Version GitHub", "les notes, le SBOM, les paquets et leurs empreintes"),
)
_RESULTS = {
    "success": "réussie",
    "failure": "échouée",
    "cancelled": "annulée",
    "skipped": "sautée",
}
_INTERRUPTED = frozenset({"failure", "cancelled"})
_IMAGE_REFERENCE = re.compile(r"[a-z0-9.-]+(?:/[a-z0-9._-]+)+@sha256:[0-9a-f]{64}")


def _partial(outcome: Mapping[str, str], image: str) -> list[str]:
    """Ce qui est publié et ce qui ne l'est pas, quand la préparation a réussi."""
    lines = []
    if image:
        lines.append(f"- l'image est dans le registre : `{image}` ;")
    if outcome["IMAGE"] != "success":
        lines.append(
            "- la publication de l'image n'a pas abouti : elle n'a peut-être pas été poussée, "
            "ou il lui manque sa signature ou ses attestations ; relancer le job la reprend ;"
        )
    if outcome["PYPI"] == "success":
        lines.append("- les paquets sont sur PyPI ;")
    elif outcome["PYPI"] in _INTERRUPTED:
        lines.append(
            "- la publication sur PyPI s'est interrompue et des fichiers ont pu partir ; PyPI "
            "refuse un fichier déjà reçu : une version incomplète se remplace par la suivante ;"
        )
    else:
        lines.append("- les paquets ne sont pas sur PyPI ;")
    if outcome["RELEASE"] in _INTERRUPTED:
        lines.append(
            "- la version GitHub n'est pas publiée ; un brouillon a pu rester, que le job "
            "remplace quand on le relance."
        )
    else:
        lines.append("- la version GitHub n'est pas créée.")
    return lines


def publication_report(results: Mapping[str, str], tag: str, image: str) -> tuple[int, str]:
    """Code de sortie et bilan, en Markdown, d'une publication.

    ``results`` donne le résultat de chaque job (``success``, ``failure``…),
    ``image`` la référence par empreinte de l'image poussée, vide si elle ne
    l'a pas été ou si son job ne l'a pas transmise. Ni l'une ni l'autre ne
    sont recopiées si elles n'ont pas la forme attendue.
    """
    version = parse_tag(tag)
    title = f"Publication de {version.tag}" if version is not None else "Publication"
    image = image if _IMAGE_REFERENCE.fullmatch(image) else ""
    outcome = {key: results.get(key, "") for key, _, _ in STAGES}
    table = [
        "| Étape | Résultat | Ce qu'elle publie |",
        "| --- | --- | --- |",
        *(
            f"| {label} | {_RESULTS.get(outcome[key], 'inconnue')} | {publishes} |"
            for key, label, publishes in STAGES
        ),
        "",
    ]
    if all(result == "success" for result in outcome.values()):
        return 0, "\n".join([f"## {title} : complète", "", *table, f"Image : `{image}`."])
    if outcome["PREPARE"] != "success":
        return 1, "\n".join(
            [
                f"## {title} : rien n'est publié",
                "",
                *table,
                "L'étiquette, le modèle de menaces, les paquets ou les notes ont été refusés "
                "avant toute publication.",
            ]
        )
    return 1, "\n".join(
        [
            f"## {title} : partielle",
            "",
            *table,
            "Ce qui est publié et ce qui ne l'est pas :",
            "",
            *_partial(outcome, image),
            "",
            "Les jobs en échec se relancent depuis l'exécution du workflow (Re-run failed jobs).",
        ]
    )


def _print(problems: Sequence[str], success: str) -> int:
    for problem in problems:
        print(f"  {problem}")
    if problems:
        print(f"{len(problems)} écart(s) : rien ne doit être publié.")
        return 1
    print(success)
    return 0


def main(
    argv: Sequence[str] | None = None,
    environment: Mapping[str, str] | None = None,
    root: Path = ROOT,
) -> int:
    env = os.environ if environment is None else environment
    parser = argparse.ArgumentParser(prog="python3 -m scripts.check_release")
    commands = parser.add_subparsers(dest="command", required=True)
    tag_command = commands.add_parser("tag", help="concordance de l'étiquette et des versions")
    tag_command.add_argument("tag")
    tag_command.add_argument("--github-output", type=Path)
    dist_command = commands.add_parser("dist", help="métadonnées des paquets construits")
    dist_command.add_argument("directory", type=Path)
    dist_command.add_argument("--tag", required=True)
    rehearse_command = commands.add_parser("rehearse", help="la publication, sans rien publier")
    rehearse_command.add_argument("directory", type=Path)
    vulnerabilities_command = commands.add_parser(
        "vulnerabilities", help="relevé des vulnérabilités connues, sans vulnérabilité bloquante"
    )
    vulnerabilities_command.add_argument("statement", type=Path)
    vulnerabilities_command.add_argument("--tag")
    commands.add_parser("report", help="bilan d'une publication, d'après son environnement")
    arguments = parser.parse_args(argv)

    if arguments.command == "vulnerabilities":
        return _vulnerabilities(arguments.statement, arguments.tag)

    if arguments.command == "report":
        code, summary = publication_report(
            {key: env.get(f"RESULT_{key}", "") for key, _, _ in STAGES},
            env.get("TAG", ""),
            env.get("IMAGE", ""),
        )
        print(summary)
        if env.get("GITHUB_STEP_SUMMARY"):
            with Path(env["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as handle:
                handle.write(summary + "\n")
        return code
    if arguments.command == "rehearse":
        found, problems = rehearse(arguments.directory, root)
        tag = found.tag if found is not None else "(aucune)"
        print(f"Étiquette fictive {tag}, paquets de {arguments.directory} :")
        code = _print(
            problems,
            f"Publication éprouvée : l'étiquette {tag} concorde avec la version déclarée, une "
            "autre étiquette est refusée, le modèle de menaces a été revu pour cette version, "
            "et les paquets sont conformes.",
        )
        pending = unsigned_acceptances(root)
        if pending:
            print(
                f"Avant de publier, le mainteneur signe {len(pending)} acceptation(s) du modèle "
                "de menaces ; la publication les refuse tant qu'elles ne le sont pas :"
            )
            for threat in pending:
                print(f"  {threat}")
        return code

    version = parse_tag(arguments.tag)
    if version is None:
        print(
            "Étiquette refusée : une version s'écrit v1.2.3, ou v1.2.3-alpha.1, "
            "v1.2.3-beta.1, v1.2.3-rc.1 pour une préversion."
        )
        return 2
    components = publishable_components(root)
    if arguments.command == "tag":
        code = _print(
            [*tag_problems(version, components), *threat_model_problems(version, root)],
            f"Étiquette {version.tag} : version {version.python} de "
            f"{len(components)} composant(s), image {version.image} ; modèle de menaces revu "
            "pour cette version, ses acceptations signées.",
        )
        if code == 0 and arguments.github_output is not None:
            with arguments.github_output.open("a", encoding="utf-8") as handle:
                handle.write(f"version={version.python}\n")
                handle.write(f"image-version={version.image}\n")
                handle.write(f"prerelease={'false' if version.final else 'true'}\n")
        return code
    return _print(
        dist_problems(arguments.directory, version, components, expectations(root)),
        f"Paquets conformes : {len(components)} composant(s), version {version.python}.",
    )


if __name__ == "__main__":
    sys.exit(main())
