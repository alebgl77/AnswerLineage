"""Cohérence du monorepo : chaque composant déclaré existe, est typé et s'importe."""

import importlib
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MEMBERS: list[str] = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"][
    "uv"
]["workspace"]["members"]
NON_PYTHON = ["ui", "deploy", "corpus", "spec/facets", "docs"]


def module_name(member: str) -> str:
    return "answerlineage_" + member.replace("-", "_")


def test_workspace_declares_the_six_python_components() -> None:
    assert sorted(MEMBERS) == ["adapters", "api", "collector", "mcp-server", "proxy", "workers"]


@pytest.mark.parametrize("member", MEMBERS)
def test_component_is_a_typed_package(member: str) -> None:
    package = ROOT / member / "src" / module_name(member)
    assert (ROOT / member / "pyproject.toml").is_file()
    assert (package / "__init__.py").is_file()
    assert (package / "py.typed").is_file()


@pytest.mark.parametrize("member", MEMBERS)
def test_component_is_importable_and_versioned(member: str) -> None:
    module = importlib.import_module(module_name(member))
    assert module.__version__ == "0.0.0"


@pytest.mark.parametrize("member", MEMBERS)
def test_distribution_name_follows_the_convention(member: str) -> None:
    project = tomllib.loads((ROOT / member / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    assert project["name"] == f"answerlineage-{member}"
    assert project["requires-python"] == ">=3.13"


@pytest.mark.parametrize("directory", NON_PYTHON)
def test_non_python_component_directory_exists(directory: str) -> None:
    assert (ROOT / directory).is_dir()


def test_mypy_checks_every_component() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    mypy = config["tool"]["mypy"]
    assert mypy["strict"] is True
    assert set(MEMBERS) <= set(mypy["files"])
    assert {f"{member}/src" for member in MEMBERS} <= set(mypy["mypy_path"])
