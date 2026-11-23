"""Verdict de l'analyse de vulnérabilités d'une image.

Grype analyse le SBOM de l'image et écrit son rapport en JSON ; ce contrôle en
tire le verdict. Une vulnérabilité de gravité haute ou critique fait échouer le
pipeline, sauf si elle a une exception valable dans
``vulnerability-exceptions.toml`` : une entrée par vulnérabilité, avec son
motif et sa date de revue. Une exception sans motif, ou dont la date de revue
est passée, est refusée et ne s'applique plus.

Les trouvailles s'affichent dans le journal (paquet, version, identifiant,
gravité, version corrigée) et, dans le pipeline, dans le résumé du job.

Usage :
  ``python3 -m scripts.check_vulnerabilities exceptions [--file FICHIER]``
  ``python3 -m scripts.check_vulnerabilities report RAPPORT.json [--exceptions FICHIER]``

Codes de sortie : 0 vert, 1 refusé (vulnérabilité bloquante ou exception
refusée), 2 fichier absent ou illisible.

Bibliothèque standard seule, Python 3.12 au moins : le pipeline le lance avec
l'interpréteur du runner.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

EXCEPTIONS_FILE = "vulnerability-exceptions.toml"
BLOCKING = ("Critical", "High")
# Ordre d'affichage des gravités, de la plus grave à la plus faible.
SEVERITIES = ("Critical", "High", "Medium", "Low", "Negligible", "Unknown")

_ENTRY_KEYS = frozenset({"vulnerability", "package", "reason", "review-by"})
# Un identifiant nommé (CVE-2024-1234, GHSA-xxxx-xxxx-xxxx…) : jamais un motif.
_VULNERABILITY = re.compile(r"[A-Z][A-Z0-9]{1,15}(-[A-Za-z0-9]{1,20}){1,5}")
_PACKAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+:~-]{0,127}")
_REASON_LIMIT = 1000
_CELL_LIMIT = 120
_FIX_STATES = {
    "not-fixed": "aucune pour l'instant",
    "wont-fix": "aucune, ne sera pas corrigée",
}
# États du correctif que Grype écrit ; tout autre se lit « unknown ».
FIX_STATES = ("fixed", "not-fixed", "wont-fix", "unknown")


@dataclass(frozen=True)
class VulnerabilityException:
    vulnerability: str
    package: str | None
    reason: str
    review_by: date

    def covers(self, finding: Finding) -> bool:
        named = self.vulnerability in (finding.vulnerability, *finding.aliases)
        return named and self.package in (None, finding.package)


@dataclass(frozen=True)
class Finding:
    vulnerability: str
    aliases: tuple[str, ...]
    severity: str
    package: str
    version: str
    package_type: str
    fixed_in: str
    # Le correctif tel que Grype le décrit : son état et les versions qui corrigent.
    fix_state: str = "unknown"
    fix_versions: tuple[str, ...] = ()


@dataclass
class Verdict:
    blocking: list[Finding] = field(default_factory=list)
    excepted: list[tuple[Finding, VulnerabilityException]] = field(default_factory=list)
    others: Counter[str] = field(default_factory=Counter)
    # Les trouvailles de gravité moyenne ou moins, que others compte.
    minor: list[Finding] = field(default_factory=list)
    unused: list[VulnerabilityException] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.blocking


def _cell(value: object) -> str:
    """Une valeur venue d'un fichier, affichable sans risque dans le journal et le résumé.

    Aucun saut de ligne (une commande du pipeline commence une ligne), aucune
    barre (une cellule de tableau), aucun balisage.
    """
    text = " ".join(str(value).split())
    text = "".join(
        "?" if character in "|<>`" or not character.isprintable() else character
        for character in text
    )
    return text if len(text) <= _CELL_LIMIT else text[: _CELL_LIMIT - 1] + "…"


# Exceptions déclarées


def load_exceptions(text: str, today: date) -> tuple[list[VulnerabilityException], list[str]]:
    """Les exceptions valables, et un message pour chaque exception refusée."""
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        return [], [f"fichier d'exceptions illisible ({type(error).__name__})"]
    unknown = sorted(set(document) - {"exception"})
    if unknown:
        # Le nom d'une clé vient du fichier : il n'est affiché que s'il est anodin.
        return [], [f"clé inconnue au premier niveau : {_cell(', '.join(unknown))}"]
    entries = document.get("exception", [])
    if not isinstance(entries, list):
        return [], ["« exception » doit être une liste d'entrées [[exception]]"]
    exceptions: list[VulnerabilityException] = []
    problems: list[str] = []
    seen: set[tuple[str, str | None]] = set()
    for number, entry in enumerate(entries, start=1):
        accepted, problem = _entry(number, entry, today)
        if accepted is not None and (accepted.vulnerability, accepted.package) in seen:
            problem = f"exception n°{number} ({accepted.vulnerability}) : déclarée deux fois"
            accepted = None
        if accepted is None:
            problems.append(str(problem))
            continue
        seen.add((accepted.vulnerability, accepted.package))
        exceptions.append(accepted)
    return exceptions, problems


def _entry(
    number: int, entry: object, today: date
) -> tuple[VulnerabilityException | None, str | None]:
    where = f"exception n°{number}"
    if not isinstance(entry, Mapping):
        return None, f"{where} : l'entrée n'est pas une table"
    vulnerability = entry.get("vulnerability")
    if not isinstance(vulnerability, str) or not _VULNERABILITY.fullmatch(vulnerability):
        return None, f"{where} : identifiant de vulnérabilité absent ou invalide"
    where = f"{where} ({vulnerability})"
    unknown = sorted(set(entry) - _ENTRY_KEYS)
    if unknown:
        return None, f"{where} : clé inconnue : {_cell(', '.join(unknown))}"
    package = entry.get("package")
    if package is not None and (not isinstance(package, str) or not _PACKAGE.fullmatch(package)):
        return None, f"{where} : nom de paquet invalide"
    reason = entry.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > _REASON_LIMIT:
        return None, f"{where} : motif absent (reason, {_REASON_LIMIT} caractères au plus)"
    review_by = entry.get("review-by")
    if review_by is None:
        return None, f"{where} : date de revue absente (review-by)"
    # TOML distingue la date de l'horodatage, qui en est une sous-classe en Python.
    if type(review_by) is not date:
        return None, f"{where} : review-by doit être une date, AAAA-MM-JJ, sans guillemets"
    if review_by < today:
        return None, f"{where} : date de revue passée ({review_by.isoformat()})"
    return VulnerabilityException(vulnerability, package, reason.strip(), review_by), None


# Rapport de Grype


def findings(report: object) -> list[Finding]:
    """Les vulnérabilités du rapport JSON de Grype, une fois chacune par paquet."""
    unexpected = ValueError("rapport de Grype inattendu")
    if not isinstance(report, Mapping) or not isinstance(report.get("matches"), list):
        raise unexpected
    found: dict[tuple[str, str, str, str], Finding] = {}
    for item in report["matches"]:
        if not isinstance(item, Mapping):
            raise unexpected
        vulnerability = item.get("vulnerability")
        artifact = item.get("artifact")
        if not isinstance(vulnerability, Mapping) or not isinstance(artifact, Mapping):
            raise unexpected
        related = item.get("relatedVulnerabilities") or []
        aliases = tuple(
            dict.fromkeys(
                _cell(alias["id"])
                for alias in related
                if isinstance(alias, Mapping) and alias.get("id")
            )
        )
        finding = Finding(
            vulnerability=_cell(vulnerability.get("id", "")),
            aliases=aliases,
            severity=_cell(vulnerability.get("severity") or "Unknown"),
            package=_cell(artifact.get("name", "")),
            version=_cell(artifact.get("version", "")),
            package_type=_cell(artifact.get("type", "")),
            fixed_in=_fixed_in(vulnerability.get("fix")),
            fix_state=_fix_state(vulnerability.get("fix")),
            fix_versions=_fix_versions(vulnerability.get("fix")),
        )
        key = (finding.vulnerability, finding.package, finding.version, finding.package_type)
        found.setdefault(key, finding)
    return list(found.values())


def _fixed_in(fix: object) -> str:
    if not isinstance(fix, Mapping):
        return "inconnue"
    versions = fix.get("versions")
    if isinstance(versions, list) and versions:
        return _cell(", ".join(str(version) for version in versions))
    return _FIX_STATES.get(str(fix.get("state")), "inconnue")


def _fix_state(fix: object) -> str:
    state = fix.get("state") if isinstance(fix, Mapping) else None
    return state if isinstance(state, str) and state in FIX_STATES else "unknown"


def _fix_versions(fix: object) -> tuple[str, ...]:
    versions = fix.get("versions") if isinstance(fix, Mapping) else None
    if not isinstance(versions, list):
        return ()
    return tuple(dict.fromkeys(_cell(version) for version in versions))


def evaluate(found: Sequence[Finding], exceptions: Sequence[VulnerabilityException]) -> Verdict:
    verdict = Verdict()
    used: set[int] = set()
    for finding in sorted(found, key=_order):
        if finding.severity not in BLOCKING:
            verdict.others[finding.severity] += 1
            verdict.minor.append(finding)
            continue
        rule = next((rule for rule in exceptions if rule.covers(finding)), None)
        if rule is None:
            verdict.blocking.append(finding)
        else:
            verdict.excepted.append((finding, rule))
            used.add(id(rule))
    verdict.unused = [rule for rule in exceptions if id(rule) not in used]
    return verdict


def _order(finding: Finding) -> tuple[int, str, str]:
    return _rank(finding.severity), finding.package, finding.vulnerability


def _rank(severity: str) -> int:
    return SEVERITIES.index(severity) if severity in SEVERITIES else len(SEVERITIES)


# Affichage


def _name(finding: Finding) -> str:
    aliases = [alias for alias in finding.aliases if alias != finding.vulnerability]
    return f"{finding.vulnerability} ({', '.join(aliases)})" if aliases else finding.vulnerability


def _others(verdict: Verdict) -> str:
    counted = sorted(verdict.others.items(), key=lambda item: _rank(item[0]))
    return ", ".join(f"{severity} : {count}" for severity, count in counted) or "aucune"


def _rows(verdict: Verdict) -> list[tuple[str, Finding, str]]:
    """Statut, trouvaille et exception éventuelle de chaque vulnérabilité haute ou critique."""
    return [("bloquante", finding, "") for finding in verdict.blocking] + [
        ("exception", finding, f"revue le {rule.review_by.isoformat()} : {_cell(rule.reason)}")
        for finding, rule in verdict.excepted
    ]


def render_log(verdict: Verdict, problems: Sequence[str]) -> list[str]:
    lines = [
        f"Vulnérabilités hautes et critiques : {len(verdict.blocking)} bloquante(s), "
        f"{len(verdict.excepted)} sous exception."
    ]
    for status, finding, note in _rows(verdict):
        lines.append(
            f"  {status:<9}  {finding.severity:<8}  {finding.package} {finding.version} "
            f"({finding.package_type})  {_name(finding)}  corrigée dans : {finding.fixed_in}"
            + (f" ; {note}" if note else "")
        )
    lines.append(f"Autres trouvailles, non bloquantes : {_others(verdict)}.")
    for rule in verdict.unused:
        lines.append(f"Exception sans objet, à retirer : {rule.vulnerability}.")
    for problem in problems:
        lines.append(f"Exception refusée : {problem}.")
    if verdict.blocking:
        lines.append(
            f"Échec : {len(verdict.blocking)} vulnérabilité(s) haute(s) ou critique(s) sans "
            "exception valable. Mettre à jour le paquet ou l'image de base, ou déclarer une "
            f"exception motivée dans {EXCEPTIONS_FILE} (revue d'un mainteneur)."
        )
    elif problems:
        lines.append(f"Échec : exception(s) refusée(s) dans {EXCEPTIONS_FILE}.")
    else:
        lines.append("Aucune vulnérabilité haute ou critique sans exception valable.")
    return lines


def render_summary(verdict: Verdict, problems: Sequence[str]) -> str:
    passed = verdict.passed and not problems
    lines = [
        "## Analyse de vulnérabilités de l'image",
        "",
        "Verdict : **vert**." if passed else "Verdict : **échec**.",
        "",
    ]
    if verdict.blocking or verdict.excepted:
        lines += [
            "| Statut | Gravité | Paquet | Version | Type | Vulnérabilité | Corrigée dans "
            "| Exception |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for status, finding, note in _rows(verdict):
            lines.append(
                f"| {status} | {finding.severity} | {finding.package} | {finding.version} | "
                f"{finding.package_type} | {_name(finding)} | {finding.fixed_in} | {note} |"
            )
        lines.append("")
    lines.append(f"Autres trouvailles, non bloquantes : {_others(verdict)}.")
    lines += [
        f"- Exception sans objet, à retirer : {rule.vulnerability}" for rule in verdict.unused
    ]
    lines += [f"- Exception refusée : {problem}" for problem in problems]
    return "\n".join(lines) + "\n"


# Ligne de commande


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        print(f"{_cell(path)} illisible ({type(error).__name__}).")
        return None


def main(
    argv: Sequence[str] | None = None,
    today: date | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.check_vulnerabilities")
    commands = parser.add_subparsers(dest="command", required=True)
    only = commands.add_parser("exceptions", help="vérifie les exceptions déclarées")
    only.add_argument("--file", type=Path, default=Path(EXCEPTIONS_FILE))
    verdict_of = commands.add_parser("report", help="verdict d'un rapport JSON de Grype")
    verdict_of.add_argument("report", type=Path)
    verdict_of.add_argument("--exceptions", type=Path, default=Path(EXCEPTIONS_FILE))
    arguments = parser.parse_args(argv)
    today = datetime.now(UTC).date() if today is None else today
    env = os.environ if environment is None else environment

    exceptions_path: Path = (
        arguments.file if arguments.command == "exceptions" else arguments.exceptions
    )
    text = _read(exceptions_path)
    if text is None:
        return 2
    exceptions, problems = load_exceptions(text, today)

    if arguments.command == "exceptions":
        for problem in problems:
            print(f"Exception refusée : {problem}.")
        print(f"{len(exceptions)} exception(s) valable(s), {len(problems)} refusée(s).")
        return 1 if problems else 0

    raw = _read(arguments.report)
    if raw is None:
        return 2
    try:
        found = findings(json.loads(raw))
    except ValueError as error:
        # Ni le contenu du rapport ni la position de l'erreur : seulement sa nature.
        print(f"Rapport de Grype illisible ({type(error).__name__}).")
        return 2
    verdict = evaluate(found, exceptions)
    print("\n".join(render_log(verdict, problems)))
    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as stream:
            stream.write(render_summary(verdict, problems))
    return 0 if verdict.passed and not problems else 1


if __name__ == "__main__":
    sys.exit(main())
