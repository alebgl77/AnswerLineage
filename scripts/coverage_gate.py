"""Porte de couverture du pipeline (ENF-MAIN-01).

Lit le rapport JSON de coverage.py et vérifie, composant par composant, que la
couverture de branches et la couverture combinée (lignes et branches) tiennent
le seuil réglé dans ``[tool.answerlineage.coverage_gate]``.

Usage : ``python -m scripts.coverage_gate [--report coverage.json] [--config pyproject.toml]``
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ComponentCoverage:
    """Couverture agrégée des fichiers d'un composant."""

    path: str
    statements: int
    covered: int
    branches: int
    covered_branches: int
    measured: bool = True

    @property
    def branch_rate(self) -> float:
        if self.branches == 0:
            return 100.0
        return 100.0 * self.covered_branches / self.branches

    @property
    def combined_rate(self) -> float:
        total = self.statements + self.branches
        if total == 0:
            return 100.0
        return 100.0 * (self.covered + self.covered_branches) / total

    def passes(self, minimum: float) -> bool:
        return self.measured and self.branch_rate >= minimum and self.combined_rate >= minimum


@dataclass(frozen=True)
class GateResult:
    component: ComponentCoverage
    minimum: float

    @property
    def ok(self) -> bool:
        return self.component.passes(self.minimum)

    @property
    def measured(self) -> bool:
        return self.component.measured

    @property
    def statements(self) -> int:
        return self.component.statements

    @property
    def branches(self) -> int:
        return self.component.branches

    @property
    def branch_rate(self) -> float:
        return self.component.branch_rate

    @property
    def combined_rate(self) -> float:
        return self.component.combined_rate


def _aggregate(files: Mapping[str, Any], path: str) -> ComponentCoverage:
    prefix = path.rstrip("/") + "/"
    statements = covered = branches = covered_branches = 0
    measured = False
    for name, entry in files.items():
        if not Path(name).as_posix().startswith(prefix):
            continue
        measured = True
        summary = entry["summary"]
        statements += int(summary["num_statements"])
        covered += int(summary["covered_lines"])
        branches += int(summary.get("num_branches", 0))
        covered_branches += int(summary.get("covered_branches", 0))
    return ComponentCoverage(path, statements, covered, branches, covered_branches, measured)


def evaluate(report: Mapping[str, Any], paths: Sequence[str], minimum: float) -> list[GateResult]:
    """Évalue chaque composant suivi contre le seuil."""
    files = report.get("files", {})
    return [GateResult(_aggregate(files, path), minimum) for path in paths]


def load_config(pyproject: Path) -> tuple[float, list[str]]:
    config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    gate = config["tool"]["answerlineage"]["coverage_gate"]
    return float(gate["minimum"]), [str(path) for path in gate["paths"]]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=Path("coverage.json"))
    parser.add_argument("--config", type=Path, default=Path("pyproject.toml"))
    args = parser.parse_args(argv)

    minimum, paths = load_config(args.config)
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"Rapport de couverture introuvable : {args.report}. Lancer d'abord make test.")
        return 2

    failed = False
    for result in evaluate(report, paths, minimum):
        if not result.measured:
            status = "ÉCHEC (aucun fichier mesuré)"
        elif result.ok:
            status = "ok"
        else:
            status = "ÉCHEC"
        failed = failed or not result.ok
        print(
            f"{result.component.path:<18} branches {result.branch_rate:6.2f} %  "
            f"lignes et branches {result.combined_rate:6.2f} %  seuil {minimum:.0f} %  {status}"
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
