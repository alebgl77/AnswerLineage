"""Une action référencée par balise fait échouer le contrôle de politique."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.check_agent_config import PROTECTED
from scripts.check_policy import (
    Violation,
    check_action,
    check_codeowners,
    check_compose,
    check_devcontainer,
    check_dockerfile,
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
    monkeypatch.setattr("sys.argv", ["check_policy"])
    assert main() == 0


def test_repository_workflows_respect_the_policy() -> None:
    paths = discover(ROOT)
    assert ROOT / ".github" / "workflows" / "ci.yml" in paths
    assert [violation for path in paths for violation in check_file(path)] == []


def agent_workflow(on: object, step: dict[str, Any], **job: Any) -> dict[str, Any]:
    document = workflow([step], on=on)
    document["jobs"]["build"].update(job)
    return document


AGENT_ACTION = {"uses": f"anthropics/claude-code-action@{SHA}"}


@pytest.mark.parametrize(
    "on", ["issues", "issue_comment", "pull_request", "pull_request_review_comment", "discussion"]
)
def test_code_agent_on_untrusted_input_must_not_write(on: str) -> None:
    """Une issue ou un commentaire piégé ne doit pas donner la main sur le dépôt."""
    document = agent_workflow({on: None}, AGENT_ACTION, permissions={"contents": "write"})
    violations = check_workflow("agent.yml", document)
    assert rules(violations) == ["agent-sur-entree-non-fiable"]
    assert "un droit d'écriture" in violations[0].message
    assert on in violations[0].message


def test_code_agent_on_untrusted_input_must_not_receive_a_secret() -> None:
    step = {**AGENT_ACTION, "with": {"api_key": "${{ secrets.CLE_API }}"}}
    violations = check_workflow("agent.yml", agent_workflow({"issue_comment": None}, step))
    assert rules(violations) == ["agent-sur-entree-non-fiable"]
    assert "un secret" in violations[0].message
    from_env = {"run": "claude -p 'trie cette issue'", "env": {"CLE": "${{ secrets['CLE'] }}"}}
    assert rules(check_workflow("agent.yml", agent_workflow({"issues": None}, from_env))) == [
        "agent-sur-entree-non-fiable"
    ]


def test_code_agent_command_line_is_recognized_in_a_script() -> None:
    step = {"run": "npx codex exec 'corrige'"}
    document = agent_workflow({"issue_comment": None}, step, permissions={"issues": "write"})
    assert rules(check_workflow("agent.yml", document)) == ["agent-sur-entree-non-fiable"]
    harmless = {"run": "echo 'le dossier .claude/ est protégé' && ./scripts/gemini-report.sh"}
    document = agent_workflow({"issue_comment": None}, harmless, permissions={"issues": "write"})
    assert check_workflow("bot.yml", document) == []


def test_code_agent_without_secret_or_write_access_is_allowed_on_untrusted_input() -> None:
    assert check_workflow("agent.yml", agent_workflow({"issue_comment": None}, AGENT_ACTION)) == []


def test_code_agent_with_write_access_is_allowed_on_trusted_triggers() -> None:
    document = agent_workflow(
        {"workflow_dispatch": None, "schedule": [{"cron": "0 6 * * 1"}]},
        {**AGENT_ACTION, "with": {"api_key": "${{ secrets.CLE_API }}"}},
        permissions={"contents": "write"},
    )
    assert check_workflow("agent.yml", document) == []


def test_workflow_without_agent_may_use_secrets_on_pull_requests() -> None:
    step = {"run": "make deploy", "env": {"JETON": "${{ secrets.JETON }}"}}
    assert check_workflow("ci.yml", agent_workflow({"pull_request": None}, step)) == []


def test_development_container_must_not_mount_host_credentials() -> None:
    clean = {"mounts": [], "remoteEnv": {"GH_TOKEN": ""}, "postCreateCommand": "make setup"}
    assert check_devcontainer("devcontainer.json", clean) == []
    for mount in (
        "source=${localEnv:HOME}/.ssh,target=/home/dev/.ssh,type=bind",
        {"source": "${localEnv:HOME}/.config/gh", "target": "/home/dev/.config/gh", "type": "bind"},
        "source=/var/run/docker.sock,target=/var/run/docker.sock,type=bind",
        "source=${localEnv:HOME}/.claude,target=/home/dev/.claude,type=bind",
    ):
        violations = check_devcontainer("devcontainer.json", {"mounts": [mount]})
        assert rules(violations) == ["conteneur-sans-identifiant"], mount
    by_argument = {"runArgs": ["-v", "${localEnv:HOME}/.aws:/home/dev/.aws:ro"]}
    assert rules(check_devcontainer("devcontainer.json", by_argument)) == [
        "conteneur-sans-identifiant"
    ]
    assert rules(check_devcontainer("devcontainer.json", ["pas un objet"])) == ["lisible"]


def test_development_container_must_not_receive_host_tokens() -> None:
    for section in ("containerEnv", "remoteEnv"):
        config = {section: {"GH_TOKEN": "${localEnv:GH_TOKEN}", "LANG": "${localEnv:LANG}"}}
        violations = check_devcontainer("devcontainer.json", config)
        assert rules(violations) == ["conteneur-sans-identifiant"]
        assert "GH_TOKEN" in violations[0].message
    renamed = {"remoteEnv": {"JETON": "${localEnv:UV_PUBLISH_TOKEN}"}}
    assert rules(check_devcontainer("devcontainer.json", renamed)) == ["conteneur-sans-identifiant"]


def test_every_protected_path_needs_a_code_owner() -> None:
    complete = "\n".join(f"/{path} @mainteneur" for path in PROTECTED)
    assert check_codeowners("CODEOWNERS", complete) == []
    without_claude = "\n".join(f"/{path} @mainteneur" for path in PROTECTED if path != ".claude/")
    violations = check_codeowners("CODEOWNERS", without_claude + "\n# /.claude/ @mainteneur\n")
    assert rules(violations) == ["chemin-protege-sans-proprietaire"]
    assert ".claude/" in violations[0].message
    assert len(check_codeowners("CODEOWNERS", "/.claude/\n")) == len(PROTECTED)


def test_dockerfile_base_images_must_be_pinned_by_digest() -> None:
    pinned = f"FROM python:3.13@{DIGEST} AS build\nRUN make\nFROM build AS tests\nFROM scratch\n"
    assert check_dockerfile("Dockerfile", pinned) == []
    floating = "FROM --platform=linux/amd64 python:3.13-slim\nFROM node:22 AS ui\n"
    assert rules(check_dockerfile("Dockerfile", floating)) == ["image-epinglee", "image-epinglee"]


def test_development_container_files_are_discovered(tmp_path: Path) -> None:
    folder = tmp_path / ".devcontainer"
    folder.mkdir()
    config = folder / "devcontainer.json"
    config.write_text('{"mounts": ["source=/home/u/.ssh,target=/root/.ssh,type=bind"]}', "utf-8")
    dockerfile = folder / "Dockerfile"
    dockerfile.write_text("FROM python:3.13\n", "utf-8")
    owners = tmp_path / ".github" / "CODEOWNERS"
    owners.parent.mkdir()
    owners.write_text("", "utf-8")
    ignored = tmp_path / ".venv" / "lib" / "Dockerfile"
    ignored.parent.mkdir(parents=True)
    ignored.write_text("FROM python:3.13\n", "utf-8")

    assert discover(tmp_path) == sorted([config, dockerfile, owners])
    assert rules(check_file(config)) == ["conteneur-sans-identifiant"]
    assert rules(check_file(dockerfile)) == ["image-epinglee"]
    assert len(check_file(owners)) == len(PROTECTED)
    config.write_text("{", "utf-8")
    assert rules(check_file(config)) == ["lisible"]


def service(**settings: Any) -> dict[str, Any]:
    return {"services": {"api": {"build": ".", **settings}}}


@pytest.mark.parametrize(
    "port",
    [
        "127.0.0.1:8000:8000",
        "127.0.0.1:8000:8000/tcp",
        "[::1]:8000:8000",
        {"target": 8000, "published": "8000", "host_ip": "127.0.0.1"},
        {"target": 8000, "published": 8000, "host_ip": "::1"},
    ],
)
def test_a_port_published_on_the_loopback_is_allowed(port: object) -> None:
    assert check_compose("compose.yaml", service(ports=[port])) == []
    assert check_compose("compose.yaml", service()) == []


@pytest.mark.parametrize(
    "port",
    [
        "8000:8000",
        "8000",
        8000,
        "0.0.0.0:8000:8000",
        "192.168.1.10:8000:8000",
        "[::]:8000:8000",
        {"target": 8000, "published": "8000"},
        {"target": 8000, "published": "8000", "host_ip": "0.0.0.0"},  # noqa: S104
        ["liste"],
    ],
)
def test_a_port_published_beyond_the_loopback_fails(port: object) -> None:
    violations = check_compose("compose.yaml", service(ports=["127.0.0.1:1:1", port]))
    assert rules(violations) == ["port-local"]
    assert "api" in violations[0].where


def test_a_service_on_the_host_network_listens_everywhere() -> None:
    assert rules(check_compose("compose.yaml", service(network_mode="host"))) == ["port-local"]
    assert check_compose("compose.yaml", service(network_mode="bridge")) == []


def test_an_image_pulled_by_a_compose_file_must_be_pinned_by_digest() -> None:
    pinned = {"services": {"postgres": {"image": f"postgres:18@{DIGEST}"}}}
    assert check_compose("compose.yaml", pinned) == []
    floating = {"services": {"postgres": {"image": "postgres:18"}, "cache": {}}}
    assert rules(check_compose("compose.yaml", floating)) == ["image-epinglee", "image-epinglee"]
    # Une image construite sur place porte un nom local, sans empreinte.
    built = {"services": {"api": {"image": "answerlineage-demo", "build": {"context": "."}}}}
    assert check_compose("compose.yaml", built) == []


def test_a_compose_file_that_is_not_one_is_reported() -> None:
    assert rules(check_compose("compose.yaml", ["pas", "un", "fichier"])) == ["lisible"]
    assert rules(check_compose("compose.yaml", {"services": ["api"]})) == ["lisible"]
    assert rules(check_compose("compose.yaml", {"services": {"api": "image"}})) == ["lisible"]


def test_compose_files_are_discovered_and_checked(tmp_path: Path) -> None:
    root = tmp_path / "compose.yaml"
    root.write_text("services:\n  api:\n    build: .\n    ports: ['8000:8000']\n", "utf-8")
    nested = tmp_path / "deploy" / "compose.essai.yml"
    nested.parent.mkdir()
    nested.write_text("services:\n  base:\n    image: postgres:18\n", "utf-8")

    assert discover(tmp_path) == sorted([root, nested])
    assert rules(check_file(root)) == ["port-local"]
    assert rules(check_file(nested)) == ["image-epinglee"]


def published_everywhere(**settings: Any) -> str:
    return yaml.safe_dump({"services": {"api": {"build": ".", "ports": ["8000:8000"], **settings}}})


@pytest.mark.parametrize(
    "name",
    [
        "compose.override.yaml",
        "compose.override.yml",
        "docker-compose.yml",
        "docker-compose.override.yml",
        "docker-compose.override.yaml",
    ],
)
def test_an_override_file_that_compose_merges_by_itself_is_checked(
    tmp_path: Path, name: str
) -> None:
    """Compose fusionne d'office le fichier d'à côté : il n'échappe pas au contrôle."""
    (tmp_path / "compose.yaml").write_text("services:\n  api:\n    build: .\n", "utf-8")
    override = tmp_path / name
    override.write_text(published_everywhere(), "utf-8")

    assert override in discover(tmp_path)
    assert rules(check_file(override, tmp_path)) == ["port-local"]


def test_a_compose_file_is_checked_whatever_its_name(tmp_path: Path) -> None:
    free = tmp_path / "deploy" / "demo.yml"
    free.parent.mkdir()
    free.write_text(published_everywhere(), "utf-8")
    included_only = tmp_path / "pile.yaml"
    included_only.write_text("include:\n  - deploy/demo.yml\n", "utf-8")
    workflow_file = tmp_path / ".github" / "workflows" / "compose-demo.yml"
    workflow_file.parent.mkdir(parents=True)
    # Un workflow a des services, mais dans ses jobs : ce n'est pas un fichier compose,
    # même quand son nom commence comme celui d'un fichier compose.
    document = workflow()
    document["jobs"]["build"]["services"] = {"base": {"image": f"postgres:18@{DIGEST}"}}
    workflow_file.write_text(yaml.safe_dump(document), "utf-8")
    for ignored in (".venv", ".venv-314", "node_modules", ".git"):
        hidden = tmp_path / ignored / "paquet" / "compose.yaml"
        hidden.parent.mkdir(parents=True)
        hidden.write_text(published_everywhere(), "utf-8")
    (tmp_path / "reglages.yaml").write_text("services: [aucun]\nniveau: 2\n", "utf-8")
    (tmp_path / "liste.yml").write_text("- services\n", "utf-8")
    (tmp_path / "casse.yml").write_text("services: [", "utf-8")

    assert discover(tmp_path) == sorted([free, included_only, workflow_file])
    assert rules(check_file(free, tmp_path)) == ["port-local"]
    assert check_file(workflow_file, tmp_path) == []
    # Le fichier inclus est contrôlé avec celui qui l'inclut.
    violations = check_file(included_only, tmp_path)
    assert rules(violations) == ["port-local"]
    assert violations[0].path == str(free)


@pytest.mark.parametrize(
    "target",
    [
        "absent.yaml",
        "../ailleurs.yaml",
        "/etc/compose.yaml",
        "${REPERTOIRE}/compose.yaml",
        "oci://registre.exemple/pile:1",
        "https://github.com/org/depot.git#main:compose.yaml",
        ".venv/paquet/compose.yaml",
        "node_modules/paquet/compose.yaml",
        "dossier",
        "",
        42,
    ],
)
def test_an_include_toward_a_file_absent_from_the_repository_fails(
    tmp_path: Path, target: object
) -> None:
    """Le contrôle ne lit que le dépôt : un fichier inclus d'ailleurs lui échapperait."""
    root = tmp_path / "depot"
    root.mkdir()
    (tmp_path / "ailleurs.yaml").write_text(published_everywhere(), "utf-8")
    (root / "dossier").mkdir()
    for ignored in (".venv", "node_modules"):
        hidden = root / ignored / "paquet" / "compose.yaml"
        hidden.parent.mkdir(parents=True)
        hidden.write_text("services: {}\n", "utf-8")
    compose = root / "compose.yaml"
    compose.write_text(yaml.safe_dump({"include": [target], "services": {}}), "utf-8")

    violations = check_file(compose, root)
    assert rules(violations) == ["fichier-du-depot"]
    assert violations[0].where == "include"


def test_an_include_is_read_in_its_short_and_long_forms(tmp_path: Path) -> None:
    (tmp_path / "base.yaml").write_text("services: {}\n", "utf-8")
    (tmp_path / "outils").mkdir()
    (tmp_path / "outils" / "cache.yaml").write_text("services: {}\n", "utf-8")
    compose = tmp_path / "compose.yaml"
    entries: list[object] = [
        "base.yaml",
        {"path": "outils/cache.yaml", "project_directory": "outils"},
        {"path": ["base.yaml", "outils/absent.yaml"]},
        {"project_directory": "outils"},
    ]
    compose.write_text(yaml.safe_dump({"include": entries}), "utf-8")

    violations = check_file(compose, tmp_path)
    assert rules(violations) == ["fichier-du-depot", "fichier-du-depot"]
    assert "outils/absent.yaml" in violations[0].message
    assert rules(check_compose("compose.yaml", {"include": "base.yaml"})) == ["lisible"]


def test_an_included_file_is_checked_as_a_compose_file_whatever_its_name(tmp_path: Path) -> None:
    """Un fichier inclus, même sans extension YAML, publie ses ports comme les autres."""
    hidden = tmp_path / "notes.txt"
    hidden.write_text(published_everywhere(), "utf-8")
    compose = tmp_path / "compose.yaml"
    compose.write_text("include:\n  - notes.txt\nservices: {}\n", "utf-8")

    violations = check_file(compose, tmp_path)
    assert rules(violations) == ["port-local"]
    assert violations[0].path == str(hidden)
    hidden.write_text("services: [", "utf-8")
    assert rules(check_file(compose, tmp_path)) == ["lisible"]


def test_files_that_include_each_other_are_checked_once(tmp_path: Path) -> None:
    first = tmp_path / "compose.yaml"
    second = tmp_path / "autre.yaml"
    first.write_text("include: [autre.yaml]\nservices: {}\n", "utf-8")
    second.write_text("include: [compose.yaml]\n" + published_everywhere(), "utf-8")

    assert rules(check_file(first, tmp_path)) == ["port-local"]


def test_a_violation_reached_twice_is_reported_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    included = tmp_path / "commun.yaml"
    included.write_text(published_everywhere(), "utf-8")
    compose = tmp_path / "compose.yaml"
    compose.write_text("include: [commun.yaml]\n", "utf-8")

    assert main([str(compose), str(included)]) == 1
    assert "1 écart(s)" in capsys.readouterr().out


@pytest.mark.parametrize(
    "extends",
    [
        {"file": "absent.yaml", "service": "api"},
        {"file": "../ailleurs.yaml", "service": "api"},
        {"file": None, "service": "api"},
        ["liste"],
    ],
)
def test_an_extends_toward_a_file_absent_from_the_repository_fails(
    tmp_path: Path, extends: object
) -> None:
    root = tmp_path / "depot"
    root.mkdir()
    (tmp_path / "ailleurs.yaml").write_text(published_everywhere(), "utf-8")
    compose = root / "compose.yaml"
    document = {"services": {"api": {"build": ".", "extends": extends}}}
    compose.write_text(yaml.safe_dump(document), "utf-8")

    violations = check_file(compose, root)
    assert rules(violations) == ["fichier-du-depot"]
    assert violations[0].where == "service api"


def test_an_extends_within_the_repository_is_followed(tmp_path: Path) -> None:
    common = tmp_path / "commun.yaml"
    common.write_text(published_everywhere(), "utf-8")
    compose = tmp_path / "compose.yaml"
    document = {
        "services": {
            "base": {"build": "."},
            "api": {"extends": {"file": "commun.yaml", "service": "api"}},
            "worker": {"build": ".", "extends": "base"},
            "seed": {"build": ".", "extends": {"service": "base"}},
        }
    }
    compose.write_text(yaml.safe_dump(document), "utf-8")

    violations = check_file(compose, tmp_path)
    assert rules(violations) == ["port-local"]
    assert violations[0].path == str(common)


@pytest.mark.parametrize(
    "network",
    [
        {"external": True, "name": "host"},
        {"external": "true", "name": "host"},
        {"external": {"name": "host"}},
        {"driver": "host"},
    ],
)
def test_a_service_on_the_host_network_through_an_external_network_fails(
    network: dict[str, Any],
) -> None:
    for attached in (["reseau"], {"reseau": {"aliases": ["api"]}}):
        document = {
            "services": {"api": {"build": ".", "networks": attached}, "seed": {"build": "."}},
            "networks": {"reseau": network},
        }
        violations = check_compose("compose.yaml", document)
        assert rules(violations) == ["port-local"]
        assert violations[0].where == "service api"


def test_an_external_network_named_host_by_its_key_fails() -> None:
    document = {
        "services": {"api": {"build": ".", "networks": ["host"]}},
        "networks": {"host": {"external": True}},
    }
    assert rules(check_compose("compose.yaml", document)) == ["port-local"]


def test_other_networks_are_allowed() -> None:
    document = {
        "services": {
            "api": {"build": ".", "networks": ["interne", "partage"]},
            "seed": {"build": ".", "network_mode": "service:api"},
        },
        "networks": {
            "interne": None,
            "partage": {"external": True, "name": "answerlineage"},
            "hote": {"external": True, "name": "host"},
        },
    }
    assert check_compose("compose.yaml", document) == []
