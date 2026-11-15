"""Contrôle de politique des workflows GitHub Actions (US-0103, ENF-SEC-02).

La chaîne de construction ne doit pas devenir un vecteur d'attaque (PRD,
section 13). Ce contrôle échoue dès qu'un workflow :

- référence une action par balise ou par branche au lieu d'une empreinte de commit ;
- utilise une image de conteneur sans empreinte ``@sha256:`` ;
- ne déclare pas de permissions, ou en accorde en écriture à tout le workflow ;
- se déclenche sur ``pull_request_target`` ;
- laisse ``actions/checkout`` conserver le jeton du pipeline.

Usage : ``python -m scripts.check_workflows [fichier ...]``
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_COMMIT = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
# Référence d'image dans un script : registre connu, puis chemin de l'image.
_IMAGE_IN_SCRIPT = re.compile(
    r"(?<![\w./-])((?:ghcr\.io|docker\.io|quay\.io|gcr\.io|registry\.k8s\.io|"
    r"mcr\.microsoft\.com|public\.ecr\.aws|[\w.-]+\.pkg\.dev)/[^\s\"'`;|&)]+)"
)
_WRITE_ALL = "write-all"


@dataclass(frozen=True)
class Violation:
    path: str
    where: str
    rule: str
    message: str

    def __str__(self) -> str:
        return f"{self.path} [{self.where}] {self.rule} : {self.message}"


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def triggers(workflow: Mapping[Any, Any]) -> set[str]:
    """Événements qui déclenchent le workflow. YAML 1.1 lit la clé ``on`` comme vrai."""
    value = workflow.get("on", workflow.get(True))
    if isinstance(value, str):
        return {value}
    if isinstance(value, Mapping):
        return {str(key) for key in value}
    if isinstance(value, Sequence):
        return {str(item) for item in value}
    return set()


def _uses_violation(uses: object) -> str | None:
    if not isinstance(uses, str) or not uses.strip():
        return "référence d'action illisible"
    reference = uses.strip()
    if reference.startswith("./"):
        return None
    if reference.startswith("docker://"):
        if _DIGEST.search(reference):
            return None
        return f"l'image {reference} doit être épinglée par empreinte @sha256:"
    name, separator, version = reference.rpartition("@")
    if not separator or not _COMMIT.fullmatch(version):
        return f"{reference} doit être épinglée par empreinte de commit (40 caractères)"
    if not name:
        return "référence d'action illisible"
    return None


def _image_violation(image: object) -> str | None:
    if not isinstance(image, str) or not _DIGEST.search(image.strip()):
        return f"l'image {image} doit être épinglée par empreinte @sha256:"
    return None


def _permission_violations(permissions: object, *, top_level: bool) -> Iterator[str]:
    if permissions == _WRITE_ALL:
        yield "permissions: write-all est interdit"
        return
    if not top_level:
        return
    if permissions is None:
        yield "le workflow doit déclarer ses permissions (lecture seule par défaut)"
        return
    if isinstance(permissions, Mapping):
        for scope, level in permissions.items():
            if level == "write":
                yield (
                    f"la permission {scope}: write doit être accordée au job qui en a besoin, "
                    "pas à tout le workflow"
                )


def _steps(container: Mapping[str, Any]) -> Iterator[tuple[int, Mapping[str, Any]]]:
    steps = container.get("steps")
    if isinstance(steps, Sequence) and not isinstance(steps, str):
        for index, step in enumerate(steps, start=1):
            if isinstance(step, Mapping):
                yield index, step


def _step_violations(path: str, where: str, step: Mapping[str, Any]) -> Iterator[Violation]:
    if "uses" in step:
        problem = _uses_violation(step["uses"])
        if problem:
            yield Violation(path, where, "action-epinglee", problem)
        uses = str(step["uses"])
        if uses.split("@")[0].lower() == "actions/checkout":
            options = _mapping(step.get("with"))
            if options.get("persist-credentials") is not False:
                yield Violation(
                    path,
                    where,
                    "jeton-non-conserve",
                    "actions/checkout doit être appelé avec persist-credentials: false",
                )
    script = step.get("run")
    if isinstance(script, str):
        for match in _IMAGE_IN_SCRIPT.finditer(script):
            problem = _image_violation(match.group(1))
            if problem:
                yield Violation(path, where, "image-epinglee", problem)


def check_workflow(path: str, workflow: object) -> list[Violation]:
    """Renvoie les écarts d'un workflow à la politique."""
    if not isinstance(workflow, Mapping):
        return [Violation(path, "workflow", "lisible", "le fichier n'est pas un workflow")]
    violations: list[Violation] = []

    if "pull_request_target" in triggers(workflow):
        violations.append(
            Violation(
                path,
                "on",
                "pas-de-pull-request-target",
                "pull_request_target exécute du code avec les droits du dépôt cible",
            )
        )
    for denied in _permission_violations(workflow.get("permissions"), top_level=True):
        violations.append(Violation(path, "permissions", "permissions-minimales", denied))

    for job_id, job in _mapping(workflow.get("jobs")).items():
        job = _mapping(job)
        where = f"job {job_id}"
        for denied in _permission_violations(job.get("permissions"), top_level=False):
            violations.append(Violation(path, where, "permissions-minimales", denied))
        if "uses" in job:
            unpinned = _uses_violation(job["uses"])
            if unpinned:
                violations.append(Violation(path, where, "action-epinglee", unpinned))
        container = job.get("container")
        if container is not None:
            image = container.get("image") if isinstance(container, Mapping) else container
            unpinned = _image_violation(image)
            if unpinned:
                violations.append(Violation(path, where, "image-epinglee", unpinned))
        for name, service in _mapping(job.get("services")).items():
            unpinned = _image_violation(_mapping(service).get("image"))
            if unpinned:
                violations.append(
                    Violation(path, f"{where}, service {name}", "image-epinglee", unpinned)
                )
        for index, step in _steps(job):
            violations.extend(_step_violations(path, f"{where}, étape {index}", step))
    return violations


def check_action(path: str, action: object) -> list[Violation]:
    """Renvoie les écarts d'une action composite du dépôt."""
    if not isinstance(action, Mapping):
        return [Violation(path, "action", "lisible", "le fichier n'est pas une action")]
    violations: list[Violation] = []
    for index, step in _steps(_mapping(action.get("runs"))):
        violations.extend(_step_violations(path, f"étape {index}", step))
    return violations


def discover(root: Path) -> list[Path]:
    """Workflows et actions composites du dépôt."""
    github = root / ".github"
    found = [
        *github.glob("workflows/*.yml"),
        *github.glob("workflows/*.yaml"),
        *github.glob("actions/**/action.yml"),
        *github.glob("actions/**/action.yaml"),
    ]
    return sorted(found)


def check_file(path: Path) -> list[Violation]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        return [Violation(str(path), "fichier", "lisible", type(error).__name__)]
    if path.name.startswith("action."):
        return check_action(str(path), document)
    return check_workflow(str(path), document)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    paths = [Path(argument) for argument in arguments] or discover(Path.cwd())
    violations = [violation for path in paths for violation in check_file(path)]
    for violation in violations:
        print(violation)
    if violations:
        print(f"{len(violations)} écart(s) à la politique des workflows.")
        return 1
    print(f"Politique des workflows respectée ({len(paths)} fichier(s)).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
