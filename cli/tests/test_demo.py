"""``answerlineage demo`` : la démonstration du dépôt, par le même chemin que ``docker compose up``.

Docker n'est pas lancé ici : un faux ``docker`` retient ce qu'on lui demande.
Le lancement réel est celui du pipeline (``scripts/check_demo.sh``).
"""

from __future__ import annotations

import stat
from pathlib import Path

import httpx
import pytest

from answerlineage_cli import demo
from answerlineage_cli.exits import Exit
from cli.tests.fakes import FakeApi, health, run, unreachable

COMPOSE = "# Démonstration\nname: answerlineage\n\nservices:\n  api:\n    image: x\n"


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """Un clone du dépôt, réduit à ce qui le fait reconnaître."""
    root = tmp_path / "AnswerLineage"
    (root / "deploy").mkdir(parents=True)
    (root / "deploy" / "Dockerfile").write_text("FROM scratch\n")
    (root / "compose.yaml").write_text(COMPOSE)
    return root


@pytest.fixture
def docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Un faux docker sur le PATH : il écrit ses arguments et son dossier, puis sort avec 0."""
    directory = tmp_path / "bin"
    directory.mkdir()
    record = tmp_path / "docker.log"
    script = directory / "docker"
    script.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$PWD" "$*" >> "{record}"\nexit "${{FAKE_DOCKER_EXIT:-0}}"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(directory))
    monkeypatch.setattr(demo, "POLL_SECONDS", 0.01)
    return record


def test_the_demonstration_starts_from_a_clone_and_gives_the_address_of_the_explorer(
    clone: Path, docker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(clone / "deploy")
    result = run(["demo"], FakeApi.sound(), monkeypatch)

    assert result.code == Exit.OK
    assert docker.read_text().splitlines() == [str(clone), "compose up --build --detach"]
    assert "http://127.0.0.1:8000/" in result.out
    assert "answerlineage doctor" in result.out
    assert "docker compose down" in result.out


def test_the_demonstration_waits_until_the_schema_is_migrated_and_the_explorer_served(
    clone: Path, docker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = iter(
        [
            httpx.Response(200, json=health(migrations="not_migrated", queue=None)),
            httpx.Response(503, json={"error": "query_timeout"}),
            httpx.Response(200, json=health()),
        ]
    )
    api = FakeApi.sound().route("/api/v1/health", lambda _: next(answers))
    result = run(["demo", "--directory", str(clone)], api, monkeypatch)

    assert result.code == Exit.OK
    assert [request.url.path for request in api.requests].count("/api/v1/health") == 3


def test_a_demonstration_that_does_not_answer_in_time_says_where_to_look(
    clone: Path, docker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run(
        ["demo", "--directory", str(clone), "--wait", "1"],
        httpx.MockTransport(unreachable),
        monkeypatch,
    )
    assert result.code == Exit.UNREACHABLE
    assert "docker compose logs" in result.err


def test_outside_a_clone_it_says_what_to_do(
    tmp_path: Path, docker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = run(["demo"], FakeApi.sound(), monkeypatch)

    assert result.code == Exit.USAGE
    assert "git clone https://github.com/alebgl77/AnswerLineage.git" in result.err
    assert not docker.exists()


def test_a_directory_with_another_compose_file_is_not_a_clone(
    clone: Path, docker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (clone / "compose.yaml").write_text("name: autre-projet\nservices: {}\n")
    result = run(["demo", "--directory", str(clone)], FakeApi.sound(), monkeypatch)
    assert result.code == Exit.USAGE
    assert not docker.exists()


def test_the_demonstration_listens_only_on_the_loopback(
    clone: Path, docker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run(
        ["demo", "--directory", str(clone), "--url", "http://lignage.exemple.fr:8000"],
        FakeApi.sound(),
        monkeypatch,
    )
    assert result.code == Exit.USAGE
    assert "boucle locale" in result.err
    assert not docker.exists()


def test_without_docker_it_says_so(
    clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "rien"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    result = run(["demo", "--directory", str(clone)], FakeApi.sound(), monkeypatch)
    assert result.code == Exit.FAILED
    assert "Docker" in result.err


def test_a_failing_compose_stops_the_command(
    clone: Path, docker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_DOCKER_EXIT", "17")
    api = FakeApi.sound()
    result = run(["demo", "--directory", str(clone)], api, monkeypatch)
    assert result.code == Exit.FAILED
    assert "17" in result.err
    assert api.requests == []


def test_the_compose_file_is_read_within_a_bound(
    clone: Path, docker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (clone / "compose.yaml").write_text(
        "#" * (demo.MAX_COMPOSE_BYTES + 1) + "\nname: answerlineage\n"
    )
    result = run(["demo", "--directory", str(clone)], FakeApi.sound(), monkeypatch)
    assert result.code == Exit.USAGE
    assert (clone / "compose.yaml").stat().st_size > demo.MAX_COMPOSE_BYTES
