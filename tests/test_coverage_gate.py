"""Porte de couverture (ENF-MAIN-01) : un composant sous le seuil bloque le pipeline."""

import json
from pathlib import Path

import pytest

from scripts.coverage_gate import ComponentCoverage, evaluate, load_config, main


def summary(
    statements: int, covered: int, branches: int, covered_branches: int
) -> dict[str, dict[str, int]]:
    return {
        "summary": {
            "num_statements": statements,
            "covered_lines": covered,
            "num_branches": branches,
            "covered_branches": covered_branches,
        }
    }


def report(files: dict[str, dict[str, dict[str, int]]]) -> dict[str, object]:
    return {"files": files}


def test_component_above_threshold_passes() -> None:
    data = report({"proxy/src/answerlineage_proxy/app.py": summary(100, 95, 40, 36)})
    results = evaluate(data, ["proxy/src"], minimum=85.0)
    assert [r.ok for r in results] == [True]
    assert results[0].branch_rate == pytest.approx(90.0)


def test_branch_rate_below_threshold_fails_even_with_high_line_rate() -> None:
    data = report({"proxy/src/answerlineage_proxy/app.py": summary(1000, 1000, 20, 10)})
    (result,) = evaluate(data, ["proxy/src"], minimum=85.0)
    assert result.combined_rate > 85.0
    assert result.branch_rate == pytest.approx(50.0)
    assert not result.ok


def test_combined_rate_below_threshold_fails() -> None:
    data = report({"proxy/src/answerlineage_proxy/app.py": summary(100, 60, 10, 10)})
    (result,) = evaluate(data, ["proxy/src"], minimum=85.0)
    assert result.branch_rate == pytest.approx(100.0)
    assert not result.ok


def test_files_are_aggregated_per_component_and_other_components_ignored() -> None:
    data = report(
        {
            "proxy/src/answerlineage_proxy/a.py": summary(10, 10, 4, 4),
            "proxy/src/answerlineage_proxy/b.py": summary(10, 8, 4, 3),
            "api/src/answerlineage_api/c.py": summary(10, 0, 4, 0),
        }
    )
    (result,) = evaluate(data, ["proxy/src"], minimum=85.0)
    assert result.statements == 20
    assert result.branches == 8
    assert result.branch_rate == pytest.approx(87.5)
    assert result.ok


def test_prefix_does_not_match_a_sibling_directory() -> None:
    data = report({"proxy/src2/x.py": summary(10, 0, 2, 0)})
    (result,) = evaluate(data, ["proxy/src"], minimum=85.0)
    assert not result.measured
    assert not result.ok


def test_component_without_branches_counts_as_fully_covered_on_branches() -> None:
    component = ComponentCoverage(
        path="api/src", statements=3, covered=3, branches=0, covered_branches=0
    )
    assert component.branch_rate == 100.0
    assert component.combined_rate == 100.0
    assert component.passes(85.0)


def test_unmeasured_component_fails_so_a_typo_cannot_disable_the_gate() -> None:
    (result,) = evaluate(report({}), ["proxy/src"], minimum=85.0)
    assert not result.ok


def test_main_returns_one_and_names_the_failing_component(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    coverage_file = tmp_path / "coverage.json"
    coverage_file.write_text(
        json.dumps(report({"proxy/src/p.py": summary(10, 2, 2, 0)})), encoding="utf-8"
    )
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[tool.answerlineage.coverage_gate]\nminimum = 85\npaths = ["proxy/src"]\n',
        encoding="utf-8",
    )
    assert main(["--report", str(coverage_file), "--config", str(pyproject)]) == 1
    assert "proxy/src" in capsys.readouterr().out


def test_main_returns_zero_when_every_component_holds(tmp_path: Path) -> None:
    coverage_file = tmp_path / "coverage.json"
    coverage_file.write_text(
        json.dumps(report({"proxy/src/p.py": summary(10, 10, 2, 2)})), encoding="utf-8"
    )
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[tool.answerlineage.coverage_gate]\nminimum = 85\npaths = ["proxy/src"]\n',
        encoding="utf-8",
    )
    assert main(["--report", str(coverage_file), "--config", str(pyproject)]) == 0


def test_main_fails_when_the_report_is_missing(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[tool.answerlineage.coverage_gate]\nminimum = 85\npaths = ["proxy/src"]\n',
        encoding="utf-8",
    )
    assert main(["--report", str(tmp_path / "absent.json"), "--config", str(pyproject)]) == 2


def test_repository_gate_covers_the_proxy_at_85_percent() -> None:
    root = Path(__file__).resolve().parent.parent
    minimum, paths = load_config(root / "pyproject.toml")
    assert minimum >= 85.0
    assert "proxy/src" in paths
