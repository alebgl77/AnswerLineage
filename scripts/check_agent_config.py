"""Revue obligatoire de la configuration d'agents et des garde-fous.

Un fichier de configuration d'agent (hook, serveur MCP, règle, skill) ou un
garde-fou du pipeline pilote ce qu'un agent de code a le droit de faire. Une
merge request qui en modifie un échoue tant qu'un mainteneur ne lui a pas posé
le label ``agent-config-approved``.

Le contrôle lit la merge request dans l'environnement du pipeline :
``BASE_SHA`` et ``HEAD_SHA`` pour les fichiers modifiés, ``PR_LABELS`` pour les
labels (tableau JSON). Hors d'une merge request, il n'y a rien à vérifier.

Usage : ``python -m scripts.check_agent_config``
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence

APPROVAL_LABEL = "agent-config-approved"

# Chemins protégés. Une entrée terminée par « / » couvre tout le dossier.
PROTECTED: tuple[str, ...] = (
    # Configuration d'agents de code.
    ".claude/",
    ".mcp.json",
    "CLAUDE.md",
    "AGENTS.md",
    "GEMINI.md",
    ".cursor/",
    ".cursorrules",
    ".codex/",
    ".gemini/",
    ".github/copilot-instructions.md",
    # Garde-fous de l'atelier et du pipeline.
    ".devcontainer/",
    ".github/workflows/",
    ".github/actions/",
    ".github/CODEOWNERS",
    ".pre-commit-config.yaml",
    "renovate.json",
    "scripts/",
)

_PRINTABLE = re.compile(r"[\w./ @+=,-]{1,300}")


def is_protected(path: str) -> bool:
    return any(
        path == entry or (entry.endswith("/") and path.startswith(entry)) for entry in PROTECTED
    )


def protected_changes(changed: Sequence[str]) -> list[str]:
    return sorted({path for path in changed if is_protected(path)})


def printable(path: str) -> str:
    """Un nom de fichier vient de la merge request : il n'est affiché que s'il est anodin."""
    return path if _PRINTABLE.fullmatch(path) else "<nom de fichier non affichable>"


def changed_files(base: str, head: str) -> list[str]:
    result = subprocess.run(  # noqa: S603 — commande fixe, empreintes fournies par le pipeline.
        ["git", "diff", "--name-only", "-z", f"{base}...{head}"],  # noqa: S607
        check=True,
        capture_output=True,
    )
    return [name.decode("utf-8", errors="replace") for name in result.stdout.split(b"\0") if name]


def labels(raw: str | None) -> set[str]:
    try:
        parsed = json.loads(raw or "[]")
    except ValueError:
        return set()
    return {item for item in parsed if isinstance(item, str)} if isinstance(parsed, list) else set()


def evaluate(changed: Sequence[str], approved: bool) -> tuple[int, list[str]]:
    """Code de sortie et lignes à afficher."""
    protected = protected_changes(changed)
    if not protected:
        return 0, ["Aucun fichier de configuration d'agents ni garde-fou n'est modifié."]
    lines = ["Fichiers protégés modifiés :", *(f"  {printable(path)}" for path in protected)]
    if approved:
        return 0, [*lines, f"Label {APPROVAL_LABEL} présent : la modification a été approuvée."]
    return 1, [
        *lines,
        f"Ces fichiers demandent la revue d'un mainteneur, qui pose le label {APPROVAL_LABEL}.",
    ]


def main(environment: Mapping[str, str] | None = None) -> int:
    env = os.environ if environment is None else environment
    base, head = env.get("BASE_SHA"), env.get("HEAD_SHA")
    if not base or not head:
        print("Hors d'une merge request : rien à vérifier.")
        return 0
    if not re.fullmatch(r"[0-9a-f]{40}", base) or not re.fullmatch(r"[0-9a-f]{40}", head):
        print("Empreintes de la merge request illisibles.")
        return 2
    code, lines = evaluate(
        changed_files(base, head), APPROVAL_LABEL in labels(env.get("PR_LABELS"))
    )
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())
