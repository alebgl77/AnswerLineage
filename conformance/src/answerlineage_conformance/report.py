"""Le rapport du kit : un texte à lire, ou un document JSON à verser dans un dossier d'audit.

Les deux disent la même chose : le verdict, le profil, l'heure de référence,
les fichiers fouillés avec leur taille et leur empreinte, les contrôles
exécutés et les problèmes. Rien n'y dépend de l'horloge ni de la machine : les
mêmes fichiers, jugés à la même heure de référence, donnent le même rapport, à
l'octet près.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Final

from answerlineage_conformance import __version__
from answerlineage_conformance.controls import GROUPS, Group
from answerlineage_conformance.judgement import Judgement, Problem
from answerlineage_conformance.sources import FileRole, SourceFile

KIT: Final = "answerlineage-conformance"
# Change quand la forme du rapport JSON change.
REPORT_VERSION: Final = 1

_GROUPS: Final[dict[Group, str]] = {
    "shape": "forme",
    "context": "contexte",
    "content": "contenu",
    "projection": "projection",
}
_FILES: Final[dict[FileRole, str]] = {
    "events": "événements",
    "canaries": "canaris",
    "witness": "témoin",
    "logs": "journal",
    "metrics": "métriques",
}
_CANARIES: Final = {
    "prompt": "canari du prompt",
    "tool_result": "canari du résultat d'outil",
    "answer": "canari de la réponse",
}
_FORMS: Final = {
    "exact": "tel quel",
    "case_insensitive": "à la casse près",
    "base64": "en base64",
    "hex": "en hexadécimal",
    "draw": "par son seul tirage",
}


@dataclass(frozen=True)
class Report:
    judgement: Judgement
    # Heure de référence du jugement : celle où l'ingestion aurait reçu les événements.
    received_at: datetime
    # Les fichiers fouillés, dans l'ordre de la ligne de commande.
    files: tuple[SourceFile, ...]


def _instant(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def render_json(report: Report) -> str:
    """Le rapport sous sa forme stable : mêmes champs, dans le même ordre, à chaque fois."""
    judgement = report.judgement
    document: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "kit": {"name": KIT, "version": __version__},
        "profile": judgement.profile,
        "verdict": "conformant" if judgement.conformant else "non_conformant",
        "received_at": _instant(report.received_at),
        "capture": {"events": judgement.events, "runs": judgement.runs},
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


def _count(number: int, one: str, many: str) -> str:
    return f"{number} {one if number < 2 else many}"


def _place(problem: Problem) -> str:
    if problem.event is not None:
        return f"événement {problem.event}" + (f", {problem.pointer}" if problem.pointer else "")
    if problem.file is not None:
        return problem.file + (f", ligne {problem.line}" if problem.line is not None else "")
    return "capture entière"


def _detail(problem: Problem) -> str:
    if problem.canary is not None:
        seen = _CANARIES[problem.canary]
        return f" ({seen}, {_FORMS[problem.form]})" if problem.form is not None else f" ({seen})"
    return f" ({problem.reason})" if problem.reason is not None else ""


def _problems(judgement: Judgement) -> list[str]:
    if judgement.conformant:
        return ["Problèmes : aucun"]
    title = f"Problèmes : {judgement.problem_count}"
    if judgement.truncated:
        title += f", dont les {len(judgement.problems)} premiers sont listés"
    lines = [title]
    lines.extend(
        f"  [{_GROUPS[problem.group]}] {problem.code} : {_place(problem)}{_detail(problem)}"
        for problem in judgement.problems
    )
    lines.extend(["", "Ce que disent ces codes"])
    messages = {problem.code: problem.message for problem in judgement.problems}
    lines.extend(f"  {code} : {message}" for code, message in messages.items())
    return lines


def _controls(judgement: Judgement) -> list[str]:
    lines = [f"Contrôles exécutés : {len(judgement.controls)}"]
    for group in GROUPS:
        codes = [control.code for control in judgement.controls if control.group == group]
        # Un verdict rendu sans canari ne dit rien du contenu : le rapport l'écrit.
        ran = ", ".join(codes) or "non jugé, aucun canari n'a été fourni"
        lines.append(f"  {_GROUPS[group]} : {ran}")
    return lines


def _files(report: Report) -> list[str]:
    lines = ["Fichiers fouillés"]
    lines.extend(
        f"  {_FILES[file.role]}  {file.path}  {file.size} octets  sha256:{file.sha256}"
        for file in report.files
    )
    roles = {file.role for file in report.files}
    if "logs" not in roles:
        lines.append("  Journaux : aucun fichier fourni, rien n'a été fouillé.")
    if "metrics" not in roles:
        lines.append("  Métriques : aucun fichier fourni, rien n'a été fouillé.")
    return lines


def render_text(report: Report) -> str:
    """Le rapport à lire : le verdict d'abord, une ligne par problème, puis ce qui a été fouillé."""
    judgement = report.judgement
    lines = [
        f"Kit de conformité AnswerLineage {__version__}",
        f"Verdict : {'CONFORME' if judgement.conformant else 'NON CONFORME'}",
        f"Profil : {judgement.profile}",
        f"Heure de référence : {_instant(report.received_at)}",
        f"Capture : {_count(judgement.events, 'événement', 'événements')}, "
        f"{_count(judgement.runs, 'run', 'runs')}",
        "",
        *_problems(judgement),
        "",
        *_controls(judgement),
        "",
        *_files(report),
    ]
    return "\n".join(lines) + "\n"
