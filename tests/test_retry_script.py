"""scripts/retry.sh : une commande refusée par la limite de débit d'un registre est retentée."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RETRY = ROOT / "scripts" / "retry.sh"


def run(*command: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — commande du test, sans shell.
        [str(RETRY), *command],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={"PATH": "/usr/bin:/bin", "RETRY_DELAY_SECONDS": "0"},
        cwd=tmp_path,
    )


def counting_command(tmp_path: Path, succeeds_at: int) -> tuple[str, ...]:
    """Une commande qui échoue jusqu'à son essai ``succeeds_at`` et compte ses essais."""
    script = (
        'n=$(( $(cat essais 2>/dev/null || echo 0) + 1 )); echo "$n" > essais; '
        f'[ "$n" -ge {succeeds_at} ]'
    )
    return ("sh", "-c", script)


def attempts(tmp_path: Path) -> int:
    return int((tmp_path / "essais").read_text())


def test_a_command_that_succeeds_runs_once(tmp_path: Path) -> None:
    result = run(*counting_command(tmp_path, succeeds_at=1), tmp_path=tmp_path)
    assert result.returncode == 0
    assert attempts(tmp_path) == 1
    assert result.stderr == ""


def test_a_passing_failure_is_retried_until_it_succeeds(tmp_path: Path) -> None:
    result = run(*counting_command(tmp_path, succeeds_at=3), tmp_path=tmp_path)
    assert result.returncode == 0
    assert attempts(tmp_path) == 3
    assert "essai 1 sur 3" in result.stderr
    assert "essai 2 sur 3" in result.stderr


def test_a_lasting_failure_stops_after_three_attempts_with_its_exit_code(tmp_path: Path) -> None:
    result = run("sh", "-c", "echo x >> essais; exit 7", tmp_path=tmp_path)
    assert result.returncode == 7
    assert (tmp_path / "essais").read_text().count("x") == 3


def test_the_command_receives_its_arguments_unchanged(tmp_path: Path) -> None:
    result = run("sh", "-c", 'printf "%s|" "$@" > arguments', "sh", "a b", "*", tmp_path=tmp_path)
    assert result.returncode == 0
    assert (tmp_path / "arguments").read_text() == "a b|*|"


def test_without_a_command_it_says_how_to_use_it(tmp_path: Path) -> None:
    result = run(tmp_path=tmp_path)
    assert result.returncode == 2
    assert "Usage" in result.stderr
