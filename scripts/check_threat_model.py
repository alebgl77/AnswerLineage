"""Modèle de menaces : bien formé, à jour des composants du dépôt, revu à chaque version.

Le modèle (``docs/securite/modele-de-menaces.toml``) décrit les frontières de
confiance, les actifs, et, pour chaque composant du dépôt, ses menaces. Chaque
menace a des parades, chacune avec le fichier du code qui la porte et le test
qui la prouve, ou une acceptation : son motif, sa conséquence, et la signature
du mainteneur, propriétaire du modèle dans ``.github/CODEOWNERS``. La page
``docs/securite/modele-de-menaces.md`` le présente.

À chaque merge request (``make policy``), ce contrôle échoue dès que :

- le modèle est illisible ou mal formé : clé inconnue, champ manquant ou vide,
  identifiant hors de sa forme ou répété, catégorie, frontière, actif ou
  composant inconnus ;
- une menace n'a ni parade ni acceptation ;
- une parade nomme un fichier absent, ou une fonction que ce fichier ne définit pas ;
- une acceptation est à moitié signée, ou signée par un autre que le propriétaire du modèle ;
- un composant du dépôt (membre de l'espace de travail uv, paquet npm, pipeline)
  manque au modèle ou n'y a aucune menace ; un composant du modèle a quitté le dépôt.

À la publication d'une version, ``scripts/check_release.py`` exige en plus que le
modèle ait été revu pour la version de l'étiquette et qu'aucune acceptation ne
reste à signer : sinon rien n'est publié.

Usage : ``python3 -m scripts.check_threat_model``

Bibliothèque standard seule, Python 3.12 au moins : la publication le lance avec
l'interpréteur du runner.
"""

from __future__ import annotations

import re
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any

from scripts.check_agent_config import printable

ROOT = Path(__file__).resolve().parent.parent
MODEL = "docs/securite/modele-de-menaces.toml"
CODEOWNERS = ".github/CODEOWNERS"
PIPELINE = ".github/workflows"

# Les catégories de STRIDE.
CATEGORIES = (
    "spoofing",
    "tampering",
    "repudiation",
    "information-disclosure",
    "denial-of-service",
    "elevation-of-privilege",
)

_SLUG = r"[a-z0-9]+(?:-[a-z0-9]+)*"
_ID = re.compile(_SLUG)
_THREAT_ID = re.compile(rf"({_SLUG})/{_SLUG}")
_ID_EXPECTED = "un identifiant en minuscules et tirets attendu"
_PATH = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_OWNER = re.compile(r"@[A-Za-z0-9-]{1,39}(?:/[A-Za-z0-9._-]{1,100})?")
_VERSION = re.compile(r"[0-9][0-9a-z.]{0,63}")
_GLOB = re.compile(r"[*?\[]")
_REFERENCE_LIMIT = 300
_NAME_LIMIT = 200
_TEXT_LIMIT = 1000
_MAX_BYTES = 1024 * 1024
_SOURCE_LIMIT = 4 * 1024 * 1024


@dataclass(frozen=True)
class Reference:
    """Un fichier du dépôt, et au besoin ce qu'il définit : ``chemin::nom``."""

    path: str
    name: str | None = None

    def __str__(self) -> str:
        return self.path if self.name is None else f"{self.path}::{self.name}"


@dataclass(frozen=True)
class Mitigation:
    description: str
    code: Reference
    test: Reference


@dataclass(frozen=True)
class Acceptance:
    reason: str
    consequence: str
    accepted_by: str | None
    accepted_on: date | None

    @property
    def signed(self) -> bool:
        return self.accepted_by is not None and self.accepted_on is not None


@dataclass(frozen=True)
class Threat:
    id: str
    name: str
    category: str
    boundary: str
    assets: tuple[str, ...]
    description: str
    mitigations: tuple[Mitigation, ...]
    acceptance: Acceptance | None
    abuse_cases: tuple[str, ...]

    @property
    def component(self) -> str:
        return self.id.partition("/")[0]


@dataclass(frozen=True)
class Entry:
    """Une frontière de confiance ou un actif."""

    id: str
    name: str
    description: str


@dataclass(frozen=True)
class Component:
    id: str
    path: str
    name: str


@dataclass(frozen=True)
class Model:
    version: str
    reviewed_on: date
    boundaries: tuple[Entry, ...]
    assets: tuple[Entry, ...]
    components: tuple[Component, ...]
    threats: tuple[Threat, ...]


def reference(value: object) -> Reference | None:
    """Un chemin relatif du dépôt, sans « . » ni « .. », suivi au besoin de ``::nom``."""
    if not isinstance(value, str) or len(value) > _REFERENCE_LIMIT:
        return None
    path, separator, name = value.partition("::")
    if not _PATH.fullmatch(path) or any(part in {".", ".."} for part in path.split("/")):
        return None
    if separator and not _NAME.fullmatch(name):
        return None
    return Reference(path, name if separator else None)


class _Reader:
    """Lit le document TOML et note chaque écart à la forme du modèle."""

    def __init__(self) -> None:
        self.problems: list[str] = []

    def note(self, where: str, problem: str) -> None:
        self.problems.append(f"{where} : {problem}")

    def table(
        self, value: object, where: str, required: Sequence[str], optional: Sequence[str] = ()
    ) -> Mapping[str, Any] | None:
        if not isinstance(value, dict):
            self.note(where, "une table est attendue")
            return None
        for key in sorted(str(key) for key in value if key not in {*required, *optional}):
            self.note(where, f"clé inconnue « {printable(key)} »")
        missing = [key for key in required if key not in value]
        for key in missing:
            self.note(where, f"{key} manque")
        return None if missing else value

    def tables(self, value: object, where: str) -> list[object]:
        if value is None:
            return []
        if not isinstance(value, list):
            self.note(where, "une liste de tables est attendue")
            return []
        return value

    def text(self, value: object, where: str, limit: int = _TEXT_LIMIT) -> str:
        if not isinstance(value, str) or not value.strip():
            self.note(where, "un texte non vide est attendu")
            return ""
        if len(value) > limit:
            self.note(where, f"plus de {limit} caractères")
            return ""
        return value

    def matching(self, value: object, where: str, pattern: re.Pattern[str], expected: str) -> str:
        if isinstance(value, str) and pattern.fullmatch(value):
            return value
        self.note(where, expected)
        return ""

    def known(
        self, value: object, where: str, known: Sequence[str] | set[str], unknown: str
    ) -> str:
        if isinstance(value, str) and value in known:
            return value
        self.note(where, unknown)
        return ""

    def day(self, value: object, where: str) -> date | None:
        if type(value) is date:
            return value
        self.note(where, "une date (AAAA-MM-JJ) est attendue")
        return None

    def entries(self, values: object, where: str) -> tuple[Entry, ...]:
        found: dict[str, Entry] = {}
        for number, value in enumerate(self.tables(values, where), start=1):
            label = f"{where} n°{number}"
            table = self.table(value, label, ("id", "name", "description"))
            if table is None:
                continue
            identifier = self.matching(table["id"], f"{label}, id", _ID, _ID_EXPECTED)
            name = self.text(table["name"], f"{label}, name", _NAME_LIMIT)
            description = self.text(table["description"], f"{label}, description")
            if identifier in found:
                self.note(label, f"identifiant {identifier} répété")
            elif identifier:
                found[identifier] = Entry(identifier, name, description)
        return tuple(found.values())

    def components(self, values: object) -> tuple[Component, ...]:
        found: dict[str, Component] = {}
        paths: set[str] = set()
        for number, value in enumerate(self.tables(values, "component"), start=1):
            label = f"composant n°{number}"
            table = self.table(value, label, ("id", "path", "name"))
            if table is None:
                continue
            identifier = self.matching(table["id"], f"{label}, id", _ID, _ID_EXPECTED)
            located = reference(table["path"])
            path = located.path if located is not None and located.name is None else ""
            if not path:
                self.note(f"{label}, path", "un chemin relatif du dépôt attendu")
            name = self.text(table["name"], f"{label}, name", _NAME_LIMIT)
            if identifier in found or path in paths:
                self.note(label, "identifiant ou chemin répété")
            elif identifier and path:
                found[identifier] = Component(identifier, path, name)
                paths.add(path)
        return tuple(found.values())

    def mitigation(self, value: object, where: str) -> Mitigation | None:
        table = self.table(value, where, ("description", "code", "test"))
        if table is None:
            return None
        description = self.text(table["description"], f"{where}, description")
        code, test = reference(table["code"]), reference(table["test"])
        for key, found in (("code", code), ("test", test)):
            if found is None:
                self.note(f"{where}, {key}", "un chemin du dépôt attendu, suivi au besoin de ::nom")
        if code is None or test is None:
            return None
        return Mitigation(description, code, test)

    def acceptance(self, value: object, where: str) -> Acceptance | None:
        table = self.table(value, where, ("reason", "consequence"), ("accepted_by", "accepted_on"))
        if table is None:
            return None
        reason = self.text(table["reason"], f"{where}, reason")
        consequence = self.text(table["consequence"], f"{where}, consequence")
        if ("accepted_by" in table) != ("accepted_on" in table):
            self.note(where, "une signature porte accepted_by et accepted_on, ou aucun des deux")
            return None
        if "accepted_by" not in table:
            return Acceptance(reason, consequence, None, None)
        signer = self.matching(
            table["accepted_by"], f"{where}, accepted_by", _OWNER, "un compte GitHub (@nom) attendu"
        )
        signed_on = self.day(table["accepted_on"], f"{where}, accepted_on")
        if not signer or signed_on is None:
            return None
        return Acceptance(reason, consequence, signer, signed_on)

    def threat(
        self,
        value: object,
        label: str,
        boundaries: set[str],
        assets: set[str],
        components: set[str],
    ) -> Threat | None:
        table = self.table(
            value,
            label,
            ("id", "name", "category", "boundary", "assets", "description"),
            ("mitigation", "acceptance", "abuse_cases"),
        )
        if table is None:
            return None
        identifier = self.matching(
            table["id"], f"{label}, id", _THREAT_ID, "un identifiant composant/menace attendu"
        )
        where = f"menace {identifier}" if identifier else label
        if identifier and identifier.partition("/")[0] not in components:
            self.note(where, "le composant de son identifiant est inconnu")
        name = self.text(table["name"], f"{where}, name", _NAME_LIMIT)
        category = self.known(
            table["category"], f"{where}, category", CATEGORIES, "catégorie inconnue"
        )
        boundary = self.known(
            table["boundary"], f"{where}, boundary", boundaries, "frontière inconnue"
        )
        threatened = table["assets"]
        if not isinstance(threatened, list) or not threatened:
            self.note(f"{where}, assets", "une liste d'actifs non vide est attendue")
            threatened = []
        names = tuple(
            self.known(asset, f"{where}, assets", assets, "actif inconnu") for asset in threatened
        )
        description = self.text(table["description"], f"{where}, description")
        mitigations = tuple(
            found
            for number, item in enumerate(
                self.tables(table.get("mitigation"), f"{where}, mitigation"), start=1
            )
            if (found := self.mitigation(item, f"{where}, parade n°{number}")) is not None
        )
        accepted = table.get("acceptance")
        acceptance = (
            None if accepted is None else self.acceptance(accepted, f"{where}, acceptation")
        )
        cases = table.get("abuse_cases", [])
        if not isinstance(cases, list):
            self.note(f"{where}, abuse_cases", "une liste de cas du corpus d'abus est attendue")
            cases = []
        cited = tuple(
            self.matching(case, f"{where}, abuse_cases", _THREAT_ID, "un cas famille/nom attendu")
            for case in cases
        )
        if not mitigations and accepted is None:
            self.note(where, "ni parade ni acceptation")
        if not identifier:
            return None
        return Threat(
            id=identifier,
            name=name,
            category=category,
            boundary=boundary,
            assets=tuple(asset for asset in names if asset),
            description=description,
            mitigations=mitigations,
            acceptance=acceptance,
            abuse_cases=tuple(case for case in cited if case),
        )


def parse(document: Mapping[str, Any]) -> tuple[Model, list[str]]:
    """Le modèle que décrit le document, et les écarts de sa forme."""
    reader = _Reader()
    reader.table(document, "modèle", ("review", "boundary", "asset", "component", "threat"))
    review = (
        reader.table(document["review"], "[review]", ("version", "date"))
        if "review" in document
        else None
    )
    version, reviewed_on = "", date.min
    if review is not None:
        version = reader.matching(
            review["version"], "[review], version", _VERSION, "une version des paquets attendue"
        )
        reviewed_on = reader.day(review["date"], "[review], date") or date.min
    boundaries = reader.entries(document.get("boundary"), "frontière")
    assets = reader.entries(document.get("asset"), "actif")
    components = reader.components(document.get("component"))
    threats: dict[str, Threat] = {}
    for number, value in enumerate(reader.tables(document.get("threat"), "threat"), start=1):
        threat = reader.threat(
            value,
            f"menace n°{number}",
            {entry.id for entry in boundaries},
            {entry.id for entry in assets},
            {component.id for component in components},
        )
        if threat is not None and threat.id in threats:
            reader.note(f"menace {threat.id}", "identifiant répété")
        elif threat is not None:
            threats[threat.id] = threat
    model = Model(version, reviewed_on, boundaries, assets, components, tuple(threats.values()))
    return model, reader.problems


def read(root: Path = ROOT) -> tuple[Model | None, list[str]]:
    """Le modèle du dépôt et les écarts de sa forme ; aucun modèle s'il est illisible."""
    path = root / MODEL
    try:
        if path.stat().st_size > _MAX_BYTES:
            return None, [f"{MODEL} : plus de {_MAX_BYTES} octets, il n'est pas lu"]
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, [f"{MODEL} absent : le dépôt n'a pas de modèle de menaces"]
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        return None, [f"{MODEL} illisible ({type(error).__name__})"]
    return parse(document)


def repository_components(root: Path = ROOT) -> list[str]:
    """Les dossiers des composants : membres de l'espace de travail uv, paquets npm, pipeline."""
    with (root / "pyproject.toml").open("rb") as handle:
        workspace = tomllib.load(handle).get("tool", {}).get("uv", {}).get("workspace", {})
    found: set[str] = set()
    for member in workspace.get("members", []):
        if not isinstance(member, str):
            continue
        if not _GLOB.search(member):
            found.add(PurePosixPath(member).as_posix())
            continue
        found.update(
            path.relative_to(root).as_posix()
            for path in root.glob(member)
            if (path / "pyproject.toml").is_file()
        )
    found.update(path.parent.name for path in root.glob("*/package.json"))
    if (root / PIPELINE).is_dir():
        found.add(PIPELINE)
    return sorted(found)


def owners(root: Path = ROOT) -> set[str]:
    """Les propriétaires du modèle dans ``.github/CODEOWNERS`` : la dernière règle qui le couvre."""
    try:
        content = (root / CODEOWNERS).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()
    found: set[str] = set()
    for line in content.splitlines():
        fields = line.split("#", 1)[0].split()
        if len(fields) < 2:
            continue
        pattern = fields[0].lstrip("/")
        if pattern == MODEL or (pattern.endswith("/") and MODEL.startswith(pattern)):
            found = {owner for owner in fields[1:] if owner.startswith("@")}
    return found


def _defines(text: str, name: str) -> bool:
    """Le texte définit-il ce nom : fonction ou classe Python, fonction shell ou TypeScript ?"""
    definition = re.compile(
        rf"^[ \t]*(?:(?:async[ \t]+)?def|class|(?:export[ \t]+)?(?:async[ \t]+)?function)"
        rf"[ \t]+{name}\b|^[ \t]*{name}[ \t]*\(\)[ \t]*\{{",
        re.MULTILINE,
    )
    return definition.search(text) is not None


def _reference_problem(root: Path, found: Reference) -> str | None:
    path = root / found.path
    if not path.is_file():
        return f"{found.path} n'est pas un fichier du dépôt"
    if found.name is None:
        return None
    try:
        if path.stat().st_size > _SOURCE_LIMIT:
            return f"{found.path} est trop grand pour y chercher {found.name}"
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return f"{found.path} illisible ({type(error).__name__})"
    if not _defines(text, found.name):
        return f"{found.path} ne définit pas {found.name}"
    return None


def repository_problems(model: Model, root: Path = ROOT) -> list[str]:
    """Écarts entre le modèle et le dépôt : composants, fichiers des parades, signataires."""
    problems = []
    present = repository_components(root)
    modelled = {component.path for component in model.components}
    for path in present:
        if path not in modelled:
            problems.append(
                f"le composant {printable(path)} n'est pas dans le modèle de menaces : "
                f"ajoutez-le à {MODEL}, avec ses menaces"
            )
    threatened = {threat.component for threat in model.threats}
    for component in model.components:
        if component.path not in present:
            problems.append(
                f"le composant {component.id} du modèle ({component.path}) n'est plus un "
                "composant du dépôt"
            )
        if component.id not in threatened:
            problems.append(f"le composant {component.id} n'a aucune menace")
    allowed = owners(root)
    for threat in model.threats:
        for number, mitigation in enumerate(threat.mitigations, start=1):
            for found in (mitigation.code, mitigation.test):
                problem = _reference_problem(root, found)
                if problem is not None:
                    problems.append(f"menace {threat.id}, parade n°{number} : {problem}")
        acceptance = threat.acceptance
        if acceptance is not None and acceptance.signed and acceptance.accepted_by not in allowed:
            problems.append(
                f"menace {threat.id} : l'acceptation est signée par {acceptance.accepted_by}, "
                f"qui n'est pas propriétaire du modèle dans {CODEOWNERS}"
            )
    return problems


def problems(root: Path = ROOT) -> list[str]:
    """Ce que vérifie chaque merge request : le modèle est bien formé et suit le dépôt."""
    model, found = read(root)
    if model is None:
        return found
    return [*found, *repository_problems(model, root)]


def review_problems(model: Model, version: str) -> list[str]:
    """Le modèle a-t-il été revu pour cette version des paquets ?"""
    if model.version == version:
        return []
    return [
        f"le modèle a été revu pour la version {model.version or '(aucune)'}, pas pour {version}"
    ]


def unsigned(model: Model) -> list[str]:
    """Les menaces dont l'acceptation attend encore la signature du mainteneur."""
    return [
        threat.id
        for threat in model.threats
        if threat.acceptance is not None and not threat.acceptance.signed
    ]


def release_problems(version: str, root: Path = ROOT) -> list[str]:
    """Ce qui arrête la publication d'une version : en plus de chaque merge request,
    un modèle qui n'a pas été revu pour elle, ou une acceptation qui reste à signer."""
    model, found = read(root)
    if model is None:
        return found
    return [
        *found,
        *repository_problems(model, root),
        *review_problems(model, version),
        *(
            f"menace {threat} : acceptation à signer par le mainteneur"
            for threat in unsigned(model)
        ),
    ]


def main(root: Path = ROOT) -> int:
    model, found = read(root)
    listed = found if model is None else [*found, *repository_problems(model, root)]
    for problem in listed:
        print(f"  {problem}")
    if model is None or listed:
        print(f"{len(listed)} écart(s) au modèle de menaces ({MODEL}).")
        return 1
    print(
        f"Modèle de menaces à jour : {len(model.threats)} menaces, "
        f"{len(model.components)} composants, revu pour la version {model.version}."
    )
    pending = unsigned(model)
    if pending:
        print(f"{len(pending)} acceptation(s) à signer par le mainteneur avant la publication :")
        for threat in pending:
            print(f"  {threat}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
