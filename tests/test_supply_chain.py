"""US-0103 : délai de carence des dépendances, détection de secrets, images épinglées."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def test_renovate_does_not_propose_a_release_younger_than_seven_days() -> None:
    config = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
    assert config["minimumReleaseAge"] == "7 days"
    # « strict » : aucune merge request tant qu'aucune version n'a passé le délai.
    assert config["internalChecksFilter"] == "strict"
    # Une version sans date de publication connue n'est pas proposée non plus.
    assert config["minimumReleaseAgeBehaviour"] == "timestamp-required"
    assert "helpers:pinGitHubActionDigests" in config["extends"]


def test_uv_refuses_releases_younger_than_seven_days() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert config["tool"]["uv"]["exclude-newer"] == "7 days"
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert 'exclude-newer-span = "P7D"' in lock


def test_gitleaks_image_is_pinned_and_tracked_by_renovate() -> None:
    script = (ROOT / "scripts" / "gitleaks.sh").read_text(encoding="utf-8")
    image = re.search(r'^IMAGE="([^"]+)"$', script, re.MULTILINE)
    assert image is not None
    assert re.fullmatch(r"ghcr\.io/gitleaks/gitleaks:v[0-9.]+@sha256:[0-9a-f]{64}", image.group(1))
    assert "--network none" in script
    assert ":/repo:ro" in script
    assert "--exit-code 1" in script

    config = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
    (manager,) = config["customManagers"]
    (pattern,) = manager["matchStrings"]
    match = re.search(pattern.replace("(?<", "(?P<"), script)
    assert match is not None
    assert match.group("depName") == "ghcr.io/gitleaks/gitleaks"
    assert match.group("currentDigest").startswith("sha256:")
    (file_pattern,) = manager["managerFilePatterns"]
    assert re.search(file_pattern.strip("/"), "scripts/gitleaks.sh")


def test_pipeline_blocks_on_secrets_and_proves_the_detector_works() -> None:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8"))
    job = workflow["jobs"]["secrets"]
    checkout = job["steps"][0]
    assert checkout["with"]["fetch-depth"] == 0
    runs = [step["run"] for step in job["steps"] if "run" in step]
    assert runs == ["scripts/gitleaks.sh selftest", "scripts/gitleaks.sh scan"]
    assert "secrets" in workflow["jobs"]["ci-ok"]["needs"]


def test_secret_detector_canary_is_not_a_literal_of_the_repository() -> None:
    """Le canari est fabriqué à l'exécution : le dépôt ne contient aucun faux secret."""
    script = (ROOT / "scripts" / "gitleaks.sh").read_text(encoding="utf-8")
    assert re.search(r"ghp_[0-9A-Za-z]{36}", script) is None
    assert "/dev/urandom" in script


def test_code_scanning_runs_for_python_and_workflows_with_a_scoped_permission() -> None:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8"))
    job = workflow["jobs"]["codeql"]
    assert job["strategy"]["matrix"]["language"] == ["python", "actions"]
    assert job["permissions"] == {"contents": "read", "security-events": "write"}
    assert "codeql" in workflow["jobs"]["ci-ok"]["needs"]
