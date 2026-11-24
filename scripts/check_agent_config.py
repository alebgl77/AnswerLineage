"""Revue obligatoire de la configuration d'agents et des garde-fous.

Un fichier de configuration d'agent (hook, serveur MCP, règle, skill) ou un
garde-fou du pipeline pilote ce qu'un agent de code a le droit de faire. Une
merge request qui en modifie un échoue tant qu'un mainteneur ne lui a pas posé
le label ``agent-config-approved``.

Le contrôle lit la merge request dans l'environnement du pipeline. Sous GitHub
Actions, le workflow lui passe ``BASE_SHA`` et ``HEAD_SHA`` pour les fichiers
modifiés, ``PR_LABELS`` pour les labels (tableau JSON). Sous GitLab CI, il lit les
variables prédéfinies de la merge request (``CI_MERGE_REQUEST_DIFF_BASE_SHA``,
``CI_MERGE_REQUEST_LABELS``…). Hors d'une merge request, il n'y a rien à vérifier.

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
    # Le pipeline GitLab, et les emplacements où GitLab lit ses propriétaires du code
    # (le premier trouvé l'emporte : la racine, docs/, puis .gitlab/).
    ".gitlab-ci.yml",
    ".gitlab/",
    "CODEOWNERS",
    "docs/CODEOWNERS",
    ".pre-commit-config.yaml",
    "renovate.json",
    "scripts/",
    # Réglages et exceptions de la détection de secrets.
    ".gitleaks.toml",
    ".gitleaksignore",
    # Exceptions de l'analyse de vulnérabilités des images.
    "vulnerability-exceptions.toml",
    # Exceptions du corpus d'abus : les cas d'attaque que le produit ne bloque pas encore.
    "corpus/src/answerlineage_corpus/abuse_open.json",
    # Modèle de menaces : ses parades, ses risques acceptés et leurs signatures.
    "docs/securite/modele-de-menaces.toml",
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


def gitlab_labels(raw: str | None) -> set[str]:
    """Les labels d'une merge request GitLab, séparés par des virgules.

    Un nom de label GitLab ne peut pas contenir de virgule : la liste se découpe
    sans ambiguïté.
    """
    return {name.strip() for name in (raw or "").split(",") if name.strip()}


def read_merge_request(env: Mapping[str, str]) -> tuple[str, str, set[str]] | None:
    """Empreintes de la base et de la tête de la merge request, et ses labels.

    ``None`` hors d'une merge request. Sous GitLab, la tête est celle de la branche
    source : dans un pipeline de résultat fusionné, le commit du pipeline est la
    fusion avec la branche cible, dont les changements ne sont pas ceux de la merge
    request.
    """
    if env.get("GITLAB_CI") == "true":
        if not env.get("CI_MERGE_REQUEST_IID"):
            return None
        source = env.get("CI_MERGE_REQUEST_SOURCE_BRANCH_SHA") or env.get("CI_COMMIT_SHA", "")
        target = env.get("CI_MERGE_REQUEST_DIFF_BASE_SHA", "")
        return target, source, gitlab_labels(env.get("CI_MERGE_REQUEST_LABELS"))
    base, head = env.get("BASE_SHA"), env.get("HEAD_SHA")
    if not base or not head:
        return None
    return base, head, labels(env.get("PR_LABELS"))


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
    request = read_merge_request(env)
    if request is None:
        print("Hors d'une merge request : rien à vérifier.")
        return 0
    base, head, found = request
    if not re.fullmatch(r"[0-9a-f]{40}", base) or not re.fullmatch(r"[0-9a-f]{40}", head):
        print("Empreintes de la merge request illisibles.")
        return 2
    code, lines = evaluate(changed_files(base, head), APPROVAL_LABEL in found)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())
