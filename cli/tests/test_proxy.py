"""``answerlineage proxy`` cède la place au proxy MCP, avec ses arguments tels quels."""

from __future__ import annotations

import sys

import pytest

from answerlineage_cli import proxy
from answerlineage_cli.cli import build_parser, main
from answerlineage_cli.exits import Exit


@pytest.fixture
def executed(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Ce que la commande aurait exécuté à sa place."""
    calls: list[list[str]] = []

    def execute(program: str, arguments: list[str]) -> None:
        calls.append([program, *arguments])

    monkeypatch.setattr(proxy, "_execute", execute)
    return calls


@pytest.mark.parametrize(
    "arguments",
    [
        # Mode stdio : la commande du serveur suit « -- », options comprises.
        ["--server-name", "meteo", "--", "/opt/mcp/serveur", "--url", "--", "-v"],
        # Mode HTTP, et l'aide du proxy lui-même.
        ["--upstream", "http://127.0.0.1:9000"],
        ["--help"],
        [],
    ],
)
def test_the_process_becomes_the_proxy_with_the_same_arguments(
    executed: list[list[str]], arguments: list[str]
) -> None:
    assert main(["proxy", *arguments]) == Exit.OK
    assert executed == [
        [sys.executable, sys.executable, "-m", "answerlineage_proxy.cli", *arguments]
    ]


def test_without_the_proxy_the_command_says_what_to_install(
    executed: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(proxy, "installed", lambda: False)
    assert main(["proxy", "--", "/opt/mcp/canari-serveur"]) == Exit.USAGE
    assert executed == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == proxy.NOT_INSTALLED + "\n"


def test_the_proxy_of_the_workspace_is_found() -> None:
    assert proxy.installed()


def test_the_help_names_the_proxy_subcommand() -> None:
    assert "proxy" in build_parser().format_help()
