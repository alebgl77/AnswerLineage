"""Le pipeline GitLab rejoue les étapes bloquantes du pipeline GitHub, commande par commande.

``.github/workflows/ci.yml`` est le pipeline de référence : ses étapes bloquantes sont
les jobs que ``ci-ok`` exige. ``.gitlab-ci.yml`` les rejoue : chacun de ses stages
porte le nom du job GitHub qu'il reproduit, et ses jobs lancent les mêmes commandes
du dépôt. Une étape que l'un gagne et que l'autre n'a pas fait échouer ces tests,
sauf exception déclarée ici avec sa raison.

Aucun GitLab n'est joignable depuis les tests : le fichier est validé contre le
schéma que publie GitLab (``spec/gitlab-ci/``), puis par un contrôle de ce que le
schéma ne voit pas (stages, ``extends``, ``needs``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft7Validator

from scripts.check_policy import GITLAB_GLOBALS

ROOT = Path(__file__).resolve().parents[1]
GITHUB = ROOT / ".github" / "workflows" / "ci.yml"
GITLAB = ROOT / ".gitlab-ci.yml"
SCHEMA = ROOT / "spec" / "gitlab-ci" / "ci.json"
GATE = "ci-ok"

# Étapes bloquantes de GitHub sans équivalent dans le pipeline GitLab, et pourquoi.
GITHUB_ONLY = {
    "codeql": (
        "Hors de GitHub Advanced Security, la licence de CodeQL n'autorise l'analyse "
        "automatisée que d'un code ouvert hébergé et maintenu sur GitHub.com. "
        "L'équivalent GitLab, l'analyse SAST, s'impose par une politique du groupe, "
        "hors du dépôt : ses images se tirent par étiquette (docs/ci.md)."
    ),
}
# Les commandes de la démonstration tournent dans le poste, un conteneur Debian qui
# partage le réseau, les dossiers et le démon Docker du job : l'explorateur s'y pilote
# dans Chromium, qui demande la glibc que l'image du démon, Alpine, n'a pas.
POSTE = re.compile(r"^docker exec poste ")
# Ce qui, dans une préparation, serait une étape du pipeline et non de l'outillage.
STEP = re.compile(r"(^|[\s;&|(])(make|uv run|uv sync|scripts/|python3? -m scripts)\b")


def load(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def commands(script: str) -> list[str]:
    """Les commandes d'un bloc de shell : une par ligne, continuations jointes, sans commentaire."""
    found = []
    for line in re.sub(r"\\\n", " ", script).splitlines():
        command = " ".join(line.split())
        if command and not command.startswith("#"):
            found.append(command)
    return found


def flatten(lines: object) -> Iterator[str]:
    """Les lignes d'un script GitLab, qui aplatit les listes imbriquées."""
    if isinstance(lines, str):
        yield lines
    elif isinstance(lines, Sequence):
        for line in lines:
            yield from flatten(line)


def blocking_steps(workflow: Mapping[str, Any]) -> dict[str, list[str]]:
    """Les commandes de chaque job que ``ci-ok`` exige, dans l'ordre de ses étapes."""
    jobs = workflow["jobs"]
    return {
        name: [
            command
            for step in jobs[name].get("steps", [])
            if "run" in step
            for command in commands(step["run"])
        ]
        for name in jobs[GATE]["needs"]
    }


def merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Comme ``extends`` : les tables se fusionnent clé à clé, le reste est remplacé."""
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(value, Mapping) and isinstance(current, Mapping):
            merged[key] = merge(current, value)
        else:
            merged[key] = value
    return merged


def resolve(document: Mapping[str, Any], name: str, seen: tuple[str, ...] = ()) -> dict[str, Any]:
    """Un job et ce qu'il hérite par ``extends``, du plus lointain au plus proche."""
    assert name not in seen, f"extends circulaire : {' → '.join((*seen, name))}"
    job = document[name]
    parents = job.get("extends", [])
    merged: dict[str, Any] = {}
    for parent in [parents] if isinstance(parents, str) else parents:
        merged = merge(merged, resolve(document, parent, (*seen, name)))
    return merge(merged, {key: value for key, value in job.items() if key != "extends"})


def gitlab_jobs(document: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Les jobs du pipeline, sans les modèles (``.nom``), avec ce qu'ils héritent."""
    return {
        name: resolve(document, name)
        for name, value in document.items()
        if name not in GITLAB_GLOBALS and not name.startswith(".") and isinstance(value, Mapping)
    }


def gitlab_steps(document: Mapping[str, Any]) -> dict[str, list[str]]:
    """Les commandes de chaque stage, job par job, dans l'ordre du fichier."""
    steps: dict[str, list[str]] = {}
    for job in gitlab_jobs(document).values():
        found = [
            POSTE.sub("", command)
            for line in flatten(job.get("script", []))
            for command in commands(line)
        ]
        steps.setdefault(job["stage"], []).extend(found)
    return steps


def differences(github: Mapping[str, Any], gitlab: Mapping[str, Any]) -> list[str]:
    """Ce qu'un pipeline fait et que l'autre ne fait pas : jobs, puis commandes."""
    expected = blocking_steps(github)
    found = gitlab_steps(gitlab)
    problems = [
        *(
            f"le job GitHub {name} n'a pas de stage GitLab"
            for name in sorted(expected.keys() - found.keys() - GITHUB_ONLY.keys())
        ),
        *(
            f"le stage GitLab {name} ne reproduit aucune étape bloquante de GitHub"
            for name in sorted(found.keys() - expected.keys())
        ),
        *(
            f"{name} est déclaré absent de GitLab, mais n'est plus une étape bloquante de GitHub"
            for name in sorted(GITHUB_ONLY.keys() - expected.keys())
        ),
        *(
            f"{name} est déclaré absent de GitLab, mais y a un stage"
            for name in sorted(GITHUB_ONLY.keys() & found.keys())
        ),
    ]
    for name in sorted(expected.keys() & found.keys()):
        problems.extend(
            f"{name} : « {command} » manque à GitLab"
            for command in sorted(set(expected[name]) - set(found[name]))
        )
        problems.extend(
            f"{name} : « {command} » manque à GitHub"
            for command in sorted(set(found[name]) - set(expected[name]))
        )
    return problems


def test_the_gitlab_pipeline_runs_the_blocking_steps_of_github_command_by_command() -> None:
    assert differences(load(GITHUB), load(GITLAB)) == []


def test_a_step_gained_by_either_pipeline_is_a_difference() -> None:
    github, gitlab = load(GITHUB), load(GITLAB)
    github["jobs"]["static"]["steps"].append({"name": "Nouveau", "run": "make nouveau"})
    assert differences(github, gitlab) == ["static : « make nouveau » manque à GitLab"]

    github, gitlab = load(GITHUB), load(GITLAB)
    gitlab["static"]["script"].append("make nouveau")
    assert differences(github, gitlab) == ["static : « make nouveau » manque à GitHub"]

    # Une commande de la démonstration ne compte qu'une fois sortie du poste.
    github, gitlab = load(GITHUB), load(GITLAB)
    gitlab["demo"]["script"].remove("docker exec poste scripts/check_demo.sh checks")
    assert differences(github, gitlab) == [
        "demo : « scripts/check_demo.sh checks » manque à GitLab"
    ]


def test_a_blocking_job_gained_by_either_pipeline_is_a_difference() -> None:
    github, gitlab = load(GITHUB), load(GITLAB)
    github["jobs"]["bench"] = {"steps": [{"run": "make bench"}]}
    # Un job que ci-ok n'exige pas n'est pas une étape bloquante.
    assert differences(github, gitlab) == []
    github["jobs"][GATE]["needs"].append("bench")
    assert differences(github, gitlab) == ["le job GitHub bench n'a pas de stage GitLab"]

    github, gitlab = load(GITHUB), load(GITLAB)
    gitlab["bench"] = {"stage": "bench", "script": ["make bench"]}
    assert differences(github, gitlab) == [
        "le stage GitLab bench ne reproduit aucune étape bloquante de GitHub"
    ]


def test_a_declared_exception_that_no_longer_holds_is_a_difference() -> None:
    github, gitlab = load(GITHUB), load(GITLAB)
    github["jobs"][GATE]["needs"].remove("codeql")
    assert differences(github, gitlab) == [
        "codeql est déclaré absent de GitLab, mais n'est plus une étape bloquante de GitHub"
    ]

    github, gitlab = load(GITHUB), load(GITLAB)
    gitlab["codeql"] = {"stage": "codeql", "script": ["make sast"]}
    assert "codeql est déclaré absent de GitLab, mais y a un stage" in differences(github, gitlab)


def github_matrix(workflow: Mapping[str, Any]) -> set[tuple[str, str]]:
    matrix = workflow["jobs"]["tests"]["strategy"]["matrix"]
    return {(python, postgres) for python in matrix["python"] for postgres in matrix["postgres"]}


def gitlab_matrix(document: Mapping[str, Any]) -> set[tuple[str, str]]:
    """Les cases (Python, PostgreSQL) des jobs du stage tests : matrice et service."""
    cells: set[tuple[str, str]] = set()
    for job in gitlab_jobs(document).values():
        if job["stage"] != "tests":
            continue
        (service,) = job["services"]
        found = re.search(r"/postgres:(\d+)", service["name"])
        assert found is not None, service["name"]
        for entry in job["parallel"]["matrix"]:
            cells.update((python, found[1]) for python in entry["UV_PYTHON"])
    return cells


def test_the_tests_run_on_the_same_python_and_postgresql_matrix() -> None:
    assert gitlab_matrix(load(GITLAB)) == github_matrix(load(GITHUB))


def test_the_matrix_comparison_sees_a_missing_cell() -> None:
    gitlab = load(GITLAB)
    gitlab["tests-postgres-17"]["parallel"] = {"matrix": [{"UV_PYTHON": ["3.13"]}]}
    assert github_matrix(load(GITHUB)) - gitlab_matrix(gitlab) == {("3.14", "17")}


def test_both_pipelines_run_the_same_uv_and_the_same_node() -> None:
    """Une mise à jour de l'outillage d'un côté se fait de l'autre."""
    github, gitlab = load(GITHUB), load(GITLAB)
    uv = re.search(r"/uv:(\d+\.\d+\.\d+)-", gitlab[".uv"]["image"])
    assert uv is not None
    assert uv[1] == github["env"]["UV_VERSION"]
    node = re.search(r"/node:(\d+)\.", gitlab["explorer-ui"]["image"])
    assert node is not None
    (setup_node,) = (
        step
        for step in github["jobs"]["explorer"]["steps"]
        if str(step.get("uses", "")).startswith("actions/setup-node@")
    )
    assert node[1] == setup_node["with"]["node-version"]


@pytest.fixture(scope="module")
def schema() -> Draft7Validator:
    document = json.loads(SCHEMA.read_text(encoding="utf-8"))
    Draft7Validator.check_schema(document)
    return Draft7Validator(document)


def test_the_gitlab_pipeline_follows_the_schema_published_by_gitlab(
    schema: Draft7Validator,
) -> None:
    errors = [
        f"{'/'.join(str(part) for part in error.absolute_path)} : {error.message}"
        for error in schema.iter_errors(load(GITLAB))
    ]
    assert errors == []


@pytest.mark.parametrize(
    "change",
    [
        lambda document: document["static"].update(scrypt=["make check"]),
        lambda document: document["static"].update(stage=3),
        lambda document: document["static"].update(timeout=["10 minutes"]),
        lambda document: document["workflow"].update(rules="toujours"),
    ],
)
def test_the_schema_rejects_what_gitlab_would_reject(schema: Draft7Validator, change: Any) -> None:
    """Le schéma lit vraiment le fichier : une clé inconnue, une valeur mal typée sont refusées."""
    document = load(GITLAB)
    change(document)
    assert not schema.is_valid(document)


def test_the_gitlab_pipeline_is_well_formed_beyond_the_schema() -> None:
    """Ce que GitLab refuserait et que le schéma ne voit pas, plus ce que ce dépôt exige."""
    document = load(GITLAB)
    stages = document["stages"]
    jobs = gitlab_jobs(document)
    assert {job["stage"] for job in jobs.values()} == set(stages)
    for name, job in jobs.items():
        assert job["stage"] in stages, name
        assert list(flatten(job["script"])), name
        assert "image" in job, name
        # Comme timeout-minutes : un job bloqué ne retient pas le pipeline indéfiniment.
        assert "timeout" in job, name
        for needed in job.get("needs", []):
            assert needed in jobs, f"{name} attend {needed}, qui n'est pas un job"


def test_the_preparation_of_a_job_runs_no_step_of_the_pipeline() -> None:
    """Préparer l'outillage (démon, poste) n'est pas une étape : une étape cachée là échapperait
    à la comparaison des deux pipelines."""
    for name, job in gitlab_jobs(load(GITLAB)).items():
        for line in flatten(job.get("before_script", [])):
            assert STEP.search(line) is None, f"{name} : « {line} »"
    assert STEP.search("uv sync --all-packages --locked")
    assert STEP.search("cd ui && make ui")
    assert STEP.search("dind dockerd --host=unix:///var/run/docker.sock") is None
