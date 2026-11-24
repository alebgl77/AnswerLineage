"""Le rapport de la suite A2A : un texte à lire, ou un document JSON pour un dossier d'audit.

Les deux disent la même chose : l'extension jugée, le verdict, les problèmes,
les contrôles exécutés et les deux fichiers avec leur taille et leur empreinte.
Rien n'y dépend de l'horloge ni de la machine : les mêmes fichiers, donnés sous
les mêmes chemins, donnent le même rapport, à l'octet près.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Final

from answerlineage_conformance import __version__
from answerlineage_conformance.a2a import GROUPS, URI, Group, Judgement, Problem
from answerlineage_conformance.report import KIT
from answerlineage_conformance.sources import SourceFile

# Change quand la forme du rapport JSON de la suite change.
REPORT_VERSION: Final = 1
SUITE: Final = "a2a"

_GROUPS: Final[dict[Group, str]] = {
    "version": "version",
    "declaration": "déclaration",
    "call": "appel",
    "response": "réponse",
    "content": "contenu",
}
_FILES: Final = {"card": "Agent Card", "exchange": "échange"}


@dataclass(frozen=True)
class A2aReport:
    judgement: Judgement
    # L'Agent Card, puis l'échange.
    files: tuple[SourceFile, ...]


def render_json(report: A2aReport) -> str:
    """Le rapport sous sa forme stable : mêmes champs, dans le même ordre, à chaque fois."""
    judgement = report.judgement
    document: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "kit": {"name": KIT, "version": __version__},
        "suite": SUITE,
        "extension": URI,
        "verdict": "conformant" if judgement.conformant else "non_conformant",
        "files": [
            {"role": file.role, "path": file.path, "bytes": file.size, "sha256": file.sha256}
            for file in report.files
        ],
        "controls": [
            {
                "code": control.code,
                "group": control.group,
                "problems": judgement.counts.get(control.code, 0),
            }
            for control in judgement.controls
        ],
        "problem_count": judgement.problem_count,
        "truncated": judgement.truncated,
        "problems": [asdict(problem) for problem in judgement.problems],
    }
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def _line(problem: Problem) -> str:
    reason = f" ({problem.reason})" if problem.reason is not None else ""
    return (
        f"  [{_GROUPS[problem.group]}] {problem.code} : {problem.file}, {problem.pointer}{reason}"
    )


def _problems(judgement: Judgement) -> list[str]:
    if judgement.conformant:
        return ["Problèmes : aucun"]
    title = f"Problèmes : {judgement.problem_count}"
    if judgement.truncated:
        title += f", dont les {len(judgement.problems)} premiers sont listés"
    lines = [title, *(_line(problem) for problem in judgement.problems)]
    lines.extend(["", "Ce que disent ces codes"])
    messages = {problem.code: problem.message for problem in judgement.problems}
    lines.extend(f"  {code} : {message}" for code, message in messages.items())
    return lines


def render_text(report: A2aReport) -> str:
    """Le rapport à lire : le verdict d'abord, une ligne par problème, puis ce qui a été fouillé."""
    judgement = report.judgement
    lines = [
        f"Kit de conformité AnswerLineage {__version__}",
        "Suite : extension A2A, version 1",
        f"Extension : {URI}",
        f"Verdict : {'CONFORME' if judgement.conformant else 'NON CONFORME'}",
        "",
        *_problems(judgement),
        "",
        f"Contrôles exécutés : {len(judgement.controls)}",
    ]
    for group in GROUPS:
        codes = [control.code for control in judgement.controls if control.group == group]
        lines.append(f"  {_GROUPS[group]} : {', '.join(codes)}")
    lines.extend(["", "Fichiers fouillés"])
    lines.extend(
        f"  {_FILES[file.role]}  {file.path}  {file.size} octets  sha256:{file.sha256}"
        for file in report.files
    )
    return "\n".join(lines) + "\n"
