"""Notes de version, tirées des commits conventionnels.

Les notes d'une version reprennent les commits entre l'étiquette précédente et
la sienne, groupés par type : nouveautés, corrections, performances… Les
ruptures, marquées d'un « ! » après le type ou d'un pied ``BREAKING CHANGE:``,
sont signalées en tête. Les notes ne citent que le message des commits, sa
ligne de résumé et le texte de sa rupture : ni auteur, ni empreinte, ni autre
pied de message, ni commit de fusion, et donc jamais un nom de branche.

Aucune référence interne n'y entre : une clé de ticket entre parenthèses
(``(ABC-123)``) qu'un ancien message porterait encore est retirée, et un
message qui renvoie aux documents de cadrage, privés, est écarté. Les
identifiants publics de vulnérabilités (CVE, CWE) restent.

L'étiquette précédente est la plus haute version atteignable qui précède
celle-ci ; pour une version finale, les préversions sont sautées : les notes de
v1.3.0 partent de v1.2.0, pas de v1.3.0-rc.2. Les entrées sont triées : deux
exécutions sur le même historique donnent les mêmes notes, octet pour octet.

Usage :
  ``python3 -m scripts.release_notes v1.2.3``   les notes de l'étiquette v1.2.3
  ``python3 -m scripts.release_notes``          celles qu'aurait l'étiquette de la
                                                version déclarée, posée sur HEAD

Bibliothèque standard seule, Python 3.12 au moins : le pipeline le lance avec
l'interpréteur du runner.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from scripts.check_release import Version, declared_version, parse_tag

# Sections des notes, dans l'ordre : un type conventionnel et son titre.
SECTIONS = (
    ("feat", "Nouveautés"),
    ("fix", "Corrections"),
    ("perf", "Performances"),
    ("revert", "Retours en arrière"),
    ("refactor", "Remaniements"),
    ("docs", "Documentation"),
    ("test", "Tests"),
    ("build", "Construction"),
    ("ci", "Intégration continue"),
    ("style", "Mise en forme"),
    ("chore", "Entretien"),
)
BREAKING = "Ruptures"
OTHERS = "Autres changements"

_HEADER = re.compile(
    r"(?P<kind>[A-Za-z]+)(?:\((?P<scope>[^()]*)\))?(?P<breaking>!)?:\s+(?P<description>.+)"
)
_FOOTER = re.compile(r"(?P<token>BREAKING[ -]CHANGE|[A-Za-z][A-Za-z-]*)(?::\s|\s#)(?P<value>.*)")
_BREAKING_FOOTER = re.compile(r"BREAKING[ -]CHANGE")
# Clé de ticket entre parenthèses : (ABC-123), (ABC-12, DEF-3)… sauf CVE et CWE, publics.
_KEY = r"(?!CVE-|CWE-)[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-[0-9]+"
_KEYS = rf"{_KEY}(?:\s*[,;]\s*{_KEY})*"
_TICKETS = re.compile(rf"\s*\(\s*{_KEYS}\s*\)")
_TICKETS_ONLY = re.compile(_KEYS)
# Renvoi aux documents de cadrage, qui ne sont pas publics.
_PRIVATE = re.compile(r"\b(?:PRD|backlog|cahier des charges)\b", re.IGNORECASE)
# Caractères de contrôle, et caractères invisibles qui cachent ou retournent un texte.
_CONTROL = re.compile(
    "[\x00-\x1f\x7f-\x9f"
    "\N{ZERO WIDTH SPACE}-\N{RIGHT-TO-LEFT MARK}"
    "\N{LINE SEPARATOR}-\N{RIGHT-TO-LEFT OVERRIDE}"
    "\N{WORD JOINER}-\N{INVISIBLE PLUS}"
    "\N{ZERO WIDTH NO-BREAK SPACE}]"
)
_ENTRY_LIMIT = 300
_REVISION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")


@dataclass(frozen=True)
class Change:
    """Ce qu'un commit apporte aux notes."""

    kind: str
    scope: str
    description: str
    breaking: str = ""


def clean(text: str) -> str:
    """Le texte d'une entrée : sans caractère invisible ni clé de ticket, sur une ligne, borné."""
    text = _TICKETS.sub("", _CONTROL.sub(" ", text))
    text = " ".join(text.split())
    if len(text) > _ENTRY_LIMIT:
        text = text[: _ENTRY_LIMIT - 1].rstrip() + "…"
    return text


def _breaking_footer(lines: Sequence[str]) -> str:
    """Le texte des pieds BREAKING CHANGE, jusqu'au pied suivant."""
    texts: list[str] = []
    collecting = False
    for line in lines:
        footer = _FOOTER.fullmatch(line)
        if footer is not None:
            collecting = _BREAKING_FOOTER.fullmatch(footer.group("token")) is not None
            if collecting:
                texts.append(footer.group("value"))
        elif collecting:
            texts.append(line)
    return clean(" ".join(texts))


def parse(message: str) -> Change | None:
    """Ce qu'un message de commit apporte aux notes ; ``None`` s'il n'y entre pas."""
    lines = message.strip().splitlines()
    if not lines:
        return None
    subject = lines[0]
    header = _HEADER.fullmatch(subject.strip())
    breaking = _breaking_footer(lines[1:])
    if header is None:
        kind, scope, description = "", "", clean(subject)
    else:
        kind = header.group("kind").lower()
        scope = clean(header.group("scope") or "")
        # Une portée qui n'est qu'une clé de ticket n'en est pas une.
        scope = "" if _TICKETS_ONLY.fullmatch(scope) else scope
        description = clean(header.group("description"))
        if header.group("breaking") and not breaking:
            breaking = description
    if not description or _PRIVATE.search(f"{subject}\n{breaking}"):
        return None
    return Change(kind, scope, description, breaking)


def _entries(pairs: Iterable[tuple[str, str]]) -> list[str]:
    """Entrées d'une section : avec portée d'abord, triées, sans doublon."""
    ordered = sorted(set(pairs), key=lambda pair: (pair[0] == "", pair[0], pair[1]))
    return [f"- **{scope}** : {text}" if scope else f"- {text}" for scope, text in ordered]


def render(changes: Sequence[Change], previous: Version | None) -> str:
    """Les notes, en Markdown."""
    lines = ["Première version." if previous is None else f"Changements depuis {previous.tag}."]
    sections = [(BREAKING, _entries((c.scope, c.breaking) for c in changes if c.breaking))]
    known = {kind for kind, _ in SECTIONS}
    for kind, title in SECTIONS:
        sections.append(
            (title, _entries((c.scope, c.description) for c in changes if c.kind == kind))
        )
    sections.append(
        (OTHERS, _entries((c.scope, c.description) for c in changes if c.kind not in known))
    )
    for title, entries in sections:
        if entries:
            lines.extend(["", f"## {title}", "", *entries])
    if not changes:
        lines.extend(["", "Aucun changement."])
    return "\n".join(lines) + "\n"


def previous_version(current: Version, tags: Iterable[str]) -> Version | None:
    """La plus haute version étiquetée qui précède ``current`` ; finale si ``current`` l'est."""
    candidates = [
        version
        for version in map(parse_tag, tags)
        if version is not None and version < current and (version.final or not current.final)
    ]
    return max(candidates, default=None)


def _git(*arguments: str) -> str:
    result = subprocess.run(  # noqa: S603 — arguments fixes, révisions contrôlées.
        ["git", *arguments],  # noqa: S607
        check=True,
        capture_output=True,
    )
    return result.stdout.decode("utf-8", errors="replace")


def notes(version: Version, revision: str) -> str:
    """Les notes de ``version``, posée sur ``revision``, d'après l'historique Git."""
    tags = _git("tag", "--merged", revision, "--list", "v*").split()
    previous = previous_version(version, tags)
    span = revision if previous is None else f"{previous.tag}..{revision}"
    messages = _git("log", "--no-merges", "-z", "--format=%B", span, "--").split("\0")
    changes = [change for change in map(parse, messages) if change is not None]
    return render(changes, previous)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.release_notes")
    parser.add_argument(
        "tag", nargs="?", help="étiquette de la version ; par défaut, la version déclarée"
    )
    parser.add_argument(
        "--to", help="commit où l'étiquette est posée ; par défaut, l'étiquette ou HEAD"
    )
    arguments = parser.parse_args(argv)
    if arguments.tag is None:
        try:
            version = declared_version()
        except ValueError as error:
            print(f"Notes impossibles : {error}.", file=sys.stderr)
            return 2
    else:
        found = parse_tag(arguments.tag)
        if found is None:
            print("Étiquette refusée : une version s'écrit v1.2.3 ou v1.2.3-rc.1.", file=sys.stderr)
            return 2
        version = found
    revision = arguments.to or (arguments.tag or "HEAD")
    if not _REVISION.fullmatch(revision):
        print("Révision refusée : un nom de commit, d'étiquette ou de branche.", file=sys.stderr)
        return 2
    try:
        sys.stdout.write(notes(version, revision))
    except subprocess.CalledProcessError as error:
        print(f"Historique illisible : git a échoué (code {error.returncode}).", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
