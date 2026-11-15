"""Une action référencée par balise fait échouer le contrôle de politique."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.check_workflows import (
    Violation,
    check_action,
    check_file,
    check_script,
    check_workflow,
    discover,
    main,
    triggers,
)

ROOT = Path(__file__).resolve().parent.parent
SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"
DIGEST = "sha256:" + "c0" * 32


def workflow(steps: list[dict[str, Any]] | None = None, **overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "name": "exemple",
        "on": {"pull_request": None},
        "permissions": {"contents": "read"},
        "jobs": {"build": {"runs-on": "ubuntu-24.04", "steps": steps or []}},
    }
    document.update(overrides)
    return document


def rules(violations: list[Violation]) -> list[str]:
    return [violation.rule for violation in violations]


def test_compliant_workflow_has_no_violation() -> None:
    steps: list[dict[str, Any]] = [
        {"uses": f"actions/checkout@{SHA}", "with": {"persist-credentials": False}},
        {"uses": f"github/codeql-action/init@{SHA}"},
        {"uses": "./.github/actions/locale"},
        {"uses": f"docker://ghcr.io/org/outil:1.2@{DIGEST}"},
        {"run": f"docker run --rm ghcr.io/org/outil:1.2@{DIGEST} scan"},
    ]
    assert check_workflow("ci.yml", workflow(steps)) == []


@pytest.mark.parametrize(
    "reference",
    [
        "actions/checkout@v7",
        "actions/checkout@v7.0.1",
        "actions/checkout@main",
        "actions/checkout@3d3c42e",
        "actions/checkout",
        f"actions/checkout@{SHA.upper()}",
        "docker://ghcr.io/org/outil:1.2",
        "docker://ghcr.io/org/outil@sha256:abc",
        "",
        42,
    ],
)
def test_action_referenced_by_tag_or_branch_fails(reference: object) -> None:
    step = {"uses": reference, "with": {"persist-credentials": False}}
    violations = check_workflow("ci.yml", workflow([step]))
    assert rules(violations) == ["action-epinglee"]
    assert violations[0].where == "job build, étape 1"


def test_reusable_workflow_must_be_pinned_too() -> None:
    document = workflow(
        jobs={"appel": {"uses": "org/depot/.github/workflows/build.yml@main"}},
    )
    assert rules(check_workflow("ci.yml", document)) == ["action-epinglee"]
    document["jobs"]["appel"]["uses"] = f"org/depot/.github/workflows/build.yml@{SHA}"
    assert check_workflow("ci.yml", document) == []


def test_checkout_must_not_keep_the_pipeline_token() -> None:
    kept = check_workflow("ci.yml", workflow([{"uses": f"actions/checkout@{SHA}"}]))
    assert rules(kept) == ["jeton-non-conserve"]
    explicit = {"uses": f"Actions/Checkout@{SHA}", "with": {"persist-credentials": True}}
    assert rules(check_workflow("ci.yml", workflow([explicit]))) == ["jeton-non-conserve"]


def test_missing_permissions_fail() -> None:
    document = workflow()
    del document["permissions"]
    assert rules(check_workflow("ci.yml", document)) == ["permissions-minimales"]


@pytest.mark.parametrize("permissions", ["write-all", {"contents": "write"}])
def test_write_permission_for_the_whole_workflow_fails(permissions: object) -> None:
    assert rules(check_workflow("ci.yml", workflow(permissions=permissions))) == [
        "permissions-minimales"
    ]


@pytest.mark.parametrize("permissions", [{}, "read-all", {"contents": "read", "actions": "none"}])
def test_read_only_permissions_pass(permissions: object) -> None:
    assert check_workflow("ci.yml", workflow(permissions=permissions)) == []


def test_write_permission_is_granted_job_by_job() -> None:
    document = workflow()
    document["jobs"]["build"]["permissions"] = {"security-events": "write"}
    assert check_workflow("ci.yml", document) == []
    document["jobs"]["build"]["permissions"] = "write-all"
    assert rules(check_workflow("ci.yml", document)) == ["permissions-minimales"]


@pytest.mark.parametrize(
    "on", ["pull_request_target", ["push", "pull_request_target"], {"pull_request_target": {}}]
)
def test_pull_request_target_is_forbidden(on: object) -> None:
    assert rules(check_workflow("ci.yml", workflow(on=on))) == ["pas-de-pull-request-target"]


def test_yaml_reads_the_on_key_as_true() -> None:
    document = yaml.safe_load("on: [push, pull_request_target]\npermissions: {}\njobs: {}\n")
    assert triggers(document) == {"push", "pull_request_target"}
    assert rules(check_workflow("ci.yml", document)) == ["pas-de-pull-request-target"]
    assert triggers({}) == set()


def test_container_images_must_be_pinned_by_digest() -> None:
    document = workflow()
    document["jobs"]["build"]["container"] = "node:22"
    document["jobs"]["build"]["services"] = {
        "base": {"image": "postgres:18"},
        "cache": {"image": f"valkey/valkey:8@{DIGEST}"},
    }
    violations = check_workflow("ci.yml", document)
    assert rules(violations) == ["image-epinglee", "image-epinglee"]
    assert violations[1].where == "job build, service base"
    document["jobs"]["build"]["container"] = {"image": f"node:22@{DIGEST}"}
    document["jobs"]["build"]["services"]["base"]["image"] = f"postgres:18@{DIGEST}"
    assert check_workflow("ci.yml", document) == []


def test_image_pulled_in_a_script_must_be_pinned_by_digest() -> None:
    step = {"run": "docker run --rm ghcr.io/gitleaks/gitleaks:v8.30.1 git /repo\necho fini"}
    assert rules(check_workflow("ci.yml", workflow([step]))) == ["image-epinglee"]


def gated(needs: object, condition: object = "${{ always() }}") -> dict[str, Any]:
    document = workflow()
    document["jobs"]["tests"] = {"runs-on": "ubuntu-24.04", "steps": []}
    document["jobs"]["ci-ok"] = {"runs-on": "ubuntu-24.04", "needs": needs, "steps": []}
    if condition is not None:
        document["jobs"]["ci-ok"]["if"] = condition
    return document


def test_required_job_must_depend_on_every_other_job() -> None:
    """Un job oublié dans la porte ne bloquerait aucune fusion."""
    assert check_workflow("ci.yml", gated(["build", "tests"])) == []
    violations = check_workflow("ci.yml", gated(["build"]))
    assert rules(violations) == ["porte-complete"]
    assert "il manque : tests" in violations[0].message
    assert rules(check_workflow("ci.yml", gated("build"))) == ["porte-complete"]
    assert rules(check_workflow("ci.yml", gated(None))) == ["porte-complete"]


def test_required_job_must_run_even_when_another_job_fails() -> None:
    """Sans « always() », le job serait sauté, et un contrôle sauté ne bloque pas."""
    assert rules(check_workflow("ci.yml", gated(["build", "tests"], None))) == ["porte-complete"]
    assert rules(check_workflow("ci.yml", gated(["build", "tests"], "success()"))) == [
        "porte-complete"
    ]


def test_image_referenced_by_a_script_must_be_pinned_by_digest() -> None:
    pinned = f'IMAGE="ghcr.io/org/outil:v1@{DIGEST}"\ndocker run --rm "$IMAGE" scan\n'
    assert check_script("scripts/outil.sh", pinned) == []
    floating = "docker run --rm ghcr.io/org/outil:v1 scan\n"
    assert rules(check_script("scripts/outil.sh", floating)) == ["image-epinglee"]


def test_composite_action_steps_are_checked() -> None:
    action = {"runs": {"using": "composite", "steps": [{"uses": "actions/cache@v5"}]}}
    assert rules(check_action("action.yml", action)) == ["action-epinglee"]
    assert rules(check_action("action.yml", "pas une action")) == ["lisible"]


def test_unreadable_documents_are_reported() -> None:
    assert rules(check_workflow("ci.yml", ["pas", "un", "workflow"])) == ["lisible"]
    steps: Any = "pas une liste"
    assert check_workflow("ci.yml", workflow(jobs={"build": {"steps": steps}})) == []


def test_files_are_discovered_and_checked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    composite = tmp_path / ".github" / "actions" / "prepare"
    composite.mkdir(parents=True)
    good = workflows / "ci.yml"
    good.write_text(yaml.safe_dump(workflow()), encoding="utf-8")
    bad = workflows / "release.yaml"
    bad.write_text(yaml.safe_dump(workflow([{"uses": "org/action@v1"}])), encoding="utf-8")
    broken = workflows / "casse.yml"
    broken.write_text("jobs: [", encoding="utf-8")
    action = composite / "action.yml"
    action.write_text(
        yaml.safe_dump({"runs": {"using": "composite", "steps": [{"uses": "org/a@v2"}]}}),
        encoding="utf-8",
    )

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "analyse.sh"
    script.write_text("docker run quay.io/org/analyseur:2 /src\n", encoding="utf-8")

    assert discover(tmp_path) == sorted([action, broken, good, bad, script])
    assert check_file(good) == []
    assert rules(check_file(script)) == ["image-epinglee"]
    assert rules(check_file(broken)) == ["lisible"]
    assert rules(check_file(action)) == ["action-epinglee"]

    assert main([str(good)]) == 0
    assert "respectée" in capsys.readouterr().out
    assert main([str(good), str(bad)]) == 1
    output = capsys.readouterr().out
    assert "org/action@v1 doit être épinglée par empreinte de commit" in output
    assert "1 écart(s)" in output


def test_main_checks_the_working_directory_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["check_workflows"])
    assert main() == 0


def test_repository_workflows_respect_the_policy() -> None:
    paths = discover(ROOT)
    assert ROOT / ".github" / "workflows" / "ci.yml" in paths
    assert [violation for path in paths for violation in check_file(path)] == []
