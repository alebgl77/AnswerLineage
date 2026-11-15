"""Une merge request qui modifie la configuration d'agents attend l'approbation d'un mainteneur."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.check_agent_config import (
    APPROVAL_LABEL,
    changed_files,
    evaluate,
    is_protected,
    labels,
    main,
    printable,
)


@pytest.mark.parametrize(
    "path",
    [
        ".claude/settings.json",
        ".claude/hooks/garde.py",
        ".claude/skills/revue/SKILL.md",
        ".claude/rules/python.md",
        ".mcp.json",
        "CLAUDE.md",
        ".github/workflows/ci.yml",
        ".github/CODEOWNERS",
        ".devcontainer/devcontainer.json",
        "scripts/check_policy.py",
        "renovate.json",
        "vulnerability-exceptions.toml",
    ],
)
def test_agent_configuration_and_guardrails_are_protected(path: str) -> None:
    assert is_protected(path)


@pytest.mark.parametrize(
    "path",
    ["proxy/src/answerlineage_proxy/app.py", "docs/CLAUDE.md", "README.md", "api/.mcp.json.bak"],
)
def test_ordinary_files_are_not_protected(path: str) -> None:
    assert not is_protected(path)


def test_merge_request_touching_a_hook_fails_until_a_maintainer_approves() -> None:
    changed = ["proxy/src/answerlineage_proxy/app.py", ".claude/hooks/garde.py", ".mcp.json"]
    code, lines = evaluate(changed, approved=False)
    assert code == 1
    assert "  .claude/hooks/garde.py" in lines
    assert "  .mcp.json" in lines
    assert APPROVAL_LABEL in lines[-1]

    code, lines = evaluate(changed, approved=True)
    assert code == 0
    assert "approuvée" in lines[-1]


def test_merge_request_touching_ordinary_files_passes() -> None:
    assert evaluate(["README.md", "api/src/answerlineage_api/app.py"], approved=False)[0] == 0
    assert evaluate([], approved=False)[0] == 0


def test_file_name_from_the_merge_request_cannot_inject_pipeline_commands() -> None:
    """Un nom de fichier est une entrée non fiable : il n'est affiché que s'il est anodin."""
    hostile = "scripts/x\n::set-env name=PATH::/tmp"
    assert printable("scripts/check_policy.py") == "scripts/check_policy.py"
    assert "::" not in printable(hostile)
    _, lines = evaluate([hostile], approved=False)
    assert all("::" not in line for line in lines)


def test_labels_are_read_from_the_pipeline_event() -> None:
    assert labels('["bug", "agent-config-approved"]') == {"bug", "agent-config-approved"}
    assert labels("null") == set()
    assert labels(None) == set()
    assert labels("pas du json") == set()
    assert labels('[1, "ok"]') == {"ok"}


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["git", "-C", str(repository), *arguments],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "HOME": str(repository),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    )
    return result.stdout.strip()


@pytest.fixture
def merge_request(tmp_path: Path) -> tuple[Path, str, str]:
    """Dépôt avec une branche qui modifie un fichier ordinaire et un hook."""
    git(tmp_path, "init", "--quiet", "--initial-branch", "main")
    (tmp_path / "README.md").write_text("projet\n", "utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "--quiet", "--no-gpg-sign", "-m", "base")
    base = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", "--quiet", "-b", "travail")
    (tmp_path / "README.md").write_text("projet modifié\n", "utf-8")
    (tmp_path / ".claude" / "hooks").mkdir(parents=True)
    (tmp_path / ".claude" / "hooks" / "garde é.py").write_text("print('x')\n", "utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "--quiet", "--no-gpg-sign", "-m", "travail")
    return tmp_path, base, git(tmp_path, "rev-parse", "HEAD")


def test_changed_files_come_from_the_merge_request_range(
    merge_request: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, base, head = merge_request
    monkeypatch.chdir(repository)
    assert sorted(changed_files(base, head)) == [".claude/hooks/garde é.py", "README.md"]


def test_pipeline_check_fails_without_the_label_and_passes_with_it(
    merge_request: tuple[Path, str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository, base, head = merge_request
    monkeypatch.chdir(repository)
    assert main({"BASE_SHA": base, "HEAD_SHA": head, "PR_LABELS": "[]"}) == 1
    assert ".claude/hooks/garde é.py" in capsys.readouterr().out
    approved = {"BASE_SHA": base, "HEAD_SHA": head, "PR_LABELS": '["agent-config-approved"]'}
    assert main(approved) == 0


def test_outside_a_merge_request_there_is_nothing_to_check(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main({}) == 0
    assert "rien à vérifier" in capsys.readouterr().out
    assert main({"BASE_SHA": "", "HEAD_SHA": ""}) == 0
    assert main({"BASE_SHA": "main; rm -rf /", "HEAD_SHA": "x"}) == 2
