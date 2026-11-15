"""Structure du pipeline (US-0102) : matrice, porte de couverture, contrôle unique à exiger."""

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def load() -> dict[Any, Any]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    # YAML 1.1 lit la clé « on » comme le booléen vrai.
    value = workflow.get("on", workflow.get(True))
    assert isinstance(value, dict)
    return value


def test_pipeline_runs_on_every_merge_request_and_on_main() -> None:
    on = triggers(load())
    assert "pull_request" in on
    assert on["push"]["branches"] == ["main"]


def test_matrix_covers_python_313_314_and_postgresql_17_18() -> None:
    tests = load()["jobs"]["tests"]
    matrix = tests["strategy"]["matrix"]
    assert matrix["python"] == ["3.13", "3.14"]
    assert matrix["postgres"] == ["17", "18"]
    assert tests["strategy"]["fail-fast"] is False
    assert tests["env"]["ANSWERLINEAGE_TEST_POSTGRES_IMAGE"] == "postgres:${{ matrix.postgres }}"
    setup_uv = next(s for s in tests["steps"] if "setup-uv" in s.get("uses", ""))
    assert setup_uv["with"]["python-version"] == "${{ matrix.python }}"


def commands(job: dict[str, Any]) -> list[str]:
    return [step["run"] for step in job["steps"] if "run" in step]


def test_static_job_runs_format_lint_and_strict_typing() -> None:
    runs = commands(load()["jobs"]["static"])
    assert {"make format-check", "make lint", "make typecheck"} <= set(runs)


def test_tests_job_runs_tests_then_the_coverage_gate() -> None:
    runs = commands(load()["jobs"]["tests"])
    assert runs.index("make test") < runs.index("make coverage-gate")


def test_dependencies_are_installed_from_the_lockfile() -> None:
    jobs = load()["jobs"]
    for name in ("static", "tests"):
        assert "uv sync --all-packages --locked" in commands(jobs[name])


def test_single_required_check_fails_when_any_blocking_job_is_not_green() -> None:
    jobs = load()["jobs"]
    gate = jobs["ci-ok"]
    assert sorted(gate["needs"]) == sorted(name for name in jobs if name != "ci-ok")
    assert gate["if"] == "${{ always() }}"
    script = "\n".join(commands(gate))
    assert '!= "success"' in script
    assert "exit 1" in script


def test_pipeline_token_is_read_only() -> None:
    workflow = load()
    assert workflow["permissions"] == {"contents": "read"}
    for job in workflow["jobs"].values():
        assert "permissions" not in job
