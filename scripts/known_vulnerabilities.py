"""Relevé des vulnérabilités connues d'une version.

À la publication d'une version, le relevé dit quelles vulnérabilités connues de
ses dépendances la concernent, d'après trois sources, chacune analysée par Grype :

- ``image`` : les paquets du système et les paquets Python installés dans
  l'image, d'après son SBOM (Syft) ;
- ``python`` : les dépendances des paquets Python publiés sur PyPI, d'après
  ``uv.lock`` ;
- ``npm`` : les paquets npm que l'explorateur construit embarque, d'après
  ``ui/package-lock.json``, qui n'entre pas dans l'image.

Syft lit ``uv.lock``, mais il en inventorie chaque entrée : le groupe de
développement (pytest, mypy…), les membres de l'espace de travail et les
paquets de toutes les plateformes, sans les distinguer. L'inventaire Python se
tire donc du verrou ici : les dépendances des membres de l'espace de travail et
celles des extras qu'elles demandent, sans les dépendances de développement.
Les marqueurs d'environnement ne sont pas évalués : un paquet réservé à
Windows y figure, du côté sûr. L'inventaire npm garde les paquets que le verrou
ne marque pas ``dev`` : ceux que la construction embarque. Chaque inventaire
est un SBOM CycloneDX, que Grype analyse comme celui de l'image.

Le relevé reprend chaque vulnérabilité trouvée, quelle que soit sa gravité,
avec son état, selon la règle de ``scripts/check_vulnerabilities.py`` :

- ``blocking`` : haute ou critique, sans exception valable ; la publication
  refuse une version qui en a une (``scripts/check_release.py vulnerabilities``) ;
- ``excepted`` : haute ou critique, écartée par une exception valable de
  ``vulnerability-exceptions.toml``, dont le relevé reprend le motif et la date
  de revue ;
- ``non_blocking`` : gravité moyenne, faible, négligeable ou inconnue.

Il s'écrit en JSON, au format que ``problems`` contrôle et que
``docs/publication.md`` documente, et en Markdown. Il ne reprend du rapport de
Grype que ses identifiants publics : la vulnérabilité et ses alias, sa
gravité, le paquet, sa version et son type, le correctif, la version de Grype
et celle de sa base. Chaque valeur n'est recopiée que si elle a sa forme
admise, sinon elle s'écrit « ? » ; une entrée démesurée est refusée plutôt que
coupée, puisqu'un relevé coupé tairait des vulnérabilités.

Usage :
  ``python3 -m scripts.known_vulnerabilities inventory python [--lock uv.lock] --output FICHIER``
  ``python3 -m scripts.known_vulnerabilities inventory npm [--lock ui/package-lock.json]
  --output FICHIER``
  ``python3 -m scripts.known_vulnerabilities build --version VERSION
  --source image=RAPPORT.json --source python=… --source npm=…
  [--exceptions FICHIER] --json FICHIER --markdown FICHIER``

Codes de sortie : 0 écrit, 2 entrée absente, illisible ou hors de sa forme.

Bibliothèque standard seule, Python 3.12 au moins : le pipeline le lance avec
l'interpréteur du runner.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.check_vulnerabilities import (
    EXCEPTIONS_FILE,
    FIX_STATES,
    SEVERITIES,
    Finding,
    VulnerabilityException,
    evaluate,
    findings,
    load_exceptions,
)

ROOT = Path(__file__).resolve().parent.parent

FORMAT = "answerlineage-known-vulnerabilities"
FORMAT_VERSION = 1
# Les sources, dans l'ordre du relevé.
SOURCES = ("image", "python", "npm")
# Les états, du plus grave au moins grave.
STATES = ("blocking", "excepted", "non_blocking")
DOCUMENT_FIELDS = (
    "format",
    "format_version",
    "version",
    "generated_at",
    "sources",
    "summary",
    "refused_exceptions",
    "findings",
)
SOURCE_FIELDS = ("name", "scanner", "scanner_version", "database_schema", "database_built_at")
FINDING_FIELDS = (
    "state",
    "source",
    "severity",
    "vulnerability",
    "aliases",
    "package",
    "package_version",
    "package_type",
    "fix_state",
    "fixed_in",
    "exception",
)
EXCEPTION_FIELDS = ("reason", "review_by")

# Une valeur hors de sa forme admise n'est pas recopiée.
HIDDEN = "?"
# Bornes : une liste au-delà est coupée, un relevé au-delà est refusé.
LIST_LIMIT = 16
FINDINGS_LIMIT = 10_000
_FILE_LIMIT = 64 * 1024 * 1024
_PACKAGES_LIMIT = 10_000
_REASON_LIMIT = 1000
_PROBLEM_LIMIT = 300
_PROBLEMS_SHOWN = 20

# Identifiant d'une vulnérabilité : CVE-2026-1234, GHSA-xxxx-xxxx-xxxx, ALSA-2026:1234…
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9._:-]{1,63}")
# Nom d'un paquet : libssl3, pyyaml, @portee/outil, github.com/x/y…
_NAME = re.compile(r"[A-Za-z0-9@][A-Za-z0-9@._+~:/-]{0,213}")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+~:-]{0,127}")
_TYPE = re.compile(r"[a-z][a-z0-9-]{0,31}")
# Version d'un relevé : celle de l'image (1.2.3, 1.2.3-rc.1) ou un commit dans le pipeline.
_RELEASE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
_GENERATED = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?"
    r"(?:Z|[+-][0-9]{2}:[0-9]{2})"
)
_SCANNER_VERSION = re.compile(r"v?[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}(?:[-+][A-Za-z0-9.-]{1,32})?")
_SCHEMA = re.compile(r"v?[0-9]{1,4}(?:\.[0-9]{1,4}){0,2}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# Noms des verrous : PyPI (normalisé par uv), npm (portée comprise).
_PYPI_NAME = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?")
_NPM_NAME = re.compile(r"(?:@[A-Za-z0-9][A-Za-z0-9._~-]{0,100}/)?[A-Za-z0-9][A-Za-z0-9._~-]{0,213}")
# Sources des membres de l'espace de travail dans uv.lock : ce sont les paquets du projet.
_WORKSPACE = frozenset({"editable", "virtual"})
_PURL_TYPES = {"python": "pypi", "npm": "npm"}


class InputError(ValueError):
    """Une entrée absente, illisible ou hors de sa forme : rien n'est écrit."""


def _read(path: Path, what: str) -> str:
    """Le texte d'un fichier, borné ; ni son contenu ni la position d'une erreur."""
    try:
        if path.stat().st_size > _FILE_LIMIT:
            raise InputError(f"{what} de plus de {_FILE_LIMIT} octets")
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise InputError(f"{what} absent ou illisible ({type(error).__name__})") from None


# Inventaire des paquets Python, d'après uv.lock


@dataclass(frozen=True)
class _Locked:
    name: str
    version: str
    local: bool
    dependencies: tuple[Mapping[str, Any], ...]
    extras: Mapping[str, tuple[Mapping[str, Any], ...]]


def _entries(value: object) -> tuple[Mapping[str, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise InputError("verrou illisible : liste de dépendances attendue")
    return tuple(value)


def _locked(entry: object) -> _Locked:
    if not isinstance(entry, Mapping):
        raise InputError("verrou illisible : entrée de paquet incomplète")
    name, version, source = entry.get("name"), entry.get("version"), entry.get("source")
    if not isinstance(name, str) or not isinstance(source, Mapping):
        raise InputError("verrou illisible : entrée de paquet incomplète")
    local = bool(_WORKSPACE & set(source))
    if not local and not isinstance(version, str):
        raise InputError("verrou illisible : entrée de paquet incomplète")
    version = version if isinstance(version, str) else "0"
    if not _PYPI_NAME.fullmatch(name) or not _VERSION.fullmatch(version):
        raise InputError("nom ou version de paquet hors format dans le verrou")
    extras = entry.get("optional-dependencies") or {}
    if not isinstance(extras, Mapping):
        raise InputError("verrou illisible : extras attendus en table")
    return _Locked(
        name,
        version,
        local,
        _entries(entry.get("dependencies")),
        {str(extra): _entries(items) for extra, items in extras.items()},
    )


def python_inventory(text: str) -> list[tuple[str, str]]:
    """Les paquets que les membres de l'espace de travail installent, d'après ``uv.lock``.

    Les dépendances des membres et des extras qu'elles demandent, de proche en
    proche, sans le groupe de développement ni les membres eux-mêmes ; une
    dépendance qui nomme une version la désigne parmi celles du verrou.
    """
    try:
        lock = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise InputError(f"verrou illisible ({type(error).__name__})") from None
    if lock.get("version") != 1:
        raise InputError("version du verrou inconnue : 1 attendue")
    entries = lock.get("package")
    if not isinstance(entries, list) or len(entries) > _PACKAGES_LIMIT:
        raise InputError(f"verrou illisible : de 0 à {_PACKAGES_LIMIT} paquets attendus")
    packages: dict[str, list[_Locked]] = {}
    for entry in entries:
        locked = _locked(entry)
        packages.setdefault(locked.name, []).append(locked)
    queue: list[tuple[_Locked, tuple[str, ...]]] = [
        (locked, ()) for versions in packages.values() for locked in versions if locked.local
    ]
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    found: set[tuple[str, str]] = set()
    while queue:
        locked, extras = queue.pop()
        key = (locked.name, locked.version, extras)
        if key in seen:
            continue
        seen.add(key)
        if not locked.local:
            found.add((locked.name, locked.version))
        wanted = [*locked.dependencies]
        for extra in extras:
            wanted.extend(locked.extras.get(extra, ()))
        for dependency in wanted:
            queue.extend(_resolve(dependency, packages))
    return sorted(found)


def _resolve(
    dependency: Mapping[str, Any], packages: Mapping[str, list[_Locked]]
) -> list[tuple[_Locked, tuple[str, ...]]]:
    name, version = dependency.get("name"), dependency.get("version")
    extras = dependency.get("extra") or []
    if (
        not isinstance(name, str)
        or not _PYPI_NAME.fullmatch(name)
        or not isinstance(extras, list)
        or not all(isinstance(extra, str) for extra in extras)
    ):
        raise InputError("dépendance hors format dans le verrou")
    candidates = [
        locked
        for locked in packages.get(name, [])
        if not isinstance(version, str) or locked.version == version
    ]
    if not candidates:
        raise InputError("le verrou nomme une dépendance qu'il ne décrit pas")
    return [(locked, tuple(sorted(extras))) for locked in candidates]


# Inventaire des paquets npm de l'explorateur, d'après son verrou


def npm_inventory(text: str) -> list[tuple[str, str]]:
    """Les paquets que l'explorateur construit embarque, d'après ``package-lock.json``.

    Tous ceux que le verrou ne marque pas ``dev``, facultatifs compris, sans le
    projet lui-même ni un dossier local ; chaque dépendance directe doit y être.
    """
    try:
        lock = json.loads(text)
    except ValueError as error:
        raise InputError(f"verrou illisible ({type(error).__name__})") from None
    if not isinstance(lock, Mapping) or lock.get("lockfileVersion") not in (2, 3):
        raise InputError("version du verrou inconnue : 2 ou 3 attendue")
    packages = lock.get("packages")
    if not isinstance(packages, Mapping) or len(packages) > _PACKAGES_LIMIT:
        raise InputError(f"verrou illisible : de 0 à {_PACKAGES_LIMIT} paquets attendus")
    root = packages.get("") or {}
    if not isinstance(root, Mapping):
        raise InputError("verrou illisible : entrée de paquet incomplète")
    found: set[tuple[str, str]] = set()
    installed: set[str] = set()
    for path, entry in packages.items():
        if not isinstance(entry, Mapping):
            raise InputError("verrou illisible : entrée de paquet incomplète")
        marker = "node_modules/"
        if marker not in path or entry.get("dev") is True or entry.get("link") is True:
            continue
        here = path.rpartition(marker)[2]
        name, version = entry.get("name", here), entry.get("version")
        if (
            not isinstance(name, str)
            or not _NPM_NAME.fullmatch(name)
            or not _NPM_NAME.fullmatch(here)
            or not isinstance(version, str)
            or not _VERSION.fullmatch(version)
        ):
            raise InputError("nom ou version de paquet hors format dans le verrou")
        found.add((name, version))
        installed.add(here)
    direct = root.get("dependencies") or {}
    if not isinstance(direct, Mapping) or not set(direct) <= installed:
        raise InputError("une dépendance directe de l'explorateur manque au verrou")
    return sorted(found)


def cyclonedx(kind: str, packages: Sequence[tuple[str, str]]) -> dict[str, Any]:
    """Un SBOM CycloneDX des paquets, que Grype analyse comme celui de l'image."""
    purl_type = _PURL_TYPES[kind]
    components = []
    for name, version in packages:
        purl = f"pkg:{purl_type}/{name.replace('@', '%40', 1)}@{version}"
        components.append(
            {"type": "library", "bom-ref": purl, "name": name, "version": version, "purl": purl}
        )
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {"component": {"type": "application", "name": f"answerlineage-{kind}"}},
        "components": components,
    }


# Rapports de Grype


@dataclass(frozen=True)
class Scan:
    """Ce que le relevé reprend d'un rapport de Grype."""

    source: str
    scanner_version: str
    database_schema: str
    database_built_at: str
    found: tuple[Finding, ...]


def _field(mapping: object, key: str) -> object:
    return mapping.get(key) if isinstance(mapping, Mapping) else None


def _formed(value: object, form: re.Pattern[str]) -> bool:
    return isinstance(value, str) and form.fullmatch(value) is not None


def read_scan(source: str, report: object) -> Scan:
    """Les trouvailles d'un rapport JSON de Grype, avec la version de Grype et de sa base."""
    descriptor = _field(report, "descriptor")
    status = _field(_field(descriptor, "db"), "status")
    version = _field(descriptor, "version")
    schema, built = _field(status, "schemaVersion"), _field(status, "built")
    if not isinstance(version, str) or not _SCANNER_VERSION.fullmatch(version):
        raise InputError(f"rapport de Grype ({source}) sans version de Grype lisible")
    if (
        _field(status, "valid") is not True
        or not isinstance(schema, str)
        or not _SCHEMA.fullmatch(schema)
        or not isinstance(built, str)
        or not _TIMESTAMP.fullmatch(built)
    ):
        raise InputError(f"rapport de Grype ({source}) sans base de vulnérabilités valable")
    try:
        found = findings(report)
    except ValueError:
        raise InputError(f"rapport de Grype ({source}) inattendu") from None
    if len(found) > FINDINGS_LIMIT:
        raise InputError(f"rapport de Grype ({source}) de plus de {FINDINGS_LIMIT} vulnérabilités")
    return Scan(source, version, schema, built, tuple(found))


# Le relevé


def _checked(value: str, form: re.Pattern[str]) -> str:
    return value if form.fullmatch(value) else HIDDEN


def _text(value: str, limit: int) -> str:
    """Un texte sur une ligne, sans caractère qui ferait une cellule, une balise ou un lien."""
    text = " ".join(value.split())
    text = "".join(
        "?" if character in "|<>`[]" or not character.isprintable() else character
        for character in text
    )
    return text[:limit]


def _row(
    source: str, state: str, finding: Finding, rule: VulnerabilityException | None
) -> dict[str, Any]:
    vulnerability = _checked(finding.vulnerability, _IDENTIFIER)
    aliases = sorted(
        {_checked(alias, _IDENTIFIER) for alias in finding.aliases} - {vulnerability, HIDDEN}
    )
    return {
        "state": state,
        "source": source,
        "severity": finding.severity if finding.severity in SEVERITIES else "Unknown",
        "vulnerability": vulnerability,
        "aliases": aliases[:LIST_LIMIT],
        "package": _checked(finding.package, _NAME),
        "package_version": _checked(finding.version, _VERSION),
        "package_type": _checked(finding.package_type, _TYPE),
        "fix_state": finding.fix_state if finding.fix_state in FIX_STATES else "unknown",
        "fixed_in": [_checked(version, _VERSION) for version in finding.fix_versions][:LIST_LIMIT],
        "exception": None
        if rule is None
        else {
            "reason": _text(rule.reason, _REASON_LIMIT),
            "review_by": rule.review_by.isoformat(),
        },
    }


def _rank(severity: object) -> int:
    return SEVERITIES.index(severity) if severity in SEVERITIES else len(SEVERITIES)


def _order(row: Mapping[str, Any]) -> tuple[object, ...]:
    """Ordre du relevé : état, gravité, source, paquet, version, vulnérabilité, puis le reste."""
    return (
        STATES.index(row["state"]) if row.get("state") in STATES else len(STATES),
        _rank(row.get("severity")),
        SOURCES.index(row["source"]) if row.get("source") in SOURCES else len(SOURCES),
        str(row.get("package")),
        str(row.get("package_version")),
        str(row.get("vulnerability")),
        json.dumps(row, sort_keys=True, ensure_ascii=False),
    )


def build(
    scans: Sequence[Scan],
    exceptions: Sequence[VulnerabilityException],
    refused: Sequence[str],
    version: str,
    generated_at: datetime,
) -> dict[str, Any]:
    """Le relevé, au format que ``problems`` contrôle."""
    if not _RELEASE.fullmatch(version):
        raise InputError("version du relevé hors format : 1.2.3, 1.2.3-rc.1 ou un commit")
    rows: list[dict[str, Any]] = []
    for scan in scans:
        verdict = evaluate(scan.found, exceptions)
        rows += [_row(scan.source, "blocking", finding, None) for finding in verdict.blocking]
        rows += [_row(scan.source, "excepted", finding, rule) for finding, rule in verdict.excepted]
        rows += [_row(scan.source, "non_blocking", finding, None) for finding in verdict.minor]
    if len(rows) > FINDINGS_LIMIT:
        raise InputError(f"relevé de plus de {FINDINGS_LIMIT} vulnérabilités")
    rows.sort(key=_order)
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "version": version,
        "generated_at": generated_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": [
            {
                "name": scan.source,
                "scanner": "grype",
                "scanner_version": scan.scanner_version,
                "database_schema": scan.database_schema,
                "database_built_at": scan.database_built_at,
            }
            for scan in sorted(scans, key=lambda scan: SOURCES.index(scan.source))
        ],
        "summary": {state: sum(row["state"] == state for row in rows) for state in STATES},
        "refused_exceptions": [_text(problem, _PROBLEM_LIMIT) for problem in refused][
            :_PROBLEMS_SHOWN
        ],
        "findings": rows,
    }


# Le format


def _fields(value: object, expected: Sequence[str], where: str) -> list[str]:
    if not isinstance(value, Mapping):
        return [f"{where} : objet attendu"]
    missing = [name for name in expected if name not in value]
    unknown = len(set(value) - set(expected))
    return [f"{where} : champ {name} absent" for name in missing] + (
        [f"{where} : {unknown} champ(s) inconnu(s)"] if unknown else []
    )


def _strings(value: object, form: re.Pattern[str], *, hidden: bool = True) -> bool:
    """Une liste bornée de valeurs à leur forme, ou « ? » si ``hidden``."""
    return (
        isinstance(value, list)
        and len(value) <= LIST_LIMIT
        and all(_formed(item, form) or (hidden and item == HIDDEN) for item in value)
    )


def _source_problems(sources: object) -> list[str]:
    if not isinstance(sources, list) or not sources:
        return ["sources : liste non vide attendue"]
    found: list[str] = []
    names: list[object] = []
    for number, source in enumerate(sources, start=1):
        where = f"source n°{number}"
        missing = _fields(source, SOURCE_FIELDS, where)
        if missing or not isinstance(source, Mapping):
            found += missing
            continue
        names.append(source["name"])
        checks = (
            (source["name"] in SOURCES, "nom inconnu"),
            (source["scanner"] == "grype", "outil d'analyse inconnu"),
            (_formed(source["scanner_version"], _SCANNER_VERSION), "version de Grype hors format"),
            (_formed(source["database_schema"], _SCHEMA), "schéma de la base hors format"),
            (_formed(source["database_built_at"], _TIMESTAMP), "date de la base hors format"),
        )
        found += [f"{where} : {problem}" for passed, problem in checks if not passed]
    if len(names) != len(set(map(str, names))):
        found.append("sources : une source est nommée deux fois")
    return found


def _exception_problems(row: Mapping[str, Any], where: str) -> list[str]:
    exception = row["exception"]
    if row["state"] != "excepted":
        return [] if exception is None else [f"{where} : exception hors de l'état excepted"]
    missing = _fields(exception, EXCEPTION_FIELDS, f"{where}, exception")
    if missing or not isinstance(exception, Mapping):
        return missing
    reason = exception["reason"]
    found = []
    if not isinstance(reason, str) or not reason.strip() or len(reason) > _REASON_LIMIT:
        found.append(f"{where} : motif d'exception absent ou démesuré")
    if not _formed(exception["review_by"], _DATE):
        found.append(f"{where} : date de revue hors format")
    return found


def _finding_problems(row: object, number: int, sources: set[object]) -> list[str]:
    where = f"vulnérabilité n°{number}"
    missing = _fields(row, FINDING_FIELDS, where)
    if missing or not isinstance(row, Mapping):
        return missing

    def single(form: re.Pattern[str]) -> Callable[[object], bool]:
        return lambda value: value == HIDDEN or _formed(value, form)

    checks: tuple[tuple[str, Callable[[object], bool]], ...] = (
        ("state", lambda value: value in STATES),
        ("source", lambda value: value in sources),
        ("severity", lambda value: value in SEVERITIES),
        ("vulnerability", single(_IDENTIFIER)),
        ("aliases", lambda value: _strings(value, _IDENTIFIER, hidden=False)),
        ("package", single(_NAME)),
        ("package_version", single(_VERSION)),
        ("package_type", single(_TYPE)),
        ("fix_state", lambda value: value in FIX_STATES),
        ("fixed_in", lambda value: _strings(value, _VERSION)),
    )
    found = [f"{where} : {name} hors format" for name, valid in checks if not valid(row[name])]
    return found or _exception_problems(row, where)


def problems(document: object) -> list[str]:
    """Ce qui écarte un relevé de son format ; aucune valeur reçue n'y est recopiée."""
    if not isinstance(document, Mapping):
        return ["le relevé n'est pas un objet JSON"]
    found = _fields(document, DOCUMENT_FIELDS, "racine")
    if found:
        return found
    if document["format"] != FORMAT or type(document["format_version"]) is not int:
        return [f"format inconnu : {FORMAT}, version {FORMAT_VERSION} attendu"]
    if document["format_version"] != FORMAT_VERSION:
        return [f"version de format inconnue : {FORMAT_VERSION} attendue"]
    if not _formed(document["version"], _RELEASE):
        found.append("version hors format")
    if not _formed(document["generated_at"], _GENERATED):
        found.append("date du relevé hors format")
    found += _source_problems(document["sources"])
    sources = document["sources"] if isinstance(document["sources"], list) else []
    declared = {source.get("name") for source in sources if isinstance(source, Mapping)}
    refused = document["refused_exceptions"]
    if (
        not isinstance(refused, list)
        or len(refused) > _PROBLEMS_SHOWN
        or not all(isinstance(item, str) and len(item) <= _PROBLEM_LIMIT for item in refused)
    ):
        found.append("refused_exceptions : liste bornée de textes attendue")
    rows = document["findings"]
    if not isinstance(rows, list) or len(rows) > FINDINGS_LIMIT:
        return [*found, f"findings : liste de {FINDINGS_LIMIT} vulnérabilités au plus attendue"]
    for number, row in enumerate(rows, start=1):
        found += _finding_problems(row, number, declared)
    if found:
        return found[:_PROBLEMS_SHOWN]
    summary = document["summary"]
    counted = {state: sum(row["state"] == state for row in rows) for state in STATES}
    if (
        not isinstance(summary, Mapping)
        or set(summary) != set(STATES)
        or any(
            type(summary[state]) is not int or summary[state] != counted[state] for state in STATES
        )
    ):
        found.append("summary : ne compte pas les vulnérabilités du relevé")
    if [_order(row) for row in rows] != sorted(_order(row) for row in rows):
        found.append("findings : hors de l'ordre du relevé")
    return found


# Le relevé lisible

_SOURCE_LABELS = {
    "image": (
        "Image",
        "Paquets du système et paquets Python installés dans l'image, d'après son SBOM",
    ),
    "python": ("Paquets Python", "Dépendances des paquets publiés sur PyPI, d'après uv.lock"),
    "npm": (
        "Explorateur",
        "Paquets npm embarqués dans l'explorateur construit, d'après ui/package-lock.json",
    ),
}
_STATE_LABELS = {
    "blocking": (
        "bloquante",
        "Haute ou critique, sans exception valable : une version qui en a une n'est pas publiée.",
    ),
    "excepted": (
        "écartée par une exception",
        "Haute ou critique, écartée après examen par une exception motivée et datée.",
    ),
    "non_blocking": ("non bloquante", "Gravité moyenne, faible, négligeable ou inconnue."),
}
_SEVERITY_LABELS = {
    "Critical": "critique",
    "High": "haute",
    "Medium": "moyenne",
    "Low": "faible",
    "Negligible": "négligeable",
    "Unknown": "inconnue",
}
_FIX_LABELS = {
    "not-fixed": "aucune pour l'instant",
    "wont-fix": "aucune, ne sera pas corrigée",
}


def fixed_in(row: Mapping[str, Any]) -> str:
    """La version qui corrige une vulnérabilité du relevé, ou pourquoi il n'y en a pas."""
    if row["fixed_in"]:
        return ", ".join(row["fixed_in"])
    return _FIX_LABELS.get(row["fix_state"], "inconnue")


def _named(row: Mapping[str, Any]) -> str:
    aliases = row["aliases"]
    return f"{row['vulnerability']} ({', '.join(aliases)})" if aliases else row["vulnerability"]


def render_markdown(document: Mapping[str, Any]) -> str:
    """Le relevé en Markdown : les mêmes données que le JSON, à lire."""
    lines = [
        f"# Vulnérabilités connues d'AnswerLineage {document['version']}",
        "",
        f"Relevé du {document['generated_at']}. Il liste chaque vulnérabilité connue à cette date "
        "des dépendances de cette version, quelle que soit sa gravité, d'après la base de "
        "vulnérabilités de Grype indiquée pour chaque source. Une vulnérabilité publiée depuis "
        "n'y figure pas : l'analyse se rejoue sur le SBOM de l'image et les verrous de la version.",
        "",
        "| Source | Ce qui est analysé | Grype | Base de vulnérabilités |",
        "| --- | --- | --- | --- |",
    ]
    for source in document["sources"]:
        label, scope = _SOURCE_LABELS[source["name"]]
        lines.append(
            f"| {label} | {scope} | {source['scanner_version']} | schéma "
            f"{source['database_schema']}, construite le {source['database_built_at']} |"
        )
    lines += ["", "## Bilan", "", "| État | Nombre | Ce que c'est |", "| --- | --- | --- |"]
    for state in STATES:
        label, meaning = _STATE_LABELS[state]
        lines.append(f"| {label} | {document['summary'][state]} | {meaning} |")
    lines += ["", "## Vulnérabilités", ""]
    if document["findings"]:
        lines += [
            "| État | Gravité | Source | Paquet | Version | Type | Vulnérabilité | Corrigée dans "
            "| Exception |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for row in document["findings"]:
            exception = row["exception"]
            note = f"revue le {exception['review_by']} : {exception['reason']}" if exception else ""
            lines.append(
                f"| {_STATE_LABELS[row['state']][0]} | {_SEVERITY_LABELS[row['severity']]} "
                f"| {_SOURCE_LABELS[row['source']][0]} | {row['package']} "
                f"| {row['package_version']} | {row['package_type']} | {_named(row)} "
                f"| {fixed_in(row)} | {note} |"
            )
    else:
        lines.append(
            "Aucune vulnérabilité connue dans les dépendances de cette version, d'après la base "
            "de vulnérabilités indiquée pour chaque source."
        )
    if document["refused_exceptions"]:
        lines += ["", "## Exceptions refusées", ""]
        lines += [
            f"- {problem} : elle ne s'applique pas." for problem in document["refused_exceptions"]
        ]
    return "\n".join(lines) + "\n"


# Ligne de commande


def _sources(values: Sequence[str]) -> dict[str, Path] | None:
    """Les rapports nommés ``source=fichier`` : chaque source connue, une fois au plus."""
    found: dict[str, Path] = {}
    for value in values:
        name, separator, path = value.partition("=")
        if not separator or name not in SOURCES or name in found or not path:
            return None
        found[name] = Path(path)
    return found or None


def _inventory(arguments: argparse.Namespace) -> int:
    default = ROOT / ("uv.lock" if arguments.kind == "python" else "ui/package-lock.json")
    lock: Path = arguments.lock or default
    try:
        text = _read(lock, "verrou")
        packages = (python_inventory if arguments.kind == "python" else npm_inventory)(text)
    except InputError as error:
        print(f"Inventaire impossible : {error}.")
        return 2
    output: Path = arguments.output
    output.write_text(json.dumps(cyclonedx(arguments.kind, packages), indent=2) + "\n", "utf-8")
    print(f"Inventaire {arguments.kind} : {len(packages)} paquet(s), dans {output}.")
    return 0


def _build(arguments: argparse.Namespace, now: datetime) -> int:
    sources = _sources(arguments.source)
    if sources is None:
        print(f"Sources refusées : une ou plusieurs de {', '.join(SOURCES)}, chacune une fois.")
        return 2
    try:
        scans = []
        for name, path in sources.items():
            try:
                report = json.loads(_read(path, f"rapport de Grype ({name})"))
            except ValueError as error:
                if isinstance(error, InputError):
                    raise
                raise InputError(f"rapport de Grype ({name}) illisible") from None
            scans.append(read_scan(name, report))
        exceptions, refused = load_exceptions(
            _read(arguments.exceptions, "fichier d'exceptions"), now.date()
        )
        document = build(scans, exceptions, refused, arguments.version, now)
    except InputError as error:
        print(f"Relevé impossible : {error}.")
        return 2
    found = problems(document)
    if found:
        # Un relevé qui sortirait de son format casse ici, dès la merge request.
        print(f"Relevé hors de son propre format : {found[0]}.")
        return 2
    arguments.json.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    arguments.markdown.write_text(render_markdown(document), encoding="utf-8")
    summary = document["summary"]
    print(
        f"Relevé des vulnérabilités connues de {document['version']} : "
        f"{summary['blocking']} bloquante(s), {summary['excepted']} écartée(s) par une exception, "
        f"{summary['non_blocking']} non bloquante(s) ; dans {arguments.json} "
        f"et {arguments.markdown}."
    )
    return 0


def main(argv: Sequence[str] | None = None, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.known_vulnerabilities")
    commands = parser.add_subparsers(dest="command", required=True)
    inventory = commands.add_parser("inventory", help="SBOM CycloneDX d'un verrou")
    inventory.add_argument("kind", choices=("python", "npm"))
    inventory.add_argument("--lock", type=Path)
    inventory.add_argument("--output", type=Path, required=True)
    statement = commands.add_parser("build", help="relevé des vulnérabilités connues")
    statement.add_argument("--version", required=True)
    statement.add_argument("--source", action="append", default=[], metavar="SOURCE=RAPPORT")
    statement.add_argument("--exceptions", type=Path, default=Path(EXCEPTIONS_FILE))
    statement.add_argument("--json", type=Path, required=True)
    statement.add_argument("--markdown", type=Path, required=True)
    arguments = parser.parse_args(argv)
    if arguments.command == "inventory":
        return _inventory(arguments)
    return _build(arguments, datetime.now(UTC) if now is None else now)


def read_document(path: Path) -> object:
    """Le relevé JSON d'un fichier ; ``InputError`` s'il est absent, démesuré ou illisible."""
    try:
        return json.loads(_read(path, "relevé"))
    except ValueError as error:
        if isinstance(error, InputError):
            raise
        raise InputError("relevé illisible") from None


if __name__ == "__main__":
    sys.exit(main())
