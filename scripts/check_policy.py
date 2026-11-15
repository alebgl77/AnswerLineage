"""Contrôle de politique du dépôt : workflows, scripts, conteneur de développement.

La chaîne de construction et l'atelier de développement ne doivent pas devenir
un vecteur d'attaque. Ce contrôle échoue dès que :

- une action est référencée par balise ou par branche au lieu d'une empreinte de commit ;
- une image de conteneur est utilisée sans empreinte ``@sha256:`` ;
- un workflow ne déclare pas de permissions, ou en accorde en écriture à tout le workflow ;
- un workflow se déclenche sur ``pull_request_target`` ;
- ``actions/checkout`` conserve le jeton du pipeline ;
- le job ``ci-ok``, seul contrôle exigé avant fusion, ne dépend pas de tous les autres ;
- un agent de code s'exécute sur une entrée non fiable (issue, commentaire, merge
  request) avec un secret ou un droit d'écriture ;
- le conteneur de développement monte un identifiant de l'hôte ou reçoit un de ses jetons ;
- un chemin protégé n'a pas de propriétaire dans ``.github/CODEOWNERS``.

Usage : ``python -m scripts.check_policy [fichier ...]``
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from scripts.check_agent_config import PROTECTED

_COMMIT = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
# Référence d'image dans un script : registre connu, puis chemin de l'image.
_IMAGE_IN_SCRIPT = re.compile(
    r"(?<![\w./-])((?:ghcr\.io|docker\.io|quay\.io|gcr\.io|registry\.k8s\.io|"
    r"mcr\.microsoft\.com|public\.ecr\.aws|[\w.-]+\.pkg\.dev)/[^\s\"'`;|&)]+)"
)
_WRITE_ALL = "write-all"
# Événements dont le contenu vient d'un tiers : issue, commentaire, merge request.
UNTRUSTED_TRIGGERS = frozenset(
    {
        "discussion",
        "discussion_comment",
        "issue_comment",
        "issues",
        "pull_request",
        "pull_request_review",
        "pull_request_review_comment",
        "pull_request_target",
        "workflow_run",
    }
)
# Actions et commandes qui lancent un agent de code.
_AGENT_ACTIONS = re.compile(
    r"^(anthropics/claude-code|openai/codex|google-github-actions/run-gemini-cli|"
    r"github/copilot|sourcegraph/amp|aider-ai/|cursor/)",
    re.IGNORECASE,
)
_AGENT_COMMANDS = re.compile(
    r"(?<![\w./-])(claude|codex|gemini|aider|cursor-agent|opencode|goose|amp)(?![\w./-])"
)
_SECRET_REFERENCE = re.compile(r"\bsecrets\s*[.\[]")
# Emplacements de l'hôte qui portent des identifiants.
_CREDENTIAL_LOCATIONS = (
    ".ssh",
    ".gnupg",
    ".pypirc",
    ".npmrc",
    ".netrc",
    ".git-credentials",
    ".config/gh",
    ".config/gcloud",
    ".aws",
    ".azure",
    ".docker",
    ".kube",
    ".claude",
    "docker.sock",
)
_FORWARDED_SECRET = re.compile(
    r"\$\{localEnv:[^}]*(TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API_?KEY|AUTH)[^}]*\}",
    re.IGNORECASE,
)
# Job exigé par la protection de la branche principale.
GATE_JOB = "ci-ok"


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
    violations.extend(_gate_violations(path, _mapping(workflow.get("jobs"))))
    violations.extend(_agent_violations(path, workflow))
    return violations


def _runs_agent(step: Mapping[str, Any]) -> bool:
    uses = step.get("uses")
    if isinstance(uses, str) and _AGENT_ACTIONS.search(uses.strip()):
        return True
    script = step.get("run")
    return isinstance(script, str) and _AGENT_COMMANDS.search(script) is not None


def _grants_write(permissions: object) -> bool:
    if permissions == _WRITE_ALL:
        return True
    return isinstance(permissions, Mapping) and "write" in permissions.values()


def _agent_violations(path: str, workflow: Mapping[Any, Any]) -> Iterator[Violation]:
    """Un agent de code ne tourne pas sur une entrée non fiable avec un secret ou une écriture.

    Une issue, un commentaire ou une merge request peuvent porter des
    instructions piégées : l'agent qui les lit ne doit rien pouvoir en faire.
    """
    untrusted = sorted(triggers(workflow) & UNTRUSTED_TRIGGERS)
    if not untrusted:
        return
    workflow_writes = _grants_write(workflow.get("permissions"))
    for job_id, job in _mapping(workflow.get("jobs")).items():
        job = _mapping(job)
        agent_steps = [index for index, step in _steps(job) if _runs_agent(step)]
        if not agent_steps:
            continue
        writes = workflow_writes or _grants_write(job.get("permissions"))
        secrets = _SECRET_REFERENCE.search(yaml.safe_dump(dict(job))) is not None
        if writes or secrets:
            reason = " et ".join(
                label
                for label, present in (("un droit d'écriture", writes), ("un secret", secrets))
                if present
            )
            yield Violation(
                path,
                f"job {job_id}, étape {agent_steps[0]}",
                "agent-sur-entree-non-fiable",
                f"un agent de code s'exécute avec {reason} sur une entrée non fiable "
                f"({', '.join(untrusted)})",
            )


def _gate_violations(path: str, jobs: Mapping[str, Any]) -> Iterator[Violation]:
    """Le job exigé avant fusion doit échouer dès qu'un autre job n'est pas vert."""
    gate = jobs.get(GATE_JOB)
    if gate is None:
        return
    gate = _mapping(gate)
    needs = gate.get("needs")
    needed = {needs} if isinstance(needs, str) else set(needs or [])
    missing = sorted(set(jobs) - {GATE_JOB} - needed)
    where = f"job {GATE_JOB}"
    if missing:
        yield Violation(
            path,
            where,
            "porte-complete",
            f"le job doit dépendre de tous les autres ; il manque : {', '.join(missing)}",
        )
    if "always()" not in str(gate.get("if", "")):
        yield Violation(
            path,
            where,
            "porte-complete",
            "le job doit s'exécuter même quand un autre échoue (if: always())",
        )


def check_script(path: str, script: str) -> list[Violation]:
    """Renvoie les images non épinglées qu'un script du dépôt référence."""
    violations = []
    for match in _IMAGE_IN_SCRIPT.finditer(script):
        problem = _image_violation(match.group(1))
        if problem:
            violations.append(Violation(path, "script", "image-epinglee", problem))
    return violations


def check_devcontainer(path: str, config: object) -> list[Violation]:
    """Le conteneur de développement ne reçoit ni identifiant ni jeton de l'hôte."""
    if not isinstance(config, Mapping):
        return [Violation(path, "fichier", "lisible", "la configuration n'est pas un objet JSON")]
    violations = []
    mounts = config.get("mounts")
    run_args = config.get("runArgs")
    sources = [
        *(mounts if isinstance(mounts, list) else []),
        *(run_args if isinstance(run_args, list) else []),
        config.get("workspaceMount", ""),
    ]
    for source in sources:
        described = json.dumps(source, ensure_ascii=False)
        for location in _CREDENTIAL_LOCATIONS:
            if re.search(rf"(?<![\w-]){re.escape(location)}(?![\w-])", described):
                violations.append(
                    Violation(
                        path,
                        "montages",
                        "conteneur-sans-identifiant",
                        f"le conteneur ne doit pas monter {location} depuis l'hôte",
                    )
                )
    for section in ("containerEnv", "remoteEnv"):
        for name, value in _mapping(config.get(section)).items():
            if isinstance(value, str) and _FORWARDED_SECRET.search(value):
                violations.append(
                    Violation(
                        path,
                        section,
                        "conteneur-sans-identifiant",
                        f"la variable {name} transmet un jeton de l'hôte au conteneur",
                    )
                )
    return violations


_FROM = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", re.IGNORECASE)


def check_dockerfile(path: str, content: str) -> list[Violation]:
    """Chaque image de base d'un Dockerfile est épinglée par empreinte."""
    violations = []
    stages: set[str] = set()
    for line in content.splitlines():
        match = _FROM.match(line)
        if match is None:
            continue
        image, alias = match.group(1), match.group(2)
        if image.lower() != "scratch" and image.lower() not in stages:
            problem = _image_violation(image)
            if problem:
                violations.append(Violation(path, "FROM", "image-epinglee", problem))
        if alias:
            stages.add(alias.lower())
    return violations


def check_codeowners(path: str, content: str) -> list[Violation]:
    """Chaque chemin protégé a un propriétaire, dont la revue peut être exigée."""
    owned = set()
    for line in content.splitlines():
        fields = line.split("#", 1)[0].split()
        if len(fields) >= 2 and all(owner.startswith("@") for owner in fields[1:]):
            owned.add(fields[0].lstrip("/"))
    return [
        Violation(
            path,
            "propriétaires",
            "chemin-protege-sans-proprietaire",
            f"{protected} est protégé mais n'a pas de propriétaire",
        )
        for protected in PROTECTED
        if protected not in owned
    ]


def check_action(path: str, action: object) -> list[Violation]:
    """Renvoie les écarts d'une action composite du dépôt."""
    if not isinstance(action, Mapping):
        return [Violation(path, "action", "lisible", "le fichier n'est pas une action")]
    violations: list[Violation] = []
    for index, step in _steps(_mapping(action.get("runs"))):
        violations.extend(_step_violations(path, f"étape {index}", step))
    return violations


def discover(root: Path) -> list[Path]:
    """Workflows, actions composites et scripts du dépôt."""
    github = root / ".github"
    found = [
        *github.glob("workflows/*.yml"),
        *github.glob("workflows/*.yaml"),
        *github.glob("actions/**/action.yml"),
        *github.glob("actions/**/action.yaml"),
        *(root / "scripts").glob("*.sh"),
        *(root / ".devcontainer").glob("**/devcontainer.json"),
        *github.glob("CODEOWNERS"),
        *(
            path
            for path in root.rglob("Dockerfile*")
            if not {".venv", "node_modules", ".git"} & set(path.relative_to(root).parts)
        ),
    ]
    return sorted(found)


def check_file(path: Path) -> list[Violation]:
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".sh":
            return check_script(str(path), text)
        if path.name == "CODEOWNERS":
            return check_codeowners(str(path), text)
        if path.name.startswith("Dockerfile"):
            return check_dockerfile(str(path), text)
        if path.name == "devcontainer.json":
            return check_devcontainer(str(path), json.loads(text))
        document = yaml.safe_load(text)
    except (OSError, ValueError, yaml.YAMLError) as error:
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
        print(f"{len(violations)} écart(s) à la politique du dépôt.")
        return 1
    print(f"Politique du dépôt respectée ({len(paths)} fichier(s)).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
